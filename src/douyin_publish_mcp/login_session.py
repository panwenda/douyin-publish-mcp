"""登录会话：一个「等扫码」的自动化进程 + 串行化的一次性检查。

## 为什么要有它（约束照小红书 MCP 的 login_session.go 抄）

扫码登录要等用户拿手机扫（几十秒到几分钟）。三个后果：

1. 当成同步调用会**卡住客户端**（stdio 下连"还在等"都没法回报）；
2. 每点一次就多一个浏览器活到超时为止 —— 所以必须有「同一时刻只保留一个待扫码会话」
   的约束，开新的就把旧的关掉；
3. 「现在到哪一步了」得有地方回答：会话状态、二维码、扫码到底成没成。

## 与 v0.2 的差别

等的那个进程从**外部 CLI** 换成了我们自己的 `--creator-helper`（见 `creator.login`）：
同一套按需驱动、同一套宿主解析，只是不再要求用户先装一个 social-auto-upload。
进度走 stderr、结论走 stdout 的最后一行 JSON —— 与浏览器通道的 helper 协议一致。

## 检查与登录的关系

登录成功后**自动**跑一次账号检查（直连探针，见 `account.check_account`）：
"扫码流程走完了"不等于"扫上了"，用户真正关心的是后者。结论留在快照里，
工具调用与状态页读到的是同一份 —— 不会出现"页面说成功、工具说没登录"。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import douyin_browser as browser
from .account import AccountCheck, check_account
from .config import RuntimeConfig, credential_path, data_dir

_OUTPUT_LIMIT = 8000      # 快照里保留的输出长度（够看结论即可）
_DEFAULT_MAX_WAIT = 240   # 等扫码的上限：再久用户基本已经走开了
#: 检查记录就是账号检查的结论（字段同形，直接复用，避免两种"差不多"的类型各自漂移）
CheckRecord = AccountCheck


def qr_path_for(account: str) -> Path:
    """本次登录二维码的落点（helper 会往里写 PNG，工具读它回给客户端）。"""
    safe = (account or "main").strip() or "main"
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in safe)
    return data_dir() / ("douyin_%s_login_qrcode.png" % safe)


def run_check(cfg: RuntimeConfig, account: str) -> AccountCheck:
    """跑一次登录态检查（直连探针，通常亚秒）。"""
    return check_account(cfg, account)


class LoginSession:
    """一次待扫码登录：helper 进程 + 读取线程 + 结论

    ★ stdout/stderr **必须**有人读：管道缓冲区填满后子进程会卡在半路，
      表现是"浏览器出来了但一直没反应"。
    """

    def __init__(
        self,
        cfg: RuntimeConfig,
        account: str,
        # ★ 默认有头（真窗口）：登录这一步抖音会挑无头浏览器，且可能要输短信验证码。
        #   窗口不会久留 —— 扫码成功后存完凭据就关掉，一直没人扫也最多等 max_wait 秒。
        #   headed=false 留给"能拿到二维码图片、但机器上没有桌面会话"的场景。
        headed: bool = True,
        on_finish: Optional[Callable[["LoginSession"], None]] = None,
        max_wait_sec: float = _DEFAULT_MAX_WAIT,
    ):
        self.cfg = cfg
        self.account = account
        self.headed = headed
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.error = ""
        self.result: Dict[str, Any] = {}
        self.credential_path = credential_path(account)
        self.qr_path = qr_path_for(account)
        self._query_done = False
        self._lines: List[str] = []
        self._stdio: List[str] = []
        self._lock = threading.Lock()
        self._on_finish = on_finish

        spec = {
            "action": "login",
            "account": account,
            "credential_path": str(self.credential_path),
            "qr_path": str(self.qr_path),
            "headed": bool(headed),
            "channel": cfg.channel,
            "max_wait_sec": float(max_wait_sec),
            "poll_sec": 2,
        }
        self.host = browser.resolve_host(browser.BrowserConfig.from_env(), cfg,
                                        subcommand="--creator-helper")
        self.run = browser.spawn_helper(self.host, spec)
        self.proc = self.run.proc

        # ★ 「结束」这个状态要等收尾（含自动检查）**做完**才算：否则调用方
        #   `wait()` 一返回就去读 loggedIn，会读到还没写的旧值 —— 表现为
        #   "明明扫上了，工具却说判不出来"。
        self._done = threading.Event()
        if self.proc.stdout is not None:
            threading.Thread(target=self._pump, args=(self.proc.stdout, True), daemon=True).start()
        if self.proc.stderr is not None:
            threading.Thread(target=self._pump, args=(self.proc.stderr, False), daemon=True).start()
        self._waiter = threading.Thread(target=self._watch, daemon=True)
        self._waiter.start()

    # ── 内部 ────────────────────────────────────────────────
    def _pump(self, stream, is_stdout: bool) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip() if isinstance(raw, bytes) else str(raw).rstrip()
                if not line:
                    continue
                with self._lock:
                    self._lines.append(line)
                    if is_stdout:
                        self._stdio.append(line)
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
        with self._lock:
            stdout = "\n".join(self._stdio)
        payload = browser._last_json_line(stdout) or {}
        self.result = payload
        self.exit_code = 0 if payload.get("ok") else (code if code not in (None, 0) else 1)
        self.finished_at = time.time()
        self.run.cleanup()
        try:
            if payload and not payload.get("ok"):
                err = payload.get("error") or {}
                self.error = "%s：%s" % (err.get("kind", "failed"), err.get("message", "登录失败"))
            elif not payload and not self.error:
                tail = self.output(limit=600).strip().splitlines()
                self.error = tail[-1] if tail else "登录进程没有给出结论（退出码 %s）。" % code
            if self._on_finish is not None:
                try:
                    self._on_finish(self)
                except Exception as e:  # noqa: BLE001 —— 回调失败不能影响会话状态
                    self.error = self.error or "登录结束后的收尾失败：%r" % (e,)
        finally:
            self._cleanup_qrcode()
            # ★ 最后才置「结束」：见 __init__ 里对 _done 的说明
            self._done.set()

    def _cleanup_qrcode(self) -> None:
        """二维码是一次性登录物料，会话结束就删掉（留着没有意义，还容易被误传）。"""
        try:
            if self.qr_path.is_file():
                self.qr_path.unlink()
        except OSError:
            pass

    # ── 快照 ────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return not self._done.is_set()

    def output(self, limit: int = _OUTPUT_LIMIT) -> str:
        with self._lock:
            text = "\n".join(self._lines)
        from .douyin_cred import redact

        return redact(text)[-limit:]

    def qr(self) -> Optional[Path]:
        """当前会话的二维码文件（helper 落盘了才有；还没等到就是 None）。"""
        if self.running and self.qr_path.is_file():
            return self.qr_path
        return None

    def snapshot(self) -> Dict[str, Any]:
        qr = self.qr()
        payload = self.result or {}
        return {
            "running": self.running,
            "account": self.account,
            "headed": self.headed,
            "startedAt": self.started_at,
            "elapsedSec": round((self.finished_at or time.time()) - self.started_at, 1),
            "exitCode": self.exit_code,
            "finished": not self.running,
            "succeeded": bool(payload.get("ok")),
            "state": str(payload.get("state") or ("running" if self.running else "")),
            "error": self.error,
            "qrAvailable": bool(qr),
            "qrPath": str(qr) if qr else "",
            "credentialPath": str(self.credential_path),
            "output": self.output(),
        }

    def cancel(self) -> None:
        """关掉这个待扫码会话（已经结束就什么都不做）"""
        if not self.running:
            return
        self.error = self.error or "会话已被新的登录请求或用户操作关闭。"
        self.run.terminate()
        try:
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    def wait(self, seconds: float) -> Dict[str, Any]:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline and self.running:
            time.sleep(0.2)
        return self.snapshot()


class LoginSessionManager:
    """进程内共享的登录会话 + 最近一次检查结论。

    一个进程一个实例：stdio 的 MCP 服务与 HTTP 状态页用的是**同一份状态**，
    所以从页面扫码、从工具查询看到的是同一件事。
    """

    def __init__(self, cfg: RuntimeConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._session: Optional[LoginSession] = None
        self._check: Optional[CheckRecord] = None

    # ── check ───────────────────────────────────────────────
    def check(self, account: str) -> CheckRecord:
        """跑一次登录态检查。

        ★ 正在等扫码时不查：此刻用户该做的事是去扫码，不是读一份"上次的结论"。
          直接回一条 `skipped` 记录，由调用方换成"等扫码结束再查"的说法。
        """
        session = self.session()
        if session is not None and session.running:
            return CheckRecord(
                account=account,
                logged_in=None,
                ok=False,
                output=(
                    "账号「%s」的登录会话正在等扫码 —— 此刻不做登录态检查"
                    "（扫码完成后会自动检查一次）。" % session.account
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
    def start_login(self, account: str, headed: bool = True,
                    max_wait_sec: float = _DEFAULT_MAX_WAIT) -> Dict[str, Any]:
        account = (account or "").strip()
        # ★ 同一时刻只保留一个待扫码会话：开新的先把旧的关掉
        #   （否则每调一次就多一个浏览器，活到各自超时为止）
        self.cancel_login(quiet=True)
        session = LoginSession(self.cfg, account, headed=headed, on_finish=self._after_login,
                              max_wait_sec=max_wait_sec)
        with self._lock:
            self._session = session
        # ★ 返回**合并后的**快照（与 snapshot() 同形）：否则调用方拿到的是少几个键的
        #   另一种形状，取 loggedIn 就会 KeyError / 拿到旧值。
        return self.snapshot()

    def _after_login(self, session: LoginSession) -> None:
        """登录流程跑完后补一次检查：进程正常退出 ≠ 扫上了。"""
        if (session.result or {}).get("ok"):
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
            "state": "",
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


__all__ = ["CheckRecord", "LoginSession", "LoginSessionManager", "qr_path_for", "run_check"]
