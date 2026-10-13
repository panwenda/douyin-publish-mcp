"""账号锁的单测：**同账号串行、不同账号并行**（不真起浏览器，全部用假工具）。

钉四件事：

1. **同账号必须串行**：两个浏览器带同一份登录态并发操作会被平台当"同一设备并发操作"，
   而且登录/发布**都会回写** storage_state，后写的会盖掉先写的。
2. **不同账号必须并行**：否则"多账号发同样内容"退化成一条队列。
3. **锁键必须归一化**：`" work "` 与 `"work"` 是同一份 `douyin_work.json`，
   用原始串当键就是"同一份凭据被两把锁保护" —— 串行静默失效。
4. **纯探针不吃锁**：`douyin_account_status` 只打直连探针（不开浏览器），
   锁住会让"扫完查一下"卡在正在跑的发布后面。
"""

import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from douyin_publish_mcp.account_lock import AccountLocks
from douyin_publish_mcp.config import RuntimeConfig, account_key, credential_path
from douyin_publish_mcp.server import _LOCK_FREE_TOOLS, Server


class Concurrency:
    """记录**临界区内**的并发峰值。

    ★ `enter()` 必须由被测代码在临界区**内部**调用。放在外面（比如线程启动时）的话，
      排队等待的人也会被算进去 —— 那样即使锁完全失效，峰值照样等于线程数，
      测试会给出"通过"的假象。
    """

    def __init__(self, hold_sec: float = 0.03):
        self.hold_sec = hold_sec
        self._inside = 0
        self._peak = 0
        self._guard = threading.Lock()

    def enter(self) -> None:
        with self._guard:
            self._inside += 1
            self._peak = max(self._peak, self._inside)
        time.sleep(self.hold_sec)      # 撑开并发窗口，让"真并发"能被看见
        with self._guard:
            self._inside -= 1

    @property
    def peak(self) -> int:
        return self._peak


def run_concurrently(fn, n: int) -> None:
    """让 n 个线程尽量同时冲进 fn（用 barrier 对齐起跑）。"""
    barrier = threading.Barrier(n)

    def worker() -> None:
        barrier.wait()
        fn()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)


class LockCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {"DOUYIN_DATA_DIR": str(self.root), "DOUYIN_ACCOUNT": ""},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _server(self) -> Server:
        """一个不落盘的 Server（工具全部由用例自己装）。"""
        return Server(RuntimeConfig(media_dir=str(self.root), timeout=30),
                      out=io.StringIO(), err=io.StringIO())

    @staticmethod
    def _call(server: Server, tool: str, account: str = "") -> dict:
        args = {"account": account} if account else {}
        return server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })


class TestAccountKey(LockCase):
    """归一口径：锁键与凭据文件必须**同源**。"""

    def test_空账号落到默认账号(self):
        self.assertEqual("main", account_key(""))
        self.assertEqual("main", account_key("   "))

    def test_非字符串按没给处理(self):
        # 模型偶尔传数字/对象；与 server._opt_str 同一口径（当成"没给"）
        self.assertEqual("main", account_key(None))    # type: ignore[arg-type]
        self.assertEqual("main", account_key(123))     # type: ignore[arg-type]

    def test_环境变量给出默认账号(self):
        with mock.patch.dict(os.environ, {"DOUYIN_ACCOUNT": "work"}):
            self.assertEqual("work", account_key(""))

    def test_清洗掉不安全字符并挡路径穿越(self):
        # ★ 断言的是"**清洗后不残留路径分隔符**"，而不是某个好看的账号名 ——
        #   连续的不安全字符会被压成一个 `_`（`../evil` → `_evil`）。
        for raw, want in (
            ("../evil", "_evil"),
            ("..", "_"),
            ("a/b", "a_b"),
            (r"a\b", "a_b"),
            ("a b", "a_b"),
            ("我的账号", "我的账号"),
        ):
            got = account_key(raw)
            self.assertEqual(want, got, "账号名 %r 的清洗结果" % raw)
            for bad in ("/", "\\", ".."):
                self.assertNotIn(bad, got, "%r 清洗后不得残留 %r" % (raw, bad))

    def test_锁键与凭据文件同源(self):
        """★ 整个设计的地基：`credential_path` 内部就该用 `account_key`。"""
        for raw in ("work", "  work  ", "我的账号", "../evil"):
            self.assertEqual(
                credential_path(raw).name,
                "douyin_%s.json" % account_key(raw),
                "账号 %r 的锁键与凭据文件名必须同源" % raw,
            )

    def test_凭据文件名未变_零迁移(self):
        self.assertEqual("douyin_main.json", credential_path("").name)
        self.assertEqual("douyin_work.json", credential_path("work").name)
        self.assertEqual("douyin_work.json", credential_path("  work  ").name)


class TestSameAccountSerial(LockCase):
    def test_同账号并发只有一个人在里面(self):
        locks = AccountLocks()
        seen = Concurrency()

        def fn() -> None:
            with locks.hold("work"):
                seen.enter()

        run_concurrently(fn, 6)
        self.assertEqual(1, seen.peak, "同一账号的临界区里最多只能有一个")

    def test_归一化后同名是同一把锁(self):
        locks = AccountLocks()
        acquired = threading.Event()

        def other() -> None:
            with locks.hold("  work  "):
                acquired.set()

        with locks.hold("work"):
            threading.Thread(target=other, daemon=True).start()
            self.assertFalse(
                acquired.wait(0.3),
                "「 work 」与「work」是同一份 douyin_work.json，必须互斥",
            )
        self.assertTrue(acquired.wait(2), "放锁后另一种写法应该能拿到")

    def test_锁覆盖整个工具调用(self):
        """★ 集成点：`Server.handle` 必须在**工具函数外面**上锁。"""
        server = self._server()
        seen = Concurrency(hold_sec=0.05)

        def fake_tool(cfg, args):
            seen.enter()
            return {"content": [{"type": "text", "text": "ok"}]}

        server.tools["fake_tool"] = fake_tool
        run_concurrently(lambda: self._call(server, "fake_tool", "work"), 4)
        self.assertEqual(1, seen.peak, "同账号的工具调用必须串行")

    def test_工具报错也要放锁(self):
        """工具抛异常时若漏放锁，这个号就永久卡死。"""
        server = self._server()

        def boom(cfg, args):
            raise RuntimeError("工具炸了")

        server.tools["boom_tool"] = boom
        resp = self._call(server, "boom_tool", "work")
        self.assertTrue(resp["result"]["isError"], "异常要变成工具错误回给模型")

        done = threading.Event()
        server.tools["ok_tool"] = lambda c, a: {"content": [{"type": "text", "text": "ok"}]}
        threading.Thread(
            target=lambda: (self._call(server, "ok_tool", "work"), done.set()),
            daemon=True,
        ).start()
        self.assertTrue(done.wait(2), "异常路径必须放锁")

    def test_没有account参数也上锁(self):
        """缺省账号（`DOUYIN_ACCOUNT`/main）同样是"一个号"，不能因为没传就免锁。"""
        server = self._server()
        seen = Concurrency(hold_sec=0.05)

        def fake_tool(cfg, args):
            seen.enter()
            return {"content": [{"type": "text", "text": "ok"}]}

        server.tools["fake_tool"] = fake_tool
        run_concurrently(lambda: self._call(server, "fake_tool"), 3)
        self.assertEqual(1, seen.peak, "不传 account 时落到默认账号，也必须串行")


class TestDifferentAccountParallel(LockCase):
    def test_不同账号可以并行(self):
        locks = AccountLocks()
        seen = Concurrency()
        names = ["a", "b", "c", "d"]

        def fn() -> None:
            with locks.hold(names.pop()):     # list.pop 原子，四个线程拿到四个不同名字
                seen.enter()

        run_concurrently(fn, 4)
        self.assertEqual(4, seen.peak, "不同账号必须能同时跑（否则多账号发布是一条队列）")

    def test_不同账号的工具调用可以并行(self):
        server = self._server()
        seen = Concurrency(hold_sec=0.05)
        names = ["a", "b", "c"]

        def fake_tool(cfg, args):
            seen.enter()
            return {"content": [{"type": "text", "text": "ok"}]}

        server.tools["fake_tool"] = fake_tool
        run_concurrently(lambda: self._call(server, "fake_tool", names.pop()), 3)
        self.assertEqual(3, seen.peak, "不同账号不该互相阻塞")


class TestLockFreeProbe(LockCase):
    def test_探针工具不吃账号锁(self):
        """发布期间也要能查登录态 —— 探针不开浏览器，没有理由排队。"""
        server = self._server()
        server.tools["douyin_account_status"] = lambda c, a: {
            "content": [{"type": "text", "text": "ok"}]
        }
        done = threading.Event()

        def call_it() -> None:
            self._call(server, "douyin_account_status", "work")
            done.set()

        with server.locks.hold("work"):        # 占住锁不放
            threading.Thread(target=call_it, daemon=True).start()
            self.assertTrue(done.wait(2), "纯探针不该被账号锁挡住")

    def test_免锁名单里只有探针(self):
        """★ 防呆：会开浏览器/写凭据的工具**绝不能**加进这个名单。"""
        for name in (
            "douyin_publish_video",
            "douyin_publish_note",
            "douyin_account_login",
            "douyin_account_logout",
        ):
            self.assertNotIn(name, _LOCK_FREE_TOOLS, "%s 必须吃账号锁" % name)
        self.assertEqual({"douyin_account_status"}, set(_LOCK_FREE_TOOLS))


class TestLockBookkeeping(LockCase):
    def test_异常也会放锁(self):
        locks = AccountLocks()
        with self.assertRaises(RuntimeError):
            with locks.hold("work"):
                raise RuntimeError("工具炸了")

        acquired = threading.Event()

        def fn() -> None:
            with locks.hold("work"):
                acquired.set()

        threading.Thread(target=fn, daemon=True).start()
        self.assertTrue(acquired.wait(2), "异常路径也必须放锁")

    def test_引用归零后表不残留(self):
        locks = AccountLocks()
        for name in ("a", "b", "c"):
            with locks.hold(name):
                pass
        self.assertEqual(0, locks.tracked(), "没人用的锁要摘掉，否则账号名一多表就涨")

    def test_排队期间表里仍有登记(self):
        locks = AccountLocks()
        waiting = threading.Event()

        def other() -> None:
            waiting.set()
            with locks.hold("work"):
                pass

        with locks.hold("work"):
            threading.Thread(target=other, daemon=True).start()
            waiting.wait(1)
            time.sleep(0.05)
            self.assertEqual(1, locks.tracked(), "还有人排队时不能摘登记")
        deadline = time.time() + 2
        while time.time() < deadline and locks.tracked():
            time.sleep(0.05)
        self.assertEqual(0, locks.tracked())

    def test_清表后重建的锁仍然互斥(self):
        """回归点：上一轮把表清空后，新一轮的两个人不能各拿一把**新**锁。"""
        locks = AccountLocks()
        with locks.hold("work"):
            pass

        seen = Concurrency()

        def fn() -> None:
            with locks.hold("work"):
                seen.enter()

        run_concurrently(fn, 4)
        self.assertEqual(1, seen.peak)


if __name__ == "__main__":
    unittest.main()
