"""发布参数校验与结果渲染的单测（纯函数，不起浏览器）。

★ 这些校验**必须**发生在起浏览器之前：上传是最贵的一步（几十秒到几分钟），
  参数错了要当场拦下，而不是等文件传完才报"标题太长"。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
