"""发布参数校验与结果渲染的单测（纯函数，不起浏览器）。

★ 这些校验**必须**发生在起浏览器之前：上传是最贵的一步（几十秒到几分钟），
  参数错了要当场拦下，而不是等文件传完才报"标题太长"。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import creator_publish as cp
from douyin_publish_mcp.config import ConfigError, RuntimeConfig


class ArgsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "a.mp4").write_bytes(b"v" * 16)
        (self.root / "cover.png").write_bytes(b"p" * 16)
        (self.root / "body.txt").write_text("正文", encoding="utf-8")
        self.cfg = RuntimeConfig(media_dir=str(self.root))


class TestMediaGate(ArgsCase):
    """素材目录是唯一的路径闸门：越界、缺配置、不存在都要拦住。"""

    def test_相对路径按素材目录解析(self):
        self.assertEqual(self.cfg.resolve_media_path("a.mp4"), (self.root / "a.mp4").resolve())

    def test_越界被拒(self):
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"x")
        self.addCleanup(outside.unlink)
        with self.assertRaises(ConfigError) as ctx:
            self.cfg.resolve_media_path(str(outside))
        self.assertIn("素材目录之外", str(ctx.exception))

    def test_没配素材目录时拒绝发布(self):
        cfg = RuntimeConfig(media_dir="")
        with self.assertRaises(ConfigError) as ctx:
            cfg.media_root()
        self.assertIn("DOUYIN_MEDIA_DIR", str(ctx.exception))

    def test_文件不存在被拒(self):
        with self.assertRaises(ConfigError):
            self.cfg.resolve_media_path("nope.mp4")


class TestValidation(ArgsCase):
    def test_标题必填且有长度上限(self):
        with self.assertRaises(cp.PublishAbort):
            cp.check_titles("", "", [])
        with self.assertRaises(cp.PublishAbort):
            cp.check_titles("x" * 31, "", [])

    def test_话题里不能有空格(self):
        with self.assertRaises(cp.PublishAbort) as ctx:
            cp.check_titles("标题", "", ["两个 词"])
        self.assertIn("空格", str(ctx.exception))

    def test_定时时间要晚于当前两小时(self):
        soon = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        with self.assertRaises(cp.PublishAbort) as ctx:
            cp.normalize_schedule(soon)
        self.assertIn("2 小时", str(ctx.exception))

    def test_定时时间被规范化(self):
        when = (datetime.now() + timedelta(days=2)).replace(hour=21, minute=30, second=0, microsecond=0)
        self.assertEqual(
            cp.normalize_schedule(when.strftime("%Y-%m-%dT%H:%M:%S")),
            when.strftime("%Y-%m-%d %H:%M"),
        )

    def test_空定时表示立即发布(self):
        self.assertEqual(cp.normalize_schedule(""), "")

    def test_文件后缀被校验(self):
        (self.root / "a.txt").write_bytes(b"x")
        with self.assertRaises(cp.PublishAbort):
            cp.require_file(str(self.root / "a.txt"), cp.VIDEO_EXTENSIONS, "视频")


class TestCredentialPrecheck(ArgsCase):
    """没登录就不该起浏览器：先报"去登录"，别让用户等两分钟才看到失败。"""

    def test_没有凭据文件时返回_not_logged_in(self):
        import asyncio

        payload = asyncio.run(
            cp.publish_video(
                {
                    "credential_path": str(self.root / "nope.json"),
                    "video": {"path": str(self.root / "a.mp4"), "title": "标题"},
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "not_logged_in")

    def test_参数错误时返回_invalid_args_且没起浏览器(self):
        import asyncio

        cred = self.root / "cred.json"
        cred.write_text("{}", encoding="utf-8")
        payload = asyncio.run(
            cp.publish_video(
                {
                    "credential_path": str(cred),
                    "video": {"path": str(self.root / "a.mp4"), "title": "x" * 40},
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "invalid_args")
        self.assertEqual(payload["state"], "invalid_args")


class TestVideoBranchUsesTitle(unittest.TestCase):
    """★ 视频分支必须把 title 落到本地变量。

    回归来源（真实缺陷，2026-10-08 干跑抓到）：`_publish` 里 `title` 原先**只在图文分支**
    赋值，视频分支直接拿去 `fill_title_and_description(page, title, ...)` ——
    于是发布视频时，参数校验全过、浏览器也起来了、文件也选上了、表单页也进了，
    到"填标题"那一刻抛 `UnboundLocalError: cannot access local variable 'title'`。

    这是最难发现的一类：**预检（服务端）全绿，崩在最靠近发布的一步**；
    而单测当时只覆盖到"没凭据/参数错"两个提前返回的分支，从没跑到过第 360 行。

    这里用一个假的 playwright 把流程驱动到"填标题"之前，断言 title 真的被用上了。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "a.mp4").write_bytes(b"v" * 16)
        self.cred = self.root / "cred.json"
        self.cred.write_text('{"cookies": [{"name": "sessionid", "value": "x"}]}', encoding="utf-8")

    def _run_video(self, monkeypatch_targets):
        """跑一次 publish_video，停在"填标题"处（用桩实现替代浏览器动作）。"""
        import asyncio
        import sys
        import types
        from unittest import mock

        seen = {}

        async def fake_launch_context(playwright, channel, headless, storage_state=""):
            page = mock.MagicMock(name="page")
            page.url = "https://creator.douyin.com/creator-micro/content/upload"
            page.wait_for_timeout = mock.AsyncMock()
            context = mock.MagicMock(name="context")
            context.new_page = mock.AsyncMock(return_value=page)
            context.close = mock.AsyncMock()
            browser = mock.MagicMock(name="browser")
            browser.close = mock.AsyncMock()
            return browser, context

        async def fake_goto_upload(page, deadline):
            return None

        async def fake_wait_upload_input(page, deadline):
            el = mock.MagicMock()
            el.count = mock.AsyncMock(return_value=1)
            el.set_input_files = mock.AsyncMock()
            return el

        async def fake_wait_publish_form(page, deadline, note=False):
            return "v2"

        async def fake_fill_title(page, title, description, tags):
            seen["title"] = title

        # ★ 必须把 patchright 也桩掉：`_publish` 是**函数内** import 它的
        #   （刻意如此：参数/凭据的问题不该被"没装驱动"盖住）。测试环境没有驱动，
        #   不桩就会在 no_driver 提前返回，永远走不到下面这段 —— 这也正是
        #   这个缺陷此前没被单测发现的原因。
        pw_cm = mock.MagicMock(name="playwright_context_manager")
        pw_cm.__aenter__ = mock.AsyncMock(return_value=mock.MagicMock(name="playwright"))
        pw_cm.__aexit__ = mock.AsyncMock(return_value=False)
        fake_api = types.ModuleType("patchright.async_api")
        fake_api.async_playwright = lambda: pw_cm
        fake_pkg = types.ModuleType("patchright")
        fake_pkg.async_api = fake_api

        with mock.patch.dict(sys.modules, {"patchright": fake_pkg,
                                           "patchright.async_api": fake_api}), \
             mock.patch.object(cp, "launch_context", fake_launch_context), \
             mock.patch.object(cp, "_goto_upload", fake_goto_upload), \
             mock.patch.object(cp, "_wait_upload_input", fake_wait_upload_input), \
             mock.patch.object(cp, "_wait_publish_form", fake_wait_publish_form), \
             mock.patch.object(cp, "fill_title_and_description", fake_fill_title), \
             mock.patch.object(cp, "_wait_video_uploaded", mock.AsyncMock()), \
             mock.patch.object(cp, "enable_sync_switch", mock.AsyncMock(return_value=False)), \
             mock.patch.object(cp, "_click_publish_until_done",
                               mock.AsyncMock(return_value="failed")):
            payload = asyncio.run(cp.publish_video(monkeypatch_targets))
        return payload, seen

    def test_视频分支把标题传到了填表那一步(self):
        payload, seen = self._run_video(
            {
                "credential_path": str(self.cred),
                "timeout_sec": 60,
                "video": {
                    "path": str(self.root / "a.mp4"),
                    "title": "周末随拍",
                    "description": "随手记录",
                    "tags": ["生活"],
                },
            }
        )
        # ★ 这就是原来的崩点：能走到 fill_title 说明没有 UnboundLocalError
        self.assertEqual(seen.get("title"), "周末随拍", "视频分支没有把 title 带到填表那一步")
        # 结果本身是"没点动发布"，但那不是这一步要验的（原来连这一步都到不了）
        self.assertEqual(payload.get("error", {}).get("kind"), "publish_failed")


class TestPayloadContract(unittest.TestCase):
    """helper 那一行 JSON 的形状：服务端与状态页都按它取字段。"""
    def test_失败时_state_等于错误种类(self):
        from douyin_publish_mcp.creator import _fail

        payload = _fail("timeout", "超时了", "重试")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["state"], "timeout")
        self.assertEqual(payload["error"]["kind"], "timeout")
        self.assertTrue(payload["error"]["hint"])

    def test_helper_入口认识三种_action(self):
        from douyin_publish_mcp.creator_helper import _run

        import asyncio

        payload = asyncio.run(_run({"action": "没这个"}))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["kind"], "bad_spec")


class CreatorHelperDriverPathTest(unittest.TestCase):
    """★ 发布件的 P0：`--creator-helper` 必须先把驱动目录插进 sys.path。

    实测背景（exe 上真发一条时炸出来的）：
      `douyin-publish-mcp.exe` 冻结后本进程的 `sys.path` 由引导器定，
      **`PYTHONPATH` 不再生效** —— 驱动目录只能靠 `DOUYIN_BROWSER_DRIVER_PATH`
      显式 `sys.path.insert`。
      `browser_helper` 有这一步（所以**读取**一直正常），但 `e013686` 新建
      `creator_helper` 时漏了 → **登录与发布**两条路都报 `no_driver:
      No module named 'patchright'`，而同一个 exe 的读取却好好的，非常难定位。

    ★ 为什么源码态测试跑不出来：源码模式下是非冻结解释器，`PYTHONPATH` 兜住了，
      所以 13 项真机验收全 PASS 也照不到这个坑。必须用**行为断言**钉住。
    """

    def test_入口会先插驱动目录再跑(self):
        from douyin_publish_mcp import creator_helper
        from douyin_publish_mcp import browser_helper

        calls = []

        def fake_extend():
            calls.append(1)
            return ["C:/fake/driver/site-packages"]

        with mock.patch.object(browser_helper, "_extend_sys_path", fake_extend), \
             mock.patch.object(creator_helper, "main", creator_helper.main):
            # 用一个不存在的 spec：走完"插路径 → 读 spec 失败 → 回一行 JSON"这条链
            rc = creator_helper.main(["不存在-spec.json"])

        self.assertEqual(calls, [1], "creator_helper 没调用 _extend_sys_path —— 发布必报 no_driver")
        self.assertEqual(rc, 1)

    def test_认_DOUYIN_BROWSER_DRIVER_PATH_环境变量(self):
        """`_extend_sys_path` 真的会读那个环境变量（两端约定不能只写一半）。"""
        import os
        import sys
        import tempfile

        from douyin_publish_mcp import browser_helper

        with tempfile.TemporaryDirectory() as tmp:
            before = list(sys.path)
            try:
                with mock.patch.dict(os.environ,
                                     {"DOUYIN_BROWSER_DRIVER_PATH": tmp}, clear=False):
                    added = browser_helper._extend_sys_path()
                self.assertIn(tmp, added)
                self.assertIn(tmp, sys.path)
            finally:
                sys.path[:] = before


if __name__ == "__main__":
    unittest.main()
