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
    """素材目录是**可选**白名单：配了才拦越界，没配就放行（只校验文件存在）。"""

    def test_相对路径按素材目录解析(self):
        self.assertEqual(self.cfg.resolve_media_path("a.mp4"), (self.root / "a.mp4").resolve())

    def test_越界被拒(self):
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"x")
        self.addCleanup(outside.unlink)
        with self.assertRaises(ConfigError) as ctx:
            self.cfg.resolve_media_path(str(outside))
        self.assertIn("素材目录之外", str(ctx.exception))

    def test_没配素材目录时不限制目录(self):
        """★ 默认不挡：没配白名单时，目录外的文件也能发布（可用优先）。"""
        outside = self.root.parent / "outside-any.mp4"
        outside.write_bytes(b"x")
        self.addCleanup(outside.unlink)
        cfg = RuntimeConfig(media_dir="")
        self.assertIsNone(cfg.media_root())
        self.assertEqual(cfg.resolve_media_path(str(outside)), outside.resolve())

    def test_没配素材目录时仍然拦不存在的文件(self):
        """放开目录限制 ≠ 不校验：文件本身不存在还是要报错。"""
        cfg = RuntimeConfig(media_dir="")
        with self.assertRaises(ConfigError) as ctx:
            cfg.resolve_media_path(str(self.root / "nope.mp4"))
        self.assertIn("文件不存在", str(ctx.exception))

    def test_配了但目录不存在要报错(self):
        """配了却指错地方（盘符写错/目录被删）不能静默放行。"""
        cfg = RuntimeConfig(media_dir=str(self.root / "no-such-dir"))
        with self.assertRaises(ConfigError) as ctx:
            cfg.media_root()
        self.assertIn("素材目录不存在", str(ctx.exception))

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
            seen["description"] = description
            seen["tags"] = list(tags or [])

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

    def test_视频分支的正文与话题也传到了填表(self):
        """★★ 正文与话题在**两条分支**上都必须真的送到填表函数。

        背景（2026-10-08）：用户报"发布时正文和话题丢失"，实测结论是**话题**没被
        抖音认成真话题（`hashtag_id=0`），修在 `creator_steps.fill_title_and_description`
        —— 那是两条分支**共用**的入口，所以视频分支一并受益（已真机验证：
        视频页载荷里 3 个话题 `hashtag_id` 与图文页完全一致）。

        ★ 这里钉的是"别让将来某条分支单独开一套填表逻辑"：一旦有人给视频或图文
        另写一个填表入口，这个修复就会**只对一半分支生效**，而且症状极隐蔽
        （参数校验全绿、发布也成功，只是话题变成纯文本）。
        """
        payload, seen = self._run_video(
            {
                "credential_path": str(self.cred),
                "timeout_sec": 60,
                "video": {
                    "path": str(self.root / "a.mp4"),
                    "title": "周末随拍",
                    "description": "随手记录",
                    "tags": ["生活", "日常"],
                },
            }
        )
        self.assertEqual(seen.get("description"), "随手记录",
                         "视频分支的正文没送到填表（话题点选修复会失效）")
        self.assertEqual(seen.get("tags"), ["生活", "日常"],
                         "视频分支的话题没送到填表（hashtag_id 会退化成 0）")

    def test_两条分支共用同一个填表入口(self):
        """源码级钉住：`creator_publish` 只 import 一个 `fill_title_and_description`。

        两条分支（视频/图文）都必须在 `_publish` 里汇到**同一个**函数上——
        话题点选这个修复只住在那里，分叉就会漏。
        """
        import inspect
        import re as _re

        src = inspect.getsource(cp)
        # 实际**调用点**（`await fill_...(...)`）；import 语句是多行 `from ... import (` 形式，不计入
        calls = _re.findall(r"await\s+fill_title_and_description\s*\(", src)
        self.assertEqual(len(calls), 1,
                         "creator_publish 里出现了多于一个填表调用点，两条分支可能已分叉")
        # 调用点必须落在 `_publish` 内部（视频/图文唯一的汇合处）
        body = src[src.index("async def _publish("):]
        self.assertIn("await fill_title_and_description(", body,
                      "填表调用必须留在 _publish 里（两条分支的汇合点）")


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


class TestTitleInputSelectors(unittest.TestCase):
    """★ 标题输入框必须能同时在**视频页**和**图文页**命中。

    回归来源（真实缺陷，2026-10-08 真机）：`fill_title_and_description` 原先写死
    `input[placeholder*="填写作品标题"]` —— 那是**视频**发布页的 placeholder。
    图文发布页（`post/image`）的标题框是「**添加**作品标题」，于是：
    图片全部传完 → 等标题框 visible → **卡满 120 秒** → 抛 `TimeoutError`。
    用户看到的是一句"等待标题输入框超时（120秒）"，而它其实只是**选择器写窄了**。

    这里用假 page 断言候选选择器**覆盖两种 placeholder**，并且 `_find_title_input`
    能在第一个候选落空时继续试后面的。
    """

    def _fake_page(self, present: dict):
        """present: 选择器 -> 命中数量。返回一个假 page。"""
        from douyin_publish_mcp import creator_steps as cs

        calls = []

        class FakeLoc:
            def __init__(self, sel):
                self.sel = sel

            @property
            def first(self):
                return self

            async def count(self):
                return present.get(self.sel, 0)

            async def wait_for(self, state=None, timeout=None):
                if not present.get(self.sel, 0):
                    raise RuntimeError("not visible")
                return True

            async def fill(self, text):
                calls.append(("fill", self.sel, text))

        class FakePage:
            async def wait_for_timeout(self, ms):
                return None

            def locator(self, sel):
                return FakeLoc(sel)

        return FakePage(), calls, cs

    def test_选择器覆盖两种_placeholder(self):
        from douyin_publish_mcp import creator_steps as cs

        joined = " ".join(cs._TITLE_INPUT_SELECTORS)
        self.assertIn("作品标题", joined)
        # ★ 关键：不能写成写死的"填写作品标题"，否则图文页（"添加作品标题"）就落空
        for sel in cs._TITLE_INPUT_SELECTORS:
            self.assertNotIn("填写作品标题", sel, "不要写死'填写'，图文页是'添加'")

    def test_图文页_添加作品标题_能命中(self):
        """图文页的 placeholder 是「添加作品标题」。"""
        import asyncio

        from douyin_publish_mcp import creator_steps as cs

        page, calls, _ = self._fake_page({'input[placeholder*="作品标题"]': 1})
        node = asyncio.run(cs._find_title_input(page, timeout_ms=3000))
        asyncio.run(node.fill("标题"))
        self.assertEqual(calls, [("fill", 'input[placeholder*="作品标题"]', "标题")])

    def test_视频页_填写作品标题_也命中(self):
        """同一个选择器要能覆盖视频页的「填写作品标题」。"""
        import asyncio

        from douyin_publish_mcp import creator_steps as cs

        # 视频页：第一个候选命中（因为用的是 *="作品标题"）
        page, calls, _ = self._fake_page({'input[placeholder*="作品标题"]': 1})
        node = asyncio.run(cs._find_title_input(page, timeout_ms=3000))
        self.assertIsNotNone(node)

    def test_第一个候选落空时继续试后面的(self):
        """★ 兜底逻辑本身也要验：首个候选不命中，不能就地卡死。"""
        import asyncio

        from douyin_publish_mcp import creator_steps as cs

        page, calls, _ = self._fake_page({
            'input[placeholder*="标题"]': 1,          # 只有第二个候选命中
        })
        node = asyncio.run(cs._find_title_input(page, timeout_ms=4000))
        asyncio.run(node.fill("兜底命中"))
        self.assertEqual(calls, [("fill", 'input[placeholder*="标题"]', "兜底命中")])

    def test_全都落空时给出候选清单(self):
        """一个都没命中 → 报错要把候选选择器列出来（便于下次照着实测改）。"""
        import asyncio

        from douyin_publish_mcp import creator_steps as cs

        page, _calls, _ = self._fake_page({})
        with self.assertRaises(Exception) as ctx:
            asyncio.run(cs._find_title_input(page, timeout_ms=600))
        self.assertIn("没找到标题输入框", str(ctx.exception))
        self.assertIn("作品标题", str(ctx.exception))


class TestMentionPick(unittest.TestCase):
    """★★ 话题必须**在下拉里点选**才算真话题（回归真实缺陷，2026-10-08）。

    真机抓抖音的提交载荷（`/web/api/media/aweme/create_v2/`）看到：
    填 ` #音乐节` 之后直接按空格，提交时三个话题的 `hashtag_id` **全是 0**
    —— 抖音只当它是普通文本，作品页不会变成可点击的话题标签，也就是"话题丢失"。
    同一个载荷里正文是完整的，所以这不是"没填进去"，而是"填法不对"。
    下拉框的 class 前缀实测为 `mention-suggest-`（`mention-suggest-mount-dom` /
    `mention-suggest-item-container-*`），且**按空格的那一瞬间下拉就收起**。
    """

    def _fake_page(self, *, suggest_visible: bool, item_text: str = "音乐节",
                   click_result: bool = True):
        from douyin_publish_mcp import creator_steps as cs

        calls = []

        class FakeItem:
            def __init__(self, sel):
                self.sel = sel

            @property
            def first(self):
                return self

            async def count(self):
                # ★ 下拉没弹出时**没有候选条目**（不是"容器不可见"）——
                #   这正是 `_pick_mention` 改成轮询候选条目本身的原因。
                if "tag-hash-view-name" in self.sel:
                    return 1 if suggest_visible else 0
                return 1

            async def inner_text(self, timeout=None):
                return item_text

            async def scroll_into_view_if_needed(self, timeout=None):
                return None

            async def bounding_box(self):
                return None      # 让 native_click 走 locator.click 分支

            async def click(self, timeout=None):
                calls.append(("click", self.sel))
                return click_result

        class FakeBox:
            def __init__(self, sel):
                self.sel = sel

            @property
            def first(self):
                return self

            def locator(self, sel):
                return FakeItem(sel)

            async def count(self):
                # 候选条目选择器：下拉没弹出时 count=0（_pick_mention 轮询的就是它）
                if "tag-hash-view-name" in self.sel:
                    return 1 if suggest_visible else 0
                return 1

            async def wait_for(self, state=None, timeout=None):
                if not suggest_visible:
                    raise RuntimeError("not visible")
                return True

            async def inner_text(self, timeout=None):
                return item_text

            async def scroll_into_view_if_needed(self, timeout=None):
                return None

            async def bounding_box(self):
                return None

            async def click(self, timeout=None):
                calls.append(("click", self.sel))
                return click_result

        class FakePage:
            def locator(self, sel):
                return FakeBox(sel)

            async def wait_for_timeout(self, ms):
                return None

        return FakePage(), calls, cs

    def test_下拉出现时点选候选(self):
        """正常路径：下拉可见 → 点第一个候选，返回 True。"""
        import asyncio

        page, calls, cs = self._fake_page(suggest_visible=True)
        self.assertTrue(asyncio.run(cs._pick_mention(page, "音乐节")))
        self.assertTrue(any(c[0] == "click" for c in calls), "必须真的点一下候选")

    def test_下拉没出现时返回False(self):
        """没有建议框（话题词太冷门）→ 如实返回 False，不抛异常。"""
        import asyncio

        page, calls, cs = self._fake_page(suggest_visible=False)
        # 关键词与候选文本设成一致，确保返回 False 的原因是"没有候选"而不是"名字不匹配"
        self.assertFalse(asyncio.run(cs._pick_mention(page, "音乐节", timeout_ms=300)))
        self.assertEqual(calls, [], "没有候选就不该有点击")

    def test_候选不含关键词时选择器就匹配不到(self):
        """下拉里只有「音乐现场」而要的是「音乐」→ 不点（宁可退回纯文本也别挂错）。

        真机实测：查"音乐"时第一条常是「音乐现场」，盲选就把内容挂错了。
        ★ 现在这条保证由**选择器本身**（`:text-is()` 精确匹配）承担，
          不再是"取首条文本再判断"。
        """
        import asyncio

        # 假 page 只在"有该文本的候选"时才算命中：这里没有「音乐」这一行
        page, calls, cs = self._fake_page(suggest_visible=False)
        self.assertFalse(asyncio.run(cs._pick_mention(page, "音乐", timeout_ms=300)))
        self.assertEqual(calls, [], "没有精确匹配的候选就不该有点击")

    def test_选择器指向单个候选而非整个列表(self):
        """★ 不能选 `mention-suggest-item-container` 本身：那是整个列表容器。

        真机 DOM：容器 517×300，点它的中心会点中**列表中间**那条候选 ——
        实测把「#音乐节」点成了「#音乐节穿搭」。必须精确到候选行里的名字 span。
        """
        from douyin_publish_mcp import creator_steps as cs

        sel = cs._mention_item_selector("音乐节")
        self.assertIn("tag-hash-view-name", sel)
        self.assertIn("mention-suggest-item-container", sel,
                      "必须限定在候选列表容器内")

    def test_按文本精确匹配候选而非取第一条(self):
        """★★ 候选列表的**第一条不保证是精确匹配**。

        真机实测：抖音会把**正文里已出现的实体词**也塞进候选列表
        （正文「去年挤在人潮…」会作为候选出现，且排在真实候选之前）。
        取 `.first` 会挂到不相干的话题上 —— 必须 `:text-is()` 精确等于话题词。
        """
        from douyin_publish_mcp import creator_steps as cs

        sel = cs._mention_item_selector("音乐节")
        self.assertIn(':text-is("音乐节")', sel,
                      "必须用 :text-is() 精确匹配，不能依赖候选顺序")

    def test_选择器里的引号被转义(self):
        """话题词里带引号不能让选择器语法崩掉。"""
        from douyin_publish_mcp import creator_steps as cs

        sel = cs._mention_item_selector('说"好"')
        self.assertIn('\\"', sel)
        self.assertEqual(sel.count(':text-is("'), 1)
        self.assertTrue(sel.endswith('")'))

    def test_用普通click而不是native_click(self):
        """★ 话题候选必须用普通 click：`native_click` 的第二套原生事件会多插一个话题。

        真机实测：点「#音乐节」时 `native_click` 先 mouse.click 插入成功，
        下拉随即重排，补发的原生事件按旧坐标命中了另一条候选，
        结果正文尾部凭空多出「我的长长长假」—— 用户根本没要求这个话题。
        """
        import inspect

        from douyin_publish_mcp import creator_steps as cs

        src = inspect.getsource(cs._pick_mention)
        self.assertIn("await item.click(", src, "必须先走普通 click")
        first_plain = src.find("await item.click(")
        first_native = src.find("native_click(page, item)")
        self.assertLess(first_plain, first_native,
                        "普通 click 必须在 native_click 之前")
        self.assertIn("native_click", src, "点不动时仍要有 native_click 兜底")

    def test_选择器用前缀匹配而非写死哈希后缀(self):
        """class 后缀是构建产物会变（如 `-F02Ddw`/`-DwMEe8`），只能 `*=` 前缀匹配。"""
        from douyin_publish_mcp import creator_steps as cs

        self.assertEqual(cs._MENTION_SUGGEST, '[class*="mention-suggest"]')
        for sel in (cs._MENTION_SUGGEST, cs._MENTION_LIST, cs._MENTION_NAME,
                    cs._mention_item_selector("音乐节")):
            # ★ 只认"构建哈希"那种后缀（如 `-F02Ddw` / `-DwMEe8`：大写开头且含数字），
            #   不能把正常的 `-suggest` / `-item-container` 一起误伤。
            self.assertNotRegex(sel, r"-[A-Z][A-Za-z0-9]*[0-9][A-Za-z0-9]*[\]'\"]",
                                "不要写死哈希后缀")

    def test_话题带前导井号也能归一(self):
        """用户可能传 '#音乐节'；`#` 不能重复打。"""
        from douyin_publish_mcp import creator_steps as cs
        import inspect

        src = inspect.getsource(cs.fill_title_and_description)
        self.assertIn('lstrip("#")', src, "填话题前必须去掉用户自带的前导 #")
        # 断言写入的是单个 # + 归一后的词
        self.assertIn('" #" + keyword', src)


if __name__ == "__main__":
    unittest.main()
