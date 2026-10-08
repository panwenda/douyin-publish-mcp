"""登录会话的单测（不真起浏览器：`browser.spawn_helper` / `check_account` 全部打桩）。

钉三件事：

1. **单一待扫码会话**：开新的必须把旧的关掉 —— 否则每点一次登录就多一个浏览器
   活到各自超时为止（小红书 MCP 的 login_session.go 就是为这个写的）。
2. **结束后的自动检查**：自动化进程退出码 0 不代表"扫上了"，结论要看那次检查。
3. **等扫码期间不做检查**：那时用户该做的事是去扫码，不该给他一份"上次的结论"。
"""

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import douyin_browser as browser
from douyin_publish_mcp.account import AccountCheck
from douyin_publish_mcp.config import RuntimeConfig
from douyin_publish_mcp.login_session import LoginSessionManager, run_check


class FakeProc:
    """假登录进程：`exit_after=None` = 永远等不到扫码结果。

    stdout 里放 helper 那行 JSON（真 helper 就是这么约定的），stderr 放进度。
    """

    def __init__(self, exit_after=None, stdout=(), stderr=()):
        self.stdout = io.BytesIO(b"".join(stdout))
        self.stderr = io.BytesIO(b"".join(stderr))
        self._exit = exit_after
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._exit

    def wait(self, timeout=None):
        if self._exit is None:
            if timeout is None:
                time.sleep(0.2)
                return None
            time.sleep(min(timeout, 0.05))
            raise subprocess.TimeoutExpired("fake-helper", timeout)
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 1

    def kill(self):
        self.killed = True
        self._exit = 1


def helper_run(proc, spec_dir: Path) -> browser.HelperRun:
    """把假进程包成真的 HelperRun（cleanup 会去删 spec 文件，删不到也不算错）"""
    return browser.HelperRun(proc=proc, spec_path=spec_dir / "spec.json", host=browser.Host(["x"]))


def payload_ok(**extra):
    body = {"ok": True, "action": "login", "state": "success", "logged_in": True}
    body.update(extra)
    return json.dumps(body).encode()


def payload_fail(kind="failed", message="boom"):
    return json.dumps(
        {"ok": False, "state": kind, "error": {"kind": kind, "message": message, "hint": ""}}
    ).encode()


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"DOUYIN_DATA_DIR": str(self.root)}, clear=False)
        self._env.start()
        self.cfg = RuntimeConfig(media_dir=str(self.root), timeout=30)
        self.mgr = LoginSessionManager(self.cfg)

    def tearDown(self):
        self.mgr.cancel_login()
        self._env.stop()
        self._tmp.cleanup()

    def _spawn(self, proc):
        return mock.patch.object(
            browser, "spawn_helper", return_value=helper_run(proc, self.root)
        )

    def _check(self, logged_in=True, output="已登录"):
        return mock.patch(
            "douyin_publish_mcp.login_session.check_account",
            return_value=AccountCheck(account="main", logged_in=logged_in, ok=True, output=output),
        )


class TestSingleSession(SessionCase):
    def test_开新会话会关掉旧的(self):
        first, second = FakeProc(exit_after=None), FakeProc(exit_after=None)
        with mock.patch.object(
            browser, "spawn_helper",
            side_effect=[helper_run(first, self.root), helper_run(second, self.root)],
        ):
            self.mgr.start_login("main")
            self.assertTrue(self.mgr.snapshot()["running"])
            self.mgr.start_login("main2")
        self.assertTrue(first.terminated, "旧的待扫码会话必须被关掉")
        snap = self.mgr.snapshot()
        self.assertEqual(snap["account"], "main2")
        self.assertTrue(snap["running"])

    def test_取消会话(self):
        proc = FakeProc(exit_after=None)
        with self._spawn(proc):
            self.mgr.start_login("main")
            self.mgr.cancel_login()
            deadline = time.time() + 3
            while time.time() < deadline and self.mgr.snapshot()["running"]:
                time.sleep(0.05)
        self.assertTrue(proc.terminated)
        self.assertIn("关闭", self.mgr.snapshot()["error"])

    def test_会话结束后自动检查并落状态(self):
        proc = FakeProc(exit_after=0, stdout=[payload_ok(), b"\n"], stderr=[b"creator: waiting\n"])
        with self._spawn(proc), self._check() as check:
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 3
            while time.time() < deadline and self.mgr.snapshot().get("loggedIn") is not True:
                time.sleep(0.05)
        self.assertGreaterEqual(check.call_count, 1, "登录流程结束后必须补一次检查")
        snap = self.mgr.snapshot()
        self.assertIs(True, snap["loggedIn"])
        self.assertTrue(snap["succeeded"])
        self.assertIn("waiting", snap["output"], "进度（stderr）也要能看到")

    def test_登录失败时带出错信息(self):
        proc = FakeProc(exit_after=1, stdout=[payload_fail("no_browser", "打不开浏览器：没有 Chrome")])
        with self._spawn(proc), self._check(logged_in=False):
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 2
            while time.time() < deadline and not self.mgr.snapshot()["error"]:
                time.sleep(0.05)
        snap = self.mgr.snapshot()
        self.assertFalse(snap["succeeded"])
        self.assertIn("没有 Chrome", snap["error"])

    def test_超时的登录会话带_state(self):
        proc = FakeProc(exit_after=0, stdout=[payload_fail("timeout", "等待扫码登录超时")])
        with self._spawn(proc), self._check(logged_in=False):
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 2
            while time.time() < deadline and self.mgr.snapshot()["state"] != "timeout":
                time.sleep(0.05)
        self.assertEqual("timeout", self.mgr.snapshot()["state"])


class TestCheck(SessionCase):
    def test_check_记录最近一次结论(self):
        with self._check() as check:
            rec = self.mgr.check("main")
        self.assertIs(True, rec.logged_in)
        self.assertEqual(rec.account, "main")
        self.assertIs(True, self.mgr.snapshot()["loggedIn"])
        self.assertIs(True, self.mgr.snapshot()["lastCheck"]["loggedIn"])
        self.assertEqual(check.call_count, 1)

    def test_检查未登录(self):
        with self._check(logged_in=False, output="未登录"):
            rec = run_check(self.cfg, "main")
        self.assertIs(False, rec.logged_in)

    def test_检查不开浏览器(self):
        """★ 检查是直连探针：不该起进程、也不该碰驱动（v0.2 那版要开一个浏览器）。"""
        with self._check(), mock.patch.object(browser, "spawn_helper") as spawn:
            self.mgr.check("main")
        spawn.assert_not_called()

    def test_等待扫码期间_检查被跳过(self):
        proc = FakeProc(exit_after=None)
        with self._spawn(proc), self._check() as check:
            self.mgr.start_login("main")
            rec = self.mgr.check("main")
        check.assert_not_called()        # ★ 不许再查一次（用户此刻该去扫码）
        self.assertTrue(rec.skipped)
        self.assertIsNone(rec.logged_in, "跳过不等于没登录")
        self.assertIn("等扫码", rec.output)


class TestSnapshotShape(SessionCase):
    def test_没有会话时的快照(self):
        snap = self.mgr.snapshot()
        self.assertFalse(snap["running"])
        self.assertIsNone(snap["loggedIn"])
        self.assertEqual(snap["account"], "")
        self.assertFalse(snap["qrAvailable"])

    def test_二维码出现后可取(self):
        from douyin_publish_mcp.login_session import qr_path_for

        qr = qr_path_for("main")
        qr.parent.mkdir(parents=True, exist_ok=True)
        qr.write_bytes(b"\x89PNG\r\n\x1a\n")
        proc = FakeProc(exit_after=None)
        with self._spawn(proc):
            session = self.mgr.start_login("main")
        self.assertTrue(session["running"])
        self.assertTrue(self.mgr.snapshot()["qrAvailable"], "helper 落盘的二维码应能被发现")
        self.assertEqual(self.mgr.snapshot()["qrPath"], str(qr))

    def test_会话结束会清掉二维码(self):
        from douyin_publish_mcp.login_session import qr_path_for

        qr = qr_path_for("main")
        qr.parent.mkdir(parents=True, exist_ok=True)
        qr.write_bytes(b"\x89PNG\r\n\x1a\n")
        proc = FakeProc(exit_after=0, stdout=[payload_ok()])
        with self._spawn(proc), self._check():
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 2
            while time.time() < deadline and self.mgr.snapshot()["running"]:
                time.sleep(0.05)
        self.assertFalse(qr.exists(), "一次性登录物料收尾时要清掉")


class TestSpecContract(SessionCase):
    def test_spec_里带上了凭据与二维码落点(self):
        proc = FakeProc(exit_after=None)
        with mock.patch.object(
            browser, "spawn_helper", return_value=helper_run(proc, self.root)
        ) as spawn:
            self.mgr.start_login("main", headed=False)
        spec = spawn.call_args.args[1]
        self.assertEqual(spec["action"], "login")
        self.assertEqual(spec["account"], "main")
        self.assertFalse(spec["headed"])
        self.assertTrue(spec["credential_path"].endswith("douyin_main.json"))
        self.assertTrue(spec["qr_path"].endswith(".png"))
        self.assertGreater(spec["max_wait_sec"], 0)


class TestAccountCheckFailurePaths(SessionCase):
    """`check_account()` 的**失败分支**必须给出可读提示，不是内部错误。

    ★ 为什么单独钉：两条失败分支拼文案时用了 `exc.message`，而 `DouyinWebError`
      原先只在 `__init__` 里把 message 交给了 `super()`，自己**没留这个属性** ——
      于是"凭据失效"与"被风控挡住"（都是预期内的正常失败）会抛
      `AttributeError: 'DouyinWebError' object has no attribute 'message'`，
      把一句有用的「下一步：重新扫码」变成一个吓人的内部错误。实测踩到。
    """

    def _cred_file(self, cookie="sessionid=abc; ttwid=xyz"):
        p = self.root / "douyin_main.json"
        p.write_text(json.dumps({"cookies": [
            {"name": k, "value": v, "domain": ".douyin.com"}
            for k, v in (kv.split("=", 1) for kv in cookie.split("; "))
        ]}), encoding="utf-8")
        return p

    def _run_check(self, raise_kind, raise_msg="探针挂了"):
        from douyin_publish_mcp.account import check_account
        from douyin_publish_mcp.douyin_web import DouyinWebError

        self._cred_file()
        cfg = RuntimeConfig(media_dir=str(self.root), account="main", timeout=30)

        def boom(self, *a, **k):
            raise DouyinWebError(raise_msg, kind=raise_kind)

        with mock.patch.dict(os.environ, {"DOUYIN_COOKIE_FILE": str(self.root / "douyin_main.json")}):
            with mock.patch.object(
                __import__("douyin_publish_mcp.douyin_web", fromlist=["DouyinWebClient"]).DouyinWebClient,
                "my_profile", boom,
            ):
                return check_account(cfg, "main")

    def test_凭据失效时给重新扫码的指引而不是报错(self):
        res = self._run_check("no_credential", "凭据里的登录标识已失效")
        body = res.output
        self.assertIn("未登录", body)
        self.assertIn("重新扫码", body, "应给出下一步动作")
        self.assertIn("凭据里的登录标识已失效", body, "原始原因要保留")
        self.assertNotIn("AttributeError", body)
        self.assertFalse(res.logged_in)

    def test_被风控挡住时说判不出来而不是说没登录(self):
        res = self._run_check("blocked", "403 Blocked by ArgusSecurityPlugin")
        body = res.output
        self.assertIn("判不出来", body)
        self.assertIn("blocked", body)
        self.assertIn("403 Blocked", body)
        self.assertNotIn("AttributeError", body)
        # ★ 关键语义：判不出来 ≠ 没登录，绝不能说"你没登录"（会骗用户重扫）
        self.assertIsNone(res.logged_in, "被风控时结论必须是 None，不能是 False")


if __name__ == "__main__":
    unittest.main()
