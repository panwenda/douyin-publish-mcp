"""`sau` 封装的纯函数单测（不碰进程、不联网）。

跑法（零三方依赖，标准库 unittest）：

    cd E:\\项目\\AI\\douyin-publish-mcp
    set PYTHONPATH=src
    ..\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import sau


def cfg(**kw) -> sau.SauConfig:
    base = dict(cmd="", project_dir="", uv="uv", media_dir="", timeout=900, headless=True)
    base.update(kw)
    return sau.SauConfig(**base)


class TestFromEnv(unittest.TestCase):
    def test_默认值与开关解析(self):
        c = sau.SauConfig.from_env(env={})
        self.assertEqual(c.base_argv(), ["sau"])          # 都没有配置 → 走 PATH
        self.assertTrue(c.headless)
        self.assertEqual(c.timeout, 900)

    def test_SAU_CMD_优先于_SAU_DIR(self):
        c = sau.SauConfig.from_env(
            env={"SAU_CMD": r"C:\tools\sau.exe", "SAU_DIR": r"D:\sau"}
        )
        self.assertEqual(c.base_argv(), [r"C:\tools\sau.exe"])

    def test_SAU_DIR_走_uv_run(self):
        c = sau.SauConfig.from_env(env={"SAU_DIR": r"D:\sau"})
        self.assertEqual(
            c.base_argv(), ["uv", "run", "--directory", r"D:\sau", "sau"]
        )
        c2 = sau.SauConfig.from_env(env={"SAU_DIR": r"D:\sau", "SAU_NO_SYNC": "1"})
        self.assertIn("--no-sync", c2.base_argv())

    def test_素材目录默认取项目目录_且验证码文件在项目根(self):
        c = sau.SauConfig.from_env(env={"SAU_DIR": r"D:\sau"})
        self.assertEqual(c.media_dir, r"D:\sau")
        self.assertTrue(c.verify_code_file.endswith("verify_code.txt"))

    def test_超时下限30秒_非法值回落(self):
        self.assertEqual(sau.SauConfig.from_env(env={"SAU_TIMEOUT": "5"}).timeout, 30)
        self.assertEqual(sau.SauConfig.from_env(env={"SAU_TIMEOUT": "abc"}).timeout, 900)


class TestArgs(unittest.TestCase):
    def test_check_args(self):
        self.assertEqual(
            sau.check_args("main"),
            ["douyin", "check", "--account", "main"],
        )

    def test_login_args_有头无头(self):
        self.assertIn("--headed", sau.login_args("main", True))
        self.assertIn("--headless", sau.login_args("main", False))

    def test_upload_video_全参数(self):
        args = sau.upload_video_args(
            "main",
            r"D:\sau\videos\a.mp4",
            "  周末 随拍  ",
            "记录日常",
            ["运动", "训练"],
            "2026-03-24 21:30",
            headless=True,
        )
        self.assertEqual(args[:2], ["douyin", "upload-video"])
        self.assertIn("--file", args)
        self.assertIn(r"D:\sau\videos\a.mp4", args)
        # 标题单行化（多余空白折成一个空格）
        self.assertIn("周末 随拍", args)
        self.assertIn("运动,训练", args)
        self.assertIn("2026-03-24 21:30", args)
        self.assertIn("--headless", args)

    def test_upload_video_可选参数缺席时不出现(self):
        args = sau.upload_video_args("main", "a.mp4", "标题")
        self.assertNotIn("--desc", args)
        self.assertNotIn("--tags", args)
        self.assertNotIn("--schedule", args)

    def test_tags_兼容字符串写法(self):
        args = sau.upload_video_args("main", "a.mp4", "标题", tags="运动, 训练 健身")
        self.assertIn("运动,训练,健身", args)

    def test_upload_note_至少一张图(self):
        with self.assertRaises(sau.SauError):
            sau.upload_note_args("main", [], "标题")
        args = sau.upload_note_args("main", ["1.png", "2.png"], "图文标题", "正文")
        self.assertEqual(args[:2], ["douyin", "upload-note"])
        self.assertIn("--images", args)
        # --images 后紧跟全部图片（sau CLI 的 nargs 写法）
        i = args.index("--images")
        self.assertEqual(args[i + 1 : i + 3], ["1.png", "2.png"])
        self.assertIn("--note", args)

    def test_标题必填且单行且限长(self):
        with self.assertRaises(sau.SauError):
            sau.upload_video_args("main", "a.mp4", "   ")
        with self.assertRaises(sau.SauError) as ctx:
            sau.upload_video_args("main", "a.mp4", "字" * 31)
        self.assertIn("30", str(ctx.exception))          # 报错要说清上限
        self.assertIn("改写", str(ctx.exception))        # 并给出可执行的下一步

    def test_账号名拒绝可疑字符(self):
        for bad in ("", "  ", "a b", "a;b", 'a"b'):
            with self.assertRaises(sau.SauError):
                sau.check_args(bad)

    def test_定时时间格式(self):
        self.assertEqual(sau.normalize_schedule("2026-03-24 21:30"), "2026-03-24 21:30")
        self.assertEqual(sau.normalize_schedule("2026-03-24T21:30:00"), "2026-03-24 21:30")
        for bad in ("2026/03/24 21:30", "明天", "2026-03-24"):
            with self.assertRaises(sau.SauError):
                sau.normalize_schedule(bad)


class TestMediaWhitelist(unittest.TestCase):
    """★ 路径闸门：模型给的路径必须落在素材目录内（否则拒绝）"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "videos").mkdir()
        self.video = self.root / "videos" / "a.mp4"
        self.video.write_bytes(b"x" * 10)
        self.outside = self.root.parent / "outside.mp4"
        self.outside.write_bytes(b"x" * 10)

    def tearDown(self):
        self._tmp.cleanup()
        try:
            self.outside.unlink()
        except OSError:
            pass

    def test_目录内_相对路径与绝对路径都行(self):
        c = cfg(media_dir=str(self.root))
        self.assertEqual(sau.resolve_media_path(c, "videos/a.mp4"), self.video.resolve())
        self.assertEqual(sau.resolve_media_path(c, str(self.video)), self.video.resolve())

    def test_目录外被拒(self):
        c = cfg(media_dir=str(self.root))
        with self.assertRaises(sau.SauError) as ctx:
            sau.resolve_media_path(c, str(self.outside))
        self.assertIn("素材目录", str(ctx.exception))

    def test_用相对路径穿越也被拒(self):
        c = cfg(media_dir=str(self.root))
        with self.assertRaises(sau.SauError):
            sau.resolve_media_path(c, "../outside.mp4")

    def test_没配素材目录时拒绝发布(self):
        with self.assertRaises(sau.SauError) as ctx:
            sau.resolve_media_path(cfg(media_dir=""), "a.mp4")
        self.assertIn("SAU_MEDIA_DIR", str(ctx.exception))

    def test_空文件被拒(self):
        empty = self.root / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(sau.SauError):
            sau.resolve_media_path(cfg(media_dir=str(self.root)), str(empty))


class TestRun(unittest.TestCase):
    def test_找不到可执行文件时给出可执行建议(self):
        c = cfg(cmd=r"C:\nope\sau.exe")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("nope")):
            r = sau.run(c, ["douyin", "check", "--account", "main"])
        self.assertFalse(r.ok)
        self.assertIsNone(r.exit_code)
        self.assertIn("SAU_CMD", r.hint)

    def test_超时不抛异常_并提醒先核对再重试(self):
        import subprocess

        c = cfg(timeout=30)
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sau", timeout=30, output=b"partial"),
        ):
            r = sau.run(c, ["douyin", "upload-video"])
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertIn("不要直接重试", r.hint)
        self.assertIn("partial", r.stdout)

    def test_成功路径解析utf8输出(self):
        c = cfg()
        fake = mock.Mock(returncode=0, stdout="登录成功\n".encode("utf-8"), stderr=b"")
        with mock.patch("subprocess.run", return_value=fake) as m:
            r = sau.run(c, ["douyin", "check", "--account", "main"])
        self.assertTrue(r.ok)
        self.assertIn("登录成功", r.tail())
        # 子进程被要求用 UTF-8（否则 Windows 上中文输出会变问号）
        env = m.call_args.kwargs.get("env") or {}
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")


class TestOutputInterpretation(unittest.TestCase):
    def test_登录态粗判_猜不出来时返回None(self):
        self.assertIs(True, sau.parse_logged_in("logged_in: True"))
        self.assertIs(True, sau.parse_logged_in("已登录：main"))
        self.assertIs(False, sau.parse_logged_in("logged_in: False"))
        self.assertIs(False, sau.parse_logged_in("未登录，请扫码"))
        self.assertIsNone(sau.parse_logged_in("done in 3.2s"))
        self.assertIsNone(sau.parse_logged_in(""))
        # ★ 同时出现正反关键词时以"否定"为准（"未登录"通常带着 "logged_in: False"）
        self.assertIs(False, sau.parse_logged_in("未登录 logged_in: false"))

    def test_二维码识别(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "qrcode.png"
            p.write_bytes(b"\x89PNG")
            self.assertEqual(sau.parse_qrcode_path("", extra_roots=[d]), p)
            self.assertIsNone(sau.parse_qrcode_path("", extra_roots=[d + "_nope"]))

    def test_脱敏(self):
        out = sau.redact("cookie: sessionid=abc123; ttwid=zzz; other=keep")
        self.assertNotIn("abc123", out)
        self.assertNotIn("zzz", out)
        self.assertIn("keep", out)


class TestCliParity(unittest.TestCase):
    """★ 与 sau CLI 的**真实**开关对齐（见 sau_cli.py 的 douyin 子命令定义）。

    这些参数原来都没暴露：少一个的代价不是"发不出去"，而是"发出去的作品缺了
    该有的东西"（封面、带货、声明、合集），用户往往过几天才发现。
    """

    def test_视频_封面与带货与声明与合集(self):
        args = sau.upload_video_args(
            "main",
            "a.mp4",
            "标题",
            thumbnail_portrait="p.png",
            thumbnail_landscape="l.png",
            product_link="https://haohuo/detail/1",
            product_title="面膜",
            declaration="虚构演绎，仅供娱乐",
            collection="我的合集",
        )
        for flag, value in (
            ("--thumbnail-portrait", "p.png"),
            ("--thumbnail-landscape", "l.png"),
            ("--product-link", "https://haohuo/detail/1"),
            ("--product-title", "面膜"),
            ("--declaration", "虚构演绎，仅供娱乐"),
            ("--collection", "我的合集"),
        ):
            self.assertIn(flag, args, args)
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_带货只给一半直接报错(self):
        with self.assertRaises(sau.SauError):
            sau.upload_video_args("main", "a.mp4", "标题", product_link="https://x/1")
        with self.assertRaises(sau.SauError):
            sau.upload_video_args("main", "a.mp4", "标题", product_title="面膜")

    def test_图文_notef_与_bgm(self):
        args = sau.upload_note_args(
            "main", ["1.png"], "标题", note_file="body.md", bgm="轻快 纯音乐"
        )
        self.assertIn("--notef", args)
        self.assertEqual(args[args.index("--notef") + 1], "body.md")
        self.assertEqual(args[args.index("--bgm") + 1], "轻快 纯音乐")
        self.assertNotIn("--note", args)

    def test_正文给两种写法直接报错(self):
        with self.assertRaises(sau.SauError) as ctx:
            sau.upload_note_args("main", ["1.png"], "标题", note="文本", note_file="body.md")
        self.assertIn("只能给一种", str(ctx.exception))


class TestLoginStateParse(unittest.TestCase):
    """★ 退出码优先：CLI 的 check 是 0=valid / 1=invalid（结构化事实），文本兜底。"""

    def _r(self, code, stdout="", stderr="", timed_out=False):
        return sau.SauResult(
            argv=["sau"], exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out
        )

    def test_退出码优先于文本(self):
        self.assertIs(True, sau.parse_login_state(self._r(0, "valid")))
        self.assertIs(False, sau.parse_login_state(self._r(1, "invalid")))
        # 退出码 0 但输出说 invalid（旧版行为）：以文本为准，别报"已登录"
        self.assertIs(False, sau.parse_login_state(self._r(0, "logged_in: false")))

    def test_其它退出码_与超时(self):
        self.assertIs(True, sau.parse_login_state(self._r(2, "已登录")))     # 文本兜底
        self.assertIsNone(sau.parse_login_state(self._r(2, "看不出来")))
        self.assertIsNone(sau.parse_login_state(self._r(None, "登录成功", timed_out=True)))


class TestCredentials(unittest.TestCase):
    """★ 凭据文件的定位与「重置登录态」的白名单删除（本服务唯一的删除操作）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "cookies").mkdir()
        self.cfg = cfg(project_dir=str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def _cred(self, account):
        p = self.root / "cookies" / f"douyin_{account}.json"
        p.write_text('{"sessionid": "x"}', encoding="utf-8")
        return p

    def test_凭据路径口径(self):
        self.assertEqual(
            sau.account_file_path(self.cfg, "main"),
            (self.root / "cookies" / "douyin_main.json").resolve(),
        )

    def test_没配项目目录时报错(self):
        with self.assertRaises(sau.SauError) as ctx:
            sau.account_file_path(cfg(), "main")
        self.assertIn("SAU_DIR", str(ctx.exception))

    def test_账号名不能当文件名(self):
        for bad in ("../x", "a b", "a/b", "a\\b", "a" * 65, ""):
            with self.assertRaises(sau.SauError, msg=bad):
                sau.account_file_path(self.cfg, bad)

    def test_不传confirm只看不删(self):
        cred = self._cred("main")
        plan = sau.logout(self.cfg, "main", confirm=False)
        self.assertTrue(plan.existed)
        self.assertFalse(plan.deleted)
        self.assertTrue(cred.is_file(), "没确认就不许删")
        self.assertIn(str(cred), plan.describe())

    def test_传confirm才删且只删这一个(self):
        cred = self._cred("main")
        other = self._cred("other")
        plan = sau.logout(self.cfg, "main", confirm=True)
        self.assertTrue(plan.deleted)
        self.assertFalse(cred.exists())
        self.assertTrue(other.is_file(), "别的账号的凭据一律不许动")

    def test_没有凭据时如实说无需重置(self):
        plan = sau.logout(self.cfg, "main", confirm=True)
        self.assertFalse(plan.existed)
        self.assertIn("无需重置", plan.describe())


if __name__ == "__main__":
    unittest.main()
