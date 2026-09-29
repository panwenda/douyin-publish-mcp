"""登录会话的单测（不真起浏览器：`sau.spawn` / `sau.run` 全部打桩）。

钉三件事：

1. **单一待扫码会话**：开新的必须把旧的关掉 —— 否则每点一次登录就多一个浏览器
   活到各自超时为止（小红书 MCP 的 login_session.go 就是为这个写的）。
2. **结束后的自动 check**：`login` 退出码 0 不代表"扫上了"，结论要看 check。
3. **串行**：sau 一次只跑一个浏览器实例，check 与 login 不能并发。
"""

import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp import sau
from douyin_publish_mcp.login_session import LoginSessionManager, run_check


class FakeProc:
    """假登录进程：`exit_after=None` = 永远等不到扫码结果"""

    def __init__(self, exit_after=None, lines=()):
        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(b"")
        self._exit = exit_after
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        if self._exit is None:
            if timeout is None:
                time.sleep(0.2)
                return None
            time.sleep(min(timeout, 0.05))
            raise subprocess.TimeoutExpired("fake-sau", timeout)
        return self._exit

    def terminate(self):
        self.terminated = True
        self._exit = 1

    def kill(self):
        self.killed = True
        self._exit = 1


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = sau.SauConfig(cmd="sau", media_dir=str(self.root), timeout=30)
        self.mgr = LoginSessionManager(self.cfg)

    def tearDown(self):
        self.mgr.cancel_login()
        self._tmp.cleanup()

    def _ok(self, stdout="valid"):
        return sau.SauResult(argv=["sau"], exit_code=0, stdout=stdout, stderr="")


class TestSingleSession(SessionCase):
    def test_开新会话会关掉旧的(self):
        first, second = FakeProc(exit_after=None), FakeProc(exit_after=None)
        with mock.patch.object(sau, "spawn", side_effect=[first, second]):
            self.mgr.start_login("main")
            self.assertTrue(self.mgr.snapshot()["running"])
            self.mgr.start_login("main2")
        self.assertTrue(first.terminated, "旧的待扫码会话必须被关掉")
        snap = self.mgr.snapshot()
        self.assertEqual(snap["account"], "main2")
        self.assertTrue(snap["running"])

    def test_取消会话(self):
        proc = FakeProc(exit_after=None)
        with mock.patch.object(sau, "spawn", return_value=proc):
            self.mgr.start_login("main")
            self.mgr.cancel_login()
            deadline = time.time() + 3
            while time.time() < deadline and self.mgr.snapshot()["running"]:
                time.sleep(0.05)
        self.assertTrue(proc.terminated)
        self.assertIn("关闭", self.mgr.snapshot()["error"])

    def test_会话结束后自动check并落状态(self):
        proc = FakeProc(exit_after=0, lines=[b"Douyin login flow completed\n"])
        with mock.patch.object(sau, "spawn", return_value=proc), mock.patch.object(
            sau, "run", return_value=self._ok()
        ) as run:
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 3
            while time.time() < deadline and self.mgr.snapshot().get("loggedIn") is not True:
                time.sleep(0.05)
        self.assertGreaterEqual(run.call_count, 1, "登录流程结束后必须补一次 check")
        snap = self.mgr.snapshot()
        self.assertIs(True, snap["loggedIn"])
        self.assertTrue(snap["succeeded"])
        self.assertIn("login flow completed", snap["output"])

    def test_登录失败时带出错信息(self):
        proc = FakeProc(exit_after=2, lines=[b"boom: chromium not found\n"])
        with mock.patch.object(sau, "spawn", return_value=proc), mock.patch.object(
            sau, "run", return_value=self._ok()
        ):
            self.mgr.start_login("main")
            self.mgr.wait(3)
            deadline = time.time() + 2
            while time.time() < deadline and not self.mgr.snapshot()["error"]:
                time.sleep(0.05)
        snap = self.mgr.snapshot()
        self.assertFalse(snap["succeeded"])
        self.assertIn("chromium not found", snap["error"])


class TestCheck(SessionCase):
    def test_check_记录最近一次且退出码优先(self):
        with mock.patch.object(sau, "run", return_value=self._ok()) as run:
            rec = self.mgr.check("main")
        self.assertIs(True, rec.logged_in)
        self.assertEqual(rec.account, "main")
        self.assertIs(True, self.mgr.snapshot()["loggedIn"])
        self.assertIs(True, self.mgr.snapshot()["lastCheck"]["loggedIn"])
        self.assertEqual(run.call_args.args[1][:2], ["douyin", "check"])

    def test_检查未登录(self):
        bad = sau.SauResult(argv=["sau"], exit_code=1, stdout="invalid", stderr="")
        with mock.patch.object(sau, "run", return_value=bad):
            rec = run_check(self.cfg, "main")
        self.assertIs(False, rec.logged_in)

    def test_并发调用被串行化(self):
        """★ 两个线程同时 call：真跑的进程数必须始终是 1（CLI 会抢浏览器资料目录）"""
        live = {"now": 0, "max": 0}
        guard = threading.Lock()

        def slow_run(*_a, **_kw):
            with guard:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.1)
            with guard:
                live["now"] -= 1
            return self._ok()

        with mock.patch.object(sau, "run", side_effect=slow_run):
            threads = [threading.Thread(target=self.mgr.check, args=("main",)) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(live["max"], 1, "sau 调用必须串行")


    def test_等待扫码期间_检查被跳过(self):
        proc = FakeProc(exit_after=None)
        with mock.patch.object(sau, "spawn", return_value=proc), mock.patch.object(sau, "run") as run:
            self.mgr.start_login("main")
            rec = self.mgr.check("main")
        run.assert_not_called()          # ★ 不许再开一个浏览器去查
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
        qr = self.root / "qrcode.png"
        qr.write_bytes(b"\x89PNG\r\n\x1a\n")
        proc = FakeProc(exit_after=None)
        with mock.patch.object(sau, "spawn", return_value=proc):
            session = self.mgr.start_login("main")
        self.assertTrue(session["running"])
        self.assertTrue(self.mgr.snapshot()["qrAvailable"], "目录里落盘的二维码应能被发现")
        self.assertEqual(self.mgr.snapshot()["qrPath"], str(qr))


if __name__ == "__main__":
    unittest.main()
