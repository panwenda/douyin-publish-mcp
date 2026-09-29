"""浏览器通道的单测：宿主/驱动解析、按需取驱动、错误映射、门面的"直连优先"。

★ 这些用例**不起浏览器、不联网**：`subprocess.run` 全部被打桩。
  钉的是四条契约：
  ① 门面只在直连被判 `blocked` 时才动用浏览器（能直连的接口不许平白多几秒）；
  ② "缺驱动 / 缺浏览器"要能分辨，并且各自只触发一次兜底（按需取驱动 / 下载 chromium）；
  ③ 浏览器通道的任何失败都必须变成可读的 `DouyinWebError`（带 kind 与下一步），
     绝不能把整个服务搞坏；
  ④ 交给模型的 DTO 与直连**完全同形**（两条通道给出同一批字段）。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import douyin_browser as db
from douyin_publish_mcp.douyin_cred import credential_from_raw
from douyin_publish_mcp.douyin_web import DouyinWebError


class FakeProc:
    """假的 subprocess 结果：stdout 只有一行 JSON（helper 的约定）"""

    def __init__(self, payload=None, stdout: str = "", stderr: str = "", code: int = 0):
        self.returncode = code
        self.stdout = stdout if payload is None else json.dumps(payload, ensure_ascii=False) + "\n"
        self.stderr = stderr


def ok_payload(**results):
    return {"ok": True, "browser": "chrome", "elapsed_ms": 1234, "results": results, "misses": {}}


def fail_payload(kind: str, message: str = "boom", hint: str = ""):
    return {"ok": False, "error": {"kind": kind, "message": message, "hint": hint}}


def aweme_detail_body(aweme_id="7664911070490126501"):
    return {
        "aweme_detail": {
            "aweme_id": aweme_id,
            "desc": "浏览器通道拿到的标题",
            "create_time": 1784682564,
            "author": {"nickname": "作者甲", "sec_uid": "MS4wLjABAAAAx", "unique_id": "jia"},
            "video": {"duration": 15000, "play_addr": {"url_list": ["https://v11-weba.douyinvod.com/playwm/x.mp4"]}},
            "statistics": {"digg_count": 1331695, "comment_count": 20825, "share_count": 7, "collect_count": 9},
            "music": {"title": "背景音乐"},
        }
    }


def user_post_body(n=2):
    return {
        "aweme_list": [
            {
                "aweme_id": "10%d" % i,
                "desc": "作品%d" % i,
                "author": {"nickname": "作者甲", "sec_uid": "MS4wLjABAAAAx"},
                "video": {"play_addr": {"url_list": ["https://v11-weba.douyinvod.com/playwm/%d.mp4" % i]}},
                "statistics": {"digg_count": i},
            }
            for i in range(n)
        ],
        "has_more": 1,
        "max_cursor": 1700000000000,
    }


class Completions:
    """按顺序返回若干次 subprocess 结果，并记下每次的 argv / env"""

    def __init__(self, *procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "env": kwargs.get("env") or {}})
        proc = self.procs[min(len(self.calls) - 1, len(self.procs) - 1)]
        if isinstance(proc, Exception):
            raise proc
        return proc


class FakeClient:
    """假的直连客户端：由用例决定"通"还是"被墙" """

    def __init__(self, error: str = "", video=None, user=None, videos=None):
        self.error = error
        self.video = video or {}
        self.user = user or {}
        self.videos = videos or {"videos": [], "has_more": False, "max_cursor": 0}
        self.calls = []

    def _maybe_raise(self, name):
        self.calls.append(name)
        if self.error == "blocked":
            raise DouyinWebError("抖音返回 403 Blocked by ArgusSecurityPlugin Uifid Not Found", kind="blocked")
        if self.error == "network":
            raise DouyinWebError("网络不通", kind="network")

    def video_detail(self, aweme_id):
        self._maybe_raise("video_detail")
        return self.video

    def user_profile(self, sec_user_id):
        self._maybe_raise("user_profile")
        return self.user

    def user_videos(self, sec_user_id, max_cursor=0, count=20):
        self._maybe_raise("user_videos")
        return self.videos

    def my_profile(self):
        self._maybe_raise("my_profile")
        return self.user


class HostResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dyb-test-"))
        self.driver_dir = self.tmp / "runtime" / "site-packages"
        (self.driver_dir / "patchright").mkdir(parents=True)
        self.patch_env = mock.patch.dict("os.environ", {"DOUYIN_BROWSER_SITE_PACKAGES": str(self.driver_dir)})
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)

    def test_describe_reports_off(self):
        text = db.describe(db.BrowserConfig(enabled=False))
        self.assertIn("已关闭", text)

    def test_describe_reports_driver_dir(self):
        text = db.describe(db.BrowserConfig(), None)
        self.assertIn(str(self.driver_dir), text)

    def test_driver_ready_sees_explicit_dir(self):
        self.assertTrue(db.driver_ready(db.BrowserConfig(), None))

    def test_resolve_host_dev_uses_self_and_helper_source(self):
        host = db.resolve_host(db.BrowserConfig(), None)
        self.assertIn("browser_helper.py", host.argv[1])
        self.assertEqual(host.argv[0], sys.executable)
        self.assertIn(str(self.driver_dir), host.sys_path)

    def test_resolve_host_frozen_uses_exe_entry(self):
        with mock.patch.object(sys, "frozen", True, create=True):
            host = db.resolve_host(db.BrowserConfig(), None)
        self.assertEqual(host.argv[1], "--browser-helper")
        self.assertEqual(host.argv[0], sys.executable)

    def test_resolve_host_frozen_with_explicit_python_is_rejected(self):
        # 冻结后没有 .py 源文件，显式指定解释器只会得到一个跑不起来的东西 —— 要当场说清
        with mock.patch.object(sys, "frozen", True, create=True):
            with mock.patch.dict("os.environ", {"DOUYIN_BROWSER_PYTHON": sys.executable}):
                with mock.patch.object(db, "_helper_source", lambda: None):
                    with self.assertRaises(DouyinWebError) as ctx:
                        db.resolve_host(db.BrowserConfig(), None)
        self.assertEqual(ctx.exception.kind, "upstream")
        self.assertIn("DOUYIN_BROWSER_SITE_PACKAGES", ctx.exception.hint)


class StorageStateTest(unittest.TestCase):
    def test_uses_credential_file_when_it_is_storage_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "douyin_main.json"
            path.write_text(json.dumps({"cookies": [{"name": "ttwid", "value": "x", "domain": ".douyin.com"}],
                                        "origins": []}), encoding="utf-8")
            cred = credential_from_raw(path.read_text(encoding="utf-8"), "test", path=path)
            self.assertEqual(db._storage_state_for(cred), str(path))

    def test_builds_temp_state_from_raw_cookie(self):
        cred = credential_from_raw("ttwid=abc; sessionid=def", "test")
        path = db._storage_state_for(cred)
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        names = [c["name"] for c in data["cookies"]]
        self.assertEqual(names, ["ttwid", "sessionid"])
        self.assertTrue(all(c["domain"] == ".douyin.com" for c in data["cookies"]))

    def test_no_cookie_gives_none(self):
        self.assertIsNone(db._storage_state_for(credential_from_raw("", "test")))


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.cfg = db.BrowserConfig()
        self.cred = credential_from_raw("ttwid=abc; sessionid=def", "test")
        self.tmp = Path(tempfile.mkdtemp(prefix="dyb-run-"))
        self.driver_dir = self.tmp / "site-packages"
        (self.driver_dir / "patchright").mkdir(parents=True)

    def test_read_passes_spec_and_returns_payload(self):
        runs = Completions(FakeProc(ok_payload(**{"video_detail:1": {"status": 200, "body": aweme_detail_body("1")}})))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                payload = db.read(self.cfg, self.cred, [{"kind": "video_detail", "aweme_id": "1"}], None)
        self.assertTrue(payload["ok"])
        spec = json.loads(Path(runs.calls[0]["argv"][-1]).read_text(encoding="utf-8"))
        self.assertEqual(spec["wants"], [{"kind": "video_detail", "aweme_id": "1"}])
        self.assertTrue(spec["headless"])
        self.assertEqual(spec["channel"], "auto")
        self.assertIn(str(self.driver_dir), runs.calls[0]["env"]["PYTHONPATH"])

    def test_missing_driver_triggers_one_bootstrap_then_retries(self):
        archive = self.tmp / "driver.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("patchright/__init__.py", "")
        runs = Completions(FakeProc(fail_payload("no_driver")), FakeProc(ok_payload()))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with mock.patch.dict("os.environ", {"DOUYIN_BROWSER_DRIVER_ARCHIVE": str(archive)}):
                    payload = db.read(self.cfg, self.cred, [{"kind": "my_profile"}], None)
        self.assertEqual(len(runs.calls), 2, "只允许多跑一次（取完驱动再试一次）")
        self.assertTrue(payload["ok"])
        # 第二次的 PYTHONPATH 里要有刚解出来的目录
        self.assertIn(str(self.driver_dir), runs.calls[1]["env"]["PYTHONPATH"])

    def test_missing_browser_triggers_download_then_retries(self):
        runs = Completions(
            FakeProc(fail_payload("no_browser")),
            FakeProc(stdout=""),                      # patchright install chromium
            FakeProc(ok_payload()),
        )
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                payload = db.read(self.cfg, self.cred, [{"kind": "my_profile"}], None)
        self.assertTrue(payload["ok"])
        install_argv = runs.calls[1]["argv"]
        self.assertIn("patchright", install_argv)
        self.assertEqual(install_argv[-2:], ["install", "chromium"])

    def test_no_download_switch_prevents_bootstrap(self):
        cfg = db.BrowserConfig(allow_download=False)
        runs = Completions(FakeProc(fail_payload("no_driver")))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with mock.patch.dict("os.environ", {"DOUYIN_BROWSER_DRIVER_ARCHIVE": str(self.tmp / "nope.zip")}):
                    payload = db.read(cfg, self.cred, [{"kind": "my_profile"}], None)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(runs.calls), 1)

    def test_timeout_becomes_upstream_error(self):
        runs = Completions(subprocess.TimeoutExpired(cmd="x", timeout=1))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with self.assertRaises(DouyinWebError) as ctx:
                    db.read(self.cfg, self.cred, [{"kind": "my_profile"}], None)
        self.assertEqual(ctx.exception.kind, "upstream")
        self.assertIn("HEADFUL", ctx.exception.hint)

    def test_garbage_stdout_becomes_upstream_error(self):
        runs = Completions(FakeProc(stdout="not json at all\n", stderr="Traceback...\n", code=1))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with self.assertRaises(DouyinWebError) as ctx:
                    db.read(self.cfg, self.cred, [{"kind": "my_profile"}], None)
        self.assertEqual(ctx.exception.kind, "upstream")


class FacadeTest(unittest.TestCase):
    """门面的核心契约：能直连就直连，被 blocked 才回退。"""

    def setUp(self):
        self.cred = credential_from_raw("ttwid=abc; sessionid=def", "test")
        self.cfg = db.BrowserConfig()
        self.tmp = Path(tempfile.mkdtemp(prefix="dyb-facade-"))
        self.driver_dir = self.tmp / "site-packages"
        (self.driver_dir / "patchright").mkdir(parents=True)

    def _run(self, client, method, *args, payload=None, cfg=None, **kwargs):
        runs = Completions(FakeProc(payload if payload is not None else ok_payload()))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                result = getattr(db, method)(cfg or self.cfg, client, *args, self.cred, None, **kwargs)
        return result, runs

    def test_direct_success_never_touches_browser(self):
        client = FakeClient(video={"aweme_id": "1", "desc": "直连标题"})
        result, runs = self._run(client, "video_detail", "1")
        self.assertEqual(result.source, "直连")
        self.assertEqual(runs.calls, [])

    def test_non_blocked_error_is_not_swallowed(self):
        # 网络问题不该被"浏览器通道能不能救"这件事掩盖 —— 直接抛回给调用方
        client = FakeClient(error="network")
        runs = Completions(FakeProc(ok_payload()))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with self.assertRaises(DouyinWebError) as ctx:
                    db.video_detail(self.cfg, client, "1", self.cred, None)
        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(runs.calls, [])

    def test_blocked_falls_back_and_maps_same_dto(self):
        client = FakeClient(error="blocked")
        payload = ok_payload(**{"video_detail:1": {"status": 200, "body": aweme_detail_body("1")}})
        result, _ = self._run(client, "video_detail", "1", payload=payload)
        self.assertIn("浏览器通道", result.source)
        self.assertEqual(result.data["desc"], "浏览器通道拿到的标题")
        self.assertEqual(result.data["stats"]["like"], 1331695)
        self.assertEqual(result.data["comments_count"], 20825)
        self.assertEqual(result.data["music"], "背景音乐")
        self.assertTrue(result.data["download_url"].startswith("https://v11-weba.douyinvod.com/"))

    def test_user_videos_fallback(self):
        client = FakeClient(error="blocked")
        payload = ok_payload(**{"user_post:MS4wLjABAAAAx": {"status": 200, "body": user_post_body(3)}})
        result, _ = self._run(client, "user_videos", "MS4wLjABAAAAx", payload=payload)
        self.assertEqual(len(result.data["videos"]), 3)
        self.assertTrue(result.data["has_more"])

    def test_user_profile_fallback(self):
        client = FakeClient(error="blocked")
        body = {"user": {"nickname": "作者甲", "sec_uid": "MS4wLjABAAAAx", "follower_count": 12}}
        payload = ok_payload(**{"user_profile": {"status": 200, "body": body}})
        result, _ = self._run(client, "user_profile", "MS4wLjABAAAAx", payload=payload)
        self.assertEqual(result.data["nickname"], "作者甲")
        self.assertEqual(result.data["follower_count"], 12)

    def test_my_profile_fallback(self):
        client = FakeClient(error="blocked")
        body = {"user": {"nickname": "我自己", "sec_uid": "MS4wLjABAAAAme"}}
        payload = ok_payload(**{"my_profile": {"status": 200, "body": body}})
        result, _ = self._run(client, "my_profile", payload=payload)
        self.assertEqual(result.data["nickname"], "我自己")

    def test_comments_are_browser_only(self):
        payload = ok_payload(**{
            "comments:1": {"status": 200, "body": {"comments": [
                {"text": "好吃", "user": {"nickname": "甲"}, "digg_count": 3, "create_time": 1784682564,
                 "reply_comment_total": 1},
            ], "has_more": False, "total": 1}}
        })
        runs = Completions(FakeProc(payload))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                result = db.comments(self.cfg, self.cred, "1", 5, None)
        self.assertEqual(result.data["comments"][0]["text"], "好吃")
        self.assertEqual(result.data["comments"][0]["reply_count"], 1)

    def test_browser_failure_says_blocked_not_silent(self):
        client = FakeClient(error="blocked")
        payload = {"ok": False, "error": {"kind": "no_response", "message": "等不到响应", "hint": "去确认登录态"},
                   "results": {}, "misses": {"video_detail:1": "等 detail 无响应（45 秒）"}}
        with mock.patch.object(db.subprocess, "run", Completions(FakeProc(payload))):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                with self.assertRaises(DouyinWebError) as ctx:
                    db.video_detail(self.cfg, client, "1", self.cred, None)
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertIn("确认登录态", ctx.exception.hint)

    def test_disabled_channel_propagates_blocked(self):
        client = FakeClient(error="blocked")
        cfg = db.BrowserConfig(enabled=False)
        runs = Completions(FakeProc(ok_payload()))
        with mock.patch.object(db.subprocess, "run", runs):
            with self.assertRaises(DouyinWebError) as ctx:
                db.video_detail(cfg, client, "1", self.cred, None)
        self.assertEqual(ctx.exception.kind, "blocked")
        self.assertEqual(runs.calls, [], "关掉通道后不许偷偷起浏览器")


class DownloadTest(unittest.TestCase):
    def test_ensure_driver_verifies_hash_from_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "driver.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("patchright/__init__.py", "")
            digest = db._sha256(archive)

            def fake_download(url, dst):
                dst.write_bytes(archive.read_bytes())

            with mock.patch.object(db, "DRIVER_DIR", tmp_path / "site-packages"):
                with mock.patch.object(db, "_download", fake_download):
                    with mock.patch.dict("os.environ", {"DOUYIN_BROWSER_DRIVER_URL": "https://x/driver.zip",
                                                        "DOUYIN_BROWSER_DRIVER_SHA256": digest}):
                        out = db.ensure_driver(db.BrowserConfig(), None)
            self.assertEqual(out, tmp_path / "site-packages")
            self.assertTrue((tmp_path / "site-packages" / "patchright" / "__init__.py").is_file())

    def test_hash_mismatch_refuses_to_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            archive = tmp_path / "driver.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("patchright/__init__.py", "")

            with mock.patch.object(db, "DRIVER_DIR", tmp_path / "site-packages"):
                with mock.patch.object(db, "_download", lambda url, dst: dst.write_bytes(b"truncated")):
                    with mock.patch.dict("os.environ", {"DOUYIN_BROWSER_DRIVER_URL": "https://x/driver.zip",
                                                        "DOUYIN_BROWSER_DRIVER_SHA256": db._sha256(archive)}):
                        with self.assertRaises(DouyinWebError) as ctx:
                            db.ensure_driver(db.BrowserConfig(), None)
            self.assertIn("哈希不匹配", str(ctx.exception))

    def test_no_source_configured_falls_back_to_builtin_release(self):
        # ★ 新机器上什么都不配时，缺驱动应该直接走**内置**的发布源（这是"按需取"的意义）
        calls = []

        def fake_download(url, dst):
            calls.append(url)
            dst.write_bytes(b"not a zip at all")   # 故意给垃圾：只为证明它去哪儿取

        stripped = {k: v for k, v in os.environ.items()
                    if k not in (db.DRIVER_URL_ENV, db.DRIVER_SHA_ENV, db.DRIVER_ARCHIVE_ENV)}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", stripped, clear=True):
                with mock.patch.object(db, "DRIVER_DIR", Path(tmp) / "site-packages"):
                    with mock.patch.object(db, "_download", fake_download):
                        with self.assertRaises(DouyinWebError):
                            db.ensure_driver(db.BrowserConfig(), None)
        self.assertEqual(len(calls), 1)
        self.assertIn("gitee.com/pan-wenda/douyin", calls[0])
        self.assertIn("douyin-browser-driver-win64-patchright", calls[0])

    def test_download_off_means_no_bootstrap(self):
        # DOUYIN_BROWSER_DOWNLOAD=0：内网机器宁可报错，也不许偷偷往外发请求
        stripped = {k: v for k, v in os.environ.items()
                    if k not in (db.DRIVER_URL_ENV, db.DRIVER_SHA_ENV, db.DRIVER_ARCHIVE_ENV)}
        with mock.patch.dict("os.environ", stripped, clear=True):
            with mock.patch.object(db, "_download", lambda url, dst: self.fail("不该发请求")):
                self.assertIsNone(db.ensure_driver(db.BrowserConfig(allow_download=False), None))


class CombinedFacadeTest(unittest.TestCase):
    """主页那两件事必须**一次抓全**（起一次浏览器不容易）。"""

    def setUp(self):
        self.cred = credential_from_raw("ttwid=abc; sessionid=def", "test")
        self.cfg = db.BrowserConfig()
        self.tmp = Path(tempfile.mkdtemp(prefix="dyb-page-"))
        self.driver_dir = self.tmp / "site-packages"
        (self.driver_dir / "patchright").mkdir(parents=True)

    def _call(self, fn, client, *args, payload, **kwargs):
        runs = Completions(FakeProc(payload))
        with mock.patch.object(db.subprocess, "run", runs):
            with mock.patch.object(db, "DRIVER_DIR", self.driver_dir):
                out = fn(self.cfg, client, *args, self.cred, sau_cfg=None, **kwargs)
        return out, runs

    def test_user_page_takes_both_in_one_browser_run(self):
        client = FakeClient(error="blocked")
        payload = ok_payload(
            **{"user_profile": {"status": 200, "body": {"user": {"nickname": "作者甲", "sec_uid": "MS4wLjABAAAAx"}}}},
            **{"user_post:MS4wLjABAAAAx": {"status": 200, "body": user_post_body(4)}},
        )
        page, runs = self._call(db.user_page, client, "MS4wLjABAAAAx", payload=payload, count=20)
        self.assertEqual(len(runs.calls), 1, "资料与作品要合并成一次调用")
        spec = json.loads(Path(runs.calls[0]["argv"][-1]).read_text(encoding="utf-8"))
        self.assertEqual([w["kind"] for w in spec["wants"]], ["user_profile", "user_post"])
        self.assertEqual(page.user["nickname"], "作者甲")
        self.assertEqual(len(page.videos), 4)
        self.assertTrue(page.has_more)
        self.assertIn("浏览器通道", page.user_source)

    def test_user_page_direct_path_costs_nothing(self):
        client = FakeClient(user={"nickname": "直连用户"}, videos={"videos": [{"aweme_id": "1"}], "has_more": False})
        page, runs = self._call(db.user_page, client, "MS4wLjABAAAAx", payload=ok_payload())
        self.assertEqual(runs.calls, [])
        self.assertEqual(page.user_source, "直连")
        self.assertEqual(page.videos_source, "直连")
        self.assertEqual(len(page.videos), 1)

    def test_user_page_can_skip_videos(self):
        client = FakeClient(error="blocked")
        payload = ok_payload(**{"user_profile": {"status": 200, "body": {"user": {"nickname": "甲"}}}})
        page, runs = self._call(db.user_page, client, "MS4wLjABAAAAx", payload=payload,
                                include_videos=False)
        self.assertEqual(page.videos, [])
        self.assertEqual(page.videos_source, "")
        self.assertEqual(len(runs.calls), 1)

    def test_self_page_falls_back_for_profile_and_videos_together(self):
        client = FakeClient(error="blocked")
        payload = ok_payload(
            **{"my_profile": {"status": 200, "body": {"user": {"nickname": "我自己", "sec_uid": "MS4wLjABAAAAme"}}}},
            **{"user_post:self": {"status": 200, "body": user_post_body(2)}},
        )
        page, runs = self._call(db.self_page, client, payload=payload, count=20)
        self.assertEqual(len(runs.calls), 1)
        spec = json.loads(Path(runs.calls[0]["argv"][-1]).read_text(encoding="utf-8"))
        self.assertEqual([w["kind"] for w in spec["wants"]], ["my_profile", "user_post"])
        self.assertEqual(page.user["nickname"], "我自己")
        self.assertEqual(len(page.videos), 2)

    def test_self_page_videos_only_fallback_when_profile_succeeded(self):
        # 资料直连通了、作品被挡：只把作品那条塞进浏览器，别顺手把资料也重抓
        class Half(FakeClient):
            def my_profile(self):
                return {"nickname": "我自己", "sec_user_id": "MS4wLjABAAAAme"}

        client = Half(error="blocked")
        # want 里没带 sec_user_id → helper 的结果键是 `user_post:self`（约定见 browser_helper._key）
        payload = ok_payload(**{"user_post:self": {"status": 200, "body": user_post_body(1)}})
        page, runs = self._call(db.self_page, client, payload=payload, count=20)
        self.assertEqual(page.user_source, "直连")
        self.assertIn("浏览器通道", page.videos_source)
        spec = json.loads(Path(runs.calls[0]["argv"][-1]).read_text(encoding="utf-8"))
        self.assertEqual([w["kind"] for w in spec["wants"]], ["user_post"])


class DriverPathTest(unittest.TestCase):
    """驱动落点：客户端与 helper 必须是**同一个地方**（分叉了会"取过了还说没驱动"）。"""

    def test_client_and_helper_share_one_definition(self):
        from douyin_publish_mcp import browser_helper

        self.assertEqual(db.DRIVER_DIR, browser_helper.default_driver_dir())
        self.assertEqual(db.RUNTIME_DIR, db.DRIVER_DIR.parent)

    def test_helper_finds_runtime_dir_without_any_env(self):
        # 直接手动跑 helper（没有客户端传 DOUYIN_BROWSER_DRIVER_PATH）时，也要认默认落点
        from douyin_publish_mcp import browser_helper

        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "site-packages"
            (fake / "patchright").mkdir(parents=True)
            stripped = {k: v for k, v in os.environ.items() if not k.startswith("DOUYIN_BROWSER")}
            with mock.patch.dict("os.environ", stripped, clear=True):
                with mock.patch.object(browser_helper, "default_driver_dir", lambda: fake):
                    added = browser_helper._extend_sys_path()
            self.assertIn(str(fake), added)
            self.assertIn(str(fake), sys.path)
            sys.path.remove(str(fake))

    def test_missing_driver_is_not_an_error(self):
        # 目录不存在时静默跳过（"没取驱动"不是崩溃，交给上层的 no_driver 说明）
        from douyin_publish_mcp import browser_helper

        with mock.patch.object(browser_helper, "default_driver_dir", lambda: Path("Z:/nope/nope")):
            self.assertEqual(browser_helper._extend_sys_path(), [])


if __name__ == "__main__":
    unittest.main()
