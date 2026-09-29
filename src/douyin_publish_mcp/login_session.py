"""登录会话：一个「等扫码」的 `sau douyin login` 进程 + 串行化的一次性 check。

## 为什么要有它（约束照小红书 MCP 的 login_session.go 抄）

`sau douyin login` 会一直等到用户扫完码（几十秒到几分钟）。三个后果：

1. 当成同步调用会**卡住客户端**（stdio 下连"还在等"都没法回报）；
2. 每点一次就多一个浏览器活到超时为止 —— 所以必须有「同一时刻只保留一个待扫码会话」
   的约束，开新的就把旧的关掉；
3. 「现在到哪一步了」得有地方回答：会话状态、最近输出、扫码到底成没成。

## 与 check 的关系

登录结束后**自动**跑一次 `sau douyin check`：`login` 退出码 0 只代表"登录流程走完了"，
不代表"扫上了"。用户真正关心的是后者，所以这里顺手确认一次，并把结果留在快照里，
工具调用与状态页都能读到同一份结论。

## 串行化

`sau` 一次只跑一个浏览器实例：并发跑会互相抢 cookies 与浏览器资料目录。
所有 CLI 调用（check / login）都过同一个 [`CLI_LOCK`]，
因此"检查登录态"不会被正在等待扫码的登录挤坏。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import sau
from .sau import SauConfig

# 所有 sau CLI 调用共用：一次只跑一个浏览器实例
CLI_LOCK = threading.Lock()

_OUTPUT_LIMIT = 8000      # 快照里保留的输出长度（够看结论即可）
_CHECK_TIMEOUT_CAP = 300  # check 是"快"命令：卡住 5 分钟一定有问题


@dataclass
class CheckRecord:
    """一次 `sau douyin check` 的结论（页面与工具共用）"""

    account: str
    logged_in: Optional[bool]
    ok: bool
    output: str
    at: float = field(default_factory=time.time)
    # 会话正在等扫码时会**跳过**检查（见 [`LoginSessionManager.check`]），
    # 这不是失败，调用方要区别对待：别把"没查"说成"没登录"。
    skipped: bool = False


def run_check(cfg: SauConfig, account: str) -> CheckRecord:
    """跑一次登录态检查（阻塞，通常几秒）。"""
    # ★ 过 CLI_LOCK：sau 一次只跑一个浏览器实例，并发跑会互相抢 cookies 与浏览器资料
    with CLI_LOCK:
        result = sau.run(cfg, sau.check_args(account), timeout=min(cfg.timeout, _CHECK_TIMEOUT_CAP))
    text = sau.redact(result.tail())
    if result.hint and result.hint not in text:
        text = (text + "\n" + result.hint).strip()
    return CheckRecord(
        account=account,
        logged_in=sau.parse_login_state(result),
        ok=result.ok,
        output=text,
    )


class LoginSession:
    """一次待扫码登录：进程 + 读取线程 + 结论

    ★ stdout/stderr **必须**有人读：管道缓冲区填满后子进程会卡在半路，
      表现是"浏览器出来了但一直没反应"。
    """

    def __init__(
        self,
        cfg: SauConfig,
        account: str,
        headed: bool = True,
        on_finish: Optional[Callable[["LoginSession"], None]] = None,
    ):
        self.cfg = cfg
        self.account = account
        self.headed = headed
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.error = ""
        self.qr_path: str = ""
        self._query_done = False
        self._lines: List[str] = []
        self._lock = threading.Lock()
        self._on_finish = on_finish

        args = sau.login_args(account, headed=headed)
        self.argv = cfg.command(args)
        # ★ 「结束」这个状态要等收尾（含自动 check）**做完**才算：否则调用方
        #   `wait()` 一返回就去读 loggedIn，会读到还没写的旧值 —— 表现为
        #   "明明扫上了，工具却说判不出来"。
        self._done = threading.Event()
        self.proc = sau.spawn(cfg, args)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                threading.Thread(target=self._pump, args=(stream,), daemon=True).start()
        self._waiter = threading.Thread(target=self._watch, daemon=True)
        self._waiter.start()

    # ── 内部 ────────────────────────────────────────────────
    def _pump(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                with self._lock:
                    self._lines.append(line)
                    if len(self._lines) > 400:
                        del self._lines[:100]
        except Exception:  # noqa: BLE001 —— 读流失败不该把后台线程带崩
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _watch(self) -> None:
        try:
            code = self.proc.wait()
        except Exception as e:  # noqa: BLE001
            code = None
            self.error = "等待登录进程失败：%r" % (e,)
        self.exit_code = code
        self.finished_at = time.time()
        try:
            if code not in (0, None) and not self.error:
                # 失败时把最后几行输出当原因（CLI 失败会抛 RuntimeError → 栈在 stderr）
                tail = self.output(limit=600).strip().splitlines()
                self.error = tail[-1] if tail else "登录流程失败（退出码 %s），但没有输出。" % code
            if self._on_finish is not None:
                try:
                    self._on_finish(self)
                except Exception as e:  # noqa: BLE001 —— 回调失败不能影响会话状态
                    self.error = self.error or "登录结束后的收尾失败：%r" % (e,)
        finally:
            # ★ 最后才置「结束」：见 __init__ 里对 _done 的说明
            self._done.set()

    # ── 快照 ────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def output(self, limit: int = _OUTPUT_LIMIT) -> str:
        with self._lock:
            text = "\n".join(self._lines)
        return sau.redact(text)[-limit:]

    def qr(self) -> Optional[Path]:
        """当前会话的二维码文件（CLI 落盘了才有；抖音这条链路通常没有）。"""
        if self.qr_path and Path(self.qr_path).is_file():
            return Path(self.qr_path)
        found = sau.parse_qrcode_path(
            self.output(), extra_roots=[self.cfg.project_dir, self.cfg.media_dir]
        )
        if found:
            self.qr_path = str(found)
        return found

    def snapshot(self) -> Dict[str, Any]:
        qr = self.qr()
        return {
            "running": self.running,
            "account": self.account,
            "headed": self.headed,
            "startedAt": self.started_at,
            "elapsedSec": round((self.finished_at or time.time()) - self.started_at, 1),
            "exitCode": self.exit_code,
            "finished": not self.running,
            "succeeded": self.exit_code == 0,
            "error": self.error,
            "qrAvailable": bool(qr),
            "qrPath": str(qr) if qr else "",
            "output": self.output(),
        }

    def cancel(self) -> None:
        """关掉这个待扫码会话（已经结束就什么都不做）"""
        if not self.running:
            return
        self.error = self.error or "会话已被新的登录请求或用户操作关闭。"
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001 —— 宽限期内没退就强杀
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def wait(self, seconds: float) -> Dict[str, Any]:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline and self.running:
            time.sleep(0.2)
        return self.snapshot()


class LoginSessionManager:
    """进程内共享的登录会话 + 最近一次 check 结论。

    一个进程一个实例：stdio 的 MCP 服务与 HTTP 状态页用的是**同一份状态**，
    所以从页面扫码、从工具查询看到的是同一件事。
    """

    def __init__(self, cfg: SauConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._session: Optional[LoginSession] = None
        self._check: Optional[CheckRecord] = None

    # ── check ───────────────────────────────────────────────
    def check(self, account: str) -> CheckRecord:
        """跑一次登录态检查。

        ★ 正在等扫码时不查：那时 `sau` 已经有一个浏览器活着，再起一个去查会：
          ① 白开一个浏览器（慢且抢资源）② 让用户以为"检查了、结果是没登录"。
          所以直接回一条 `skipped` 记录，由调用方换成"等扫码结束再查"的说法。
        """
        session = self.session()
        if session is not None and session.running:
            return CheckRecord(
                account=account,
                logged_in=None,
                ok=False,
                output=(
                    "账号「%s」的登录会话正在等扫码 —— 此刻不做登录态检查"
                    "（扫描完成后会自动检查一次）。" % session.account
                ),
                skipped=True,
            )
        rec = run_check(self.cfg, account)
        with self._lock:
            self._check = rec
        return rec

    def last_check(self) -> Optional[CheckRecord]:
        with self._lock:
            return self._check

    # ── login ───────────────────────────────────────────────
    def start_login(self, account: str, headed: bool = True) -> Dict[str, Any]:
        account = (account or "").strip()
        # ★ 同一时刻只保留一个待扫码会话：开新的先把旧的关掉
        #   （否则每调一次就多一个浏览器，活到各自超时为止）
        self.cancel_login(quiet=True)
        session = LoginSession(self.cfg, account, headed=headed, on_finish=self._after_login)
        with self._lock:
            self._session = session
        # ★ 返回**合并后的**快照（与 snapshot() 同形）：否则调用方拿到的是少几个键的
        #   另一种形状，取 loggedIn 就会 KeyError / 拿到旧值。
        return self.snapshot()

    def _after_login(self, session: LoginSession) -> None:
        """登录流程跑完后补一次 check：退出码 0 ≠ 扫上了。"""
        if session.exit_code == 0:
            rec = run_check(self.cfg, session.account)
            with self._lock:
                self._check = rec

    def session(self) -> Optional[LoginSession]:
        with self._lock:
            return self._session

    def snapshot(self) -> Dict[str, Any]:
        session = self.session()
        rec = self.last_check()
        base: Dict[str, Any] = {
            "running": False,
            "account": "",
            "headed": True,
            "startedAt": 0.0,
            "elapsedSec": 0.0,
            "exitCode": None,
            "finished": True,
            "succeeded": False,
            "error": "",
            "qrAvailable": False,
            "qrPath": "",
            "output": "",
        }
        if session is not None:
            base.update(session.snapshot())
        if rec is not None:
            base["loggedIn"] = rec.logged_in
            base["lastCheck"] = {
                "account": rec.account,
                "loggedIn": rec.logged_in,
                "ok": rec.ok,
                "at": rec.at,
                "output": rec.output,
            }
        else:
            base["loggedIn"] = None
        return base

    def wait(self, seconds: float) -> Dict[str, Any]:
        session = self.session()
        if session is None:
            return self.snapshot()
        session.wait(seconds)
        return self.snapshot()

    def cancel_login(self, quiet: bool = False) -> Dict[str, Any]:
        session = self.session()
        if session is not None:
            session.cancel()
        return self.snapshot()


__all__ = ["CLI_LOCK", "CheckRecord", "LoginSession", "LoginSessionManager", "run_check"]
