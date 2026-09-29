"""本机常驻 HTTP 服务（streamable_http 形态）+ 本地状态页。

## 为什么要有这一半

stdio 形态下，本服务是宿主的**子进程**：宿主一重启就没了、宿主一关就跟着死，
而且「扫码登录」这件事没地方放（stdio 没有页面给用户看/操作）。
小红书 MCP（xpzouying/xiaohongshu-mcp）用的是另一种形态，也是本平台更成熟的那种：

| | stdio（本包默认） | streamable_http（本文件） |
| --- | --- | --- |
| 谁拉起 | 宿主当子进程拉起，`kill_on_drop` | 配置里给 `command`，客户端以 detached 方式常驻 |
| 端点 | stdin/stdout | `POST http://127.0.0.1:<port>/mcp` |
| 停用 | 宿主 kill 子进程 | 客户端按**进程台账/端口归属**收掉（`local_service.rs`） |
| 登录 | 工具（会话式）+ 终端里的浏览器窗口 | 同上，**外加一个本机状态页**：发起/取消扫码、看状态、重置登录态 |
| 鉴权 | 无需（父子进程） | 可选 `AUTH_TOKEN` → `Authorization: Bearer <token>` |

两种形态共用同一份工具实现与**同一个登录会话管理器**
（[`server.Server.sessions`]）—— 所以"页面看到的"和"工具看到的"永远是同一件事，
不会出现页面说在等扫码、工具说没有会话。

## 安全边界

- 默认**只绑 127.0.0.1**：这个服务持有抖音登录态、且能直接发布。
  部署者要改 `--host` 就**必须**同时给 token（见 [`serve_http`]）。
- 配了 token 时，`/mcp` 必须带 `Authorization: Bearer <token>`；页面路由要求同样的凭据
  （可用 `?token=` 传入，方便用户直接点开）。
- `/health` 永远免鉴权：启动器靠它判断「起来了没有」，它不含任何业务信息。
- 页面**只回显路径与状态，绝不读 cookie**；「重置登录态」是唯一的写操作，
  且两步确认（见 `sau.logout`）。
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import sau
from .douyin_cred import resolve_credential
from .login_session import LoginSessionManager
from .sau import SauConfig, SauError
from .server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, Server

# 页面/接口允许的账号名（与 sau 的凭据文件名规则同口径，这里先挡一道）
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class App:
    """一次进程内的全部共享状态：配置 / MCP 处理器（含登录会话）/ 页面提示 / 鉴权。

    ★ 「最近一次 check」与「当前登录会话」都放在 `self.mcp.sessions` 里 ——
      工具调用与页面共用，不要在这里再存一份（两份状态一定会分叉）。
    """

    def __init__(self, cfg: SauConfig, token: str = "", host: str = "127.0.0.1", port: int = 18080):
        self.cfg = cfg
        self.token = (token or "").strip()
        self.host = host
        self.port = port
        self.mcp = Server(cfg)
        self._lock = threading.Lock()
        self._notice = ""      # 页面顶部的一条提示（操作结果/错误原因）
        self._notice_err = False
        self._checking = False

    @property
    def sessions(self) -> LoginSessionManager:
        return self.mcp.sessions

    # ── 鉴权 ────────────────────────────────────────────────
    def auth_ok(self, path: str, header_auth: str, query: Dict[str, List[str]]) -> bool:
        if not self.token:
            return True
        if path == "/health":
            return True
        if (header_auth or "").strip() == "Bearer " + self.token:
            return True
        # 页面路由允许 ?token= 传入（用户直接在浏览器里打开这种场景）
        return bool(query.get("token")) and query["token"][0] == self.token

    def token_qs(self) -> str:
        return "?token=" + self.token if self.token else ""

    # ── 页面提示 ────────────────────────────────────────────
    def set_notice(self, text: str, is_error: bool = True) -> None:
        with self._lock:
            self._notice = text
            self._notice_err = is_error

    def notice(self) -> Tuple[str, bool]:
        with self._lock:
            return self._notice, self._notice_err

    def mark_checking(self, value: bool) -> None:
        with self._lock:
            self._checking = value

    def checking(self) -> bool:
        with self._lock:
            return self._checking

    # ── 动作（都委托给共享的会话管理器）─────────────────────
    def start_login(self, account: str, headed: bool = True) -> None:
        if not ACCOUNT_RE.match((account or "").strip()):
            self.set_notice("账号名只能是字母、数字、下划线、连字符（1~64 位）。")
            return
        try:
            snap = self.sessions.start_login(account, headed=headed)
        except SauError as e:
            self.set_notice(str(e))
            return
        self.set_notice(
            f"已发起账号「{account}」的登录，请在屏幕上弹出的浏览器窗口扫码。"
            f"（当前状态：{'等待扫码' if snap.get('running') else '已结束'}）",
            is_error=False,
        )

    def start_check(self, account: str) -> None:
        if not ACCOUNT_RE.match((account or "").strip()):
            self.set_notice("账号名只能是字母、数字、下划线、连字符（1~64 位）。")
            return
        if self.checking():
            self.set_notice("正在检查中，稍等再点。", is_error=False)
            return
        self.mark_checking(True)
        self.set_notice("正在检查登录态…", is_error=False)

        def worker() -> None:
            try:
                rec = self.sessions.check(account)
                self.set_notice(
                    f"账号「{account}」登录态：{_state_text(rec.logged_in)}",
                    is_error=rec.logged_in is False,
                )
            except Exception as e:  # noqa: BLE001 —— 页面不能因为一次检查失败就 500
                self.set_notice(f"检查失败：{e!r}")
            finally:
                self.mark_checking(False)

        threading.Thread(target=worker, name="sau-check", daemon=True).start()

    def do_logout(self, account: str, confirm: bool) -> None:
        if not ACCOUNT_RE.match((account or "").strip()):
            self.set_notice("账号名只能是字母、数字、下划线、连字符（1~64 位）。")
            return
        try:
            plan = sau.logout(self.cfg, account, confirm=confirm)
        except SauError as e:
            self.set_notice(str(e))
            return
        self.set_notice(plan.describe(), is_error=not plan.deleted and plan.existed)

    # ── 页面 ────────────────────────────────────────────────
    def page_html(self) -> str:
        cfg = self.cfg
        snap = self.sessions.snapshot()
        notice, notice_err = self.notice()
        qs = self.token_qs()
        account = html.escape(str(snap.get("account") or _default_account()))
        mcp_url = "http://%s:%d/mcp" % (self.host, self.port)
        auth_note = "已开启鉴权（需要 Authorization: Bearer <token>）" if self.token else "未开启鉴权（仅本机可访问）"
        rows = [
            ("MCP 端点", mcp_url),
            ("鉴权", auth_note),
            ("sau 可执行文件 (SAU_CMD)", cfg.cmd or "（未设置：走 SAU_DIR 或 PATH）"),
            ("项目目录 (SAU_DIR)", cfg.project_dir or "（未设置）"),
            ("素材目录 (SAU_MEDIA_DIR)", cfg.media_dir or "（未设置，发布会被拒绝）"),
            ("默认无头 (SAU_HEADLESS)", "是" if cfg.headless else "否"),
            ("单次超时 (SAU_TIMEOUT)", "%ds" % cfg.timeout),
            ("短信验证码文件", cfg.verify_code_file or "（未设置）"),
            ("账号凭据文件", _credential_hint(cfg, snap)),
            ("读取通道", _read_channel_hint(cfg, snap)),
        ]
        trs = "\n".join(
            "<tr><th>%s</th><td>%s</td></tr>" % (html.escape(k), html.escape(str(v)))
            for k, v in rows
        )

        if snap.get("running"):
            state_line = (
                '<b class="busy">等待扫码中…</b>（账号 %s，已等 %ss）'
                % (html.escape(str(snap.get("account"))), html.escape(str(snap.get("elapsedSec"))))
            )
        elif snap.get("checking") or self.checking():
            state_line = '<b class="busy">正在检查登录态…</b>'
        else:
            state_line = "登录态：<b class='%s'>%s</b>" % (
                "ok" if snap.get("loggedIn") else "",
                html.escape(_state_text(snap.get("loggedIn"))),
            )
        if snap.get("error"):
            state_line += '<div class="err">%s</div>' % html.escape(str(snap.get("error")))

        qr_block = ""
        if snap.get("qrAvailable"):
            qr_block = (
                '<p><img src="/qr.png%s" alt="登录二维码" style="max-width:260px"></p>'
                '<p class="hint">二维码文件：<code>%s</code></p>'
                % (qs, html.escape(str(snap.get("qrPath") or "")))
            )
        elif snap.get("running"):
            qr_block = (
                '<p class="hint">这个平台/版本没把二维码落盘 —— 二维码在用户屏幕上弹出的浏览器窗口里，'
                "让他直接看那个窗口扫码即可。</p>"
            )

        notice_block = (
            '<p class="%s">%s</p>' % ("err" if notice_err else "ok", html.escape(notice))
            if notice
            else ""
        )
        last_check = snap.get("lastCheck") or {}
        output_text = last_check.get("output") or snap.get("output") or "(暂无)"
        return _PAGE_TEMPLATE.format(
            server=SERVER_NAME,
            version=SERVER_VERSION,
            rows=trs,
            qs=qs,
            account=account,
            state_line=state_line,
            notice=notice_block,
            qr_block=qr_block,
            output=html.escape(str(output_text)),
            refresh_ms=1500 if (snap.get("running") or self.checking()) else 3000,
        )


# 状态页模板（★ 用 .format 而不是 f-string：页面里全是 CSS 花括号，
#   f-string 会把它们当占位符，逐个转义读起来一塌糊涂）
_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{server} · 本机状态</title>
<style>
 body{{font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:24px;max-width:860px;color:#222}}
 h1{{font-size:18px}} h2{{font-size:15px;margin-top:24px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border:1px solid #e5e5e5;padding:6px 8px;text-align:left;vertical-align:top}}
 th{{width:240px;background:#fafafa;font-weight:600}}
 .hint{{color:#888}} .err{{color:#c0392b;white-space:pre-wrap}} .busy{{color:#b26a00}}
 .ok{{color:#2e7d32}} pre{{background:#f7f7f7;padding:10px;overflow:auto;max-height:320px}}
 form{{margin:10px 0}} input{{padding:6px;width:220px}} button{{padding:6px 12px;margin-left:6px}}
 button.danger{{border-color:#c0392b;color:#c0392b}}
</style></head><body>
<h1>{server} v{version} · 本机状态页</h1>
<p class="hint">这一页只服务本机用户：看配置、发起/取消扫码登录、重置登录态、看最近一次 CLI 输出。
工具调用走 MCP 端点。</p>
<table>{rows}</table>
<h2>登录</h2>
<p>{state_line}</p>
{notice}
<form method="post" action="/login{qs}">
  <input name="account" value="{account}" placeholder="账号名（自定义，如 main）" required>
  <button type="submit">扫码登录</button>
</form>
<form method="post" action="/check{qs}">
  <input name="account" value="{account}" placeholder="账号名" required>
  <button type="submit">检查登录态</button>
</form>
<form method="post" action="/cancel-login{qs}">
  <button type="submit">取消等待扫码</button>
</form>
{qr_block}
<h2>重置登录态</h2>
<p class="hint">「重置」= 删除该账号的凭据文件（&lt;项目&gt;/cookies/douyin_&lt;账号&gt;.json）。
分两步：先看要删什么，再确认删除。</p>
<form method="post" action="/logout{qs}">
  <input type="hidden" name="confirm" value="0">
  <input name="account" value="{account}" placeholder="账号名" required>
  <button type="submit">查看将删除的文件</button>
</form>
<form method="post" action="/logout{qs}">
  <input type="hidden" name="confirm" value="1">
  <input name="account" value="{account}" placeholder="账号名" required>
  <button class="danger" type="submit">确认删除凭据（退出登录）</button>
</form>
<h2>最近一次输出</h2>
<pre>{output}</pre>
<p class="hint">本页自动刷新（有任务在跑时更快）。</p>
<script>setTimeout(function(){{location.reload()}}, {refresh_ms});</script>
</body></html>"""


def _state_text(state: Optional[bool]) -> str:
    if state is True:
        return "已登录"
    if state is False:
        return "未登录（需要扫码）"
    return "未检查 / 判不出来"


def _credential_hint(cfg: SauConfig, snap: Dict[str, Any]) -> str:
    account = str(snap.get("account") or _default_account())
    if not cfg.project_dir:
        return "（未设置 SAU_DIR，定位不到）"
    try:
        return str(sau.account_file_path(cfg, account))
    except SauError:
        return "（账号名不合规，无法定位）"


def _default_account() -> str:
    """页面默认账号名：只读一个显式配置，**不猜**。"""
    import os

    return (os.environ.get("SAU_ACCOUNT") or "main").strip()


def _read_channel_hint(cfg: SauConfig, snap: Dict[str, Any]) -> str:
    """读取通道现状：给用户看"能不能读、凭什么读"。

    ★ 只回显来源与 cookie **名**（[`Credential.describe`] 的约定），
      状态页永远不显示 cookie 的值 —— 这一页是给人看的，不是给人抄凭据的。
    """
    account = str(snap.get("account") or _default_account())
    try:
        cred = resolve_credential(cfg, account)
    except SauError as e:
        return "不可用：%s" % e
    if not cred.usable:
        return "不可用（没有凭据）—— 读取工具会提示先扫码登录"
    return cred.describe().replace("\n", "；")


def make_handler(app: App) -> Callable[..., BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = SERVER_NAME + "/" + SERVER_VERSION

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            # 访问日志走 stderr：stdout 在 stdio 形态下是协议通道，习惯保持一致
            sys.stderr.write("[%s] %s %s\n" % (SERVER_NAME, self.address_string(), fmt % args))
            sys.stderr.flush()

        # ── 基础工具 ────────────────────────────────────────
        def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, status: int = 200, extra: Optional[Dict[str, str]] = None) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length > 0 else b""

        def _parsed(self) -> Tuple[str, Dict[str, List[str]]]:
            u = urlparse(self.path)
            return u.path, parse_qs(u.query)

        def _unauthorized(self) -> None:
            body = json.dumps(
                {"error": "unauthorized", "hint": "需要 Authorization: Bearer <token>"},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(401, body, "application/json; charset=utf-8", {"WWW-Authenticate": "Bearer"})

        def _guarded(self) -> Optional[str]:
            """鉴权通过返回 path，否则回 401 并返回 None"""
            path, query = self._parsed()
            if app.auth_ok(path, self.headers.get("Authorization", ""), query):
                return path
            self._unauthorized()
            return None

        def _redirect_home(self) -> None:
            self.send_response(303)
            self.send_header("Location", "/" + app.token_qs())
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _snapshot(self) -> Dict[str, Any]:
            snap = app.sessions.snapshot()
            snap["checking"] = app.checking()
            snap["notice"], snap["noticeIsError"] = app.notice()
            snap["mcpEndpoint"] = "http://%s:%d/mcp" % (app.host, app.port)
            return snap

        # ── 路由 ────────────────────────────────────────────
        def do_GET(self) -> None:  # noqa: N802
            path = self._guarded()
            if path is None:
                return
            if path == "/health":
                return self._json({"status": "ok", "service": SERVER_NAME, "version": SERVER_VERSION})
            if path == "/state.json":
                return self._json(self._snapshot())
            if path == "/qr.png":
                qr_path = str(app.sessions.snapshot().get("qrPath") or "")
                if not qr_path or not Path(qr_path).is_file():
                    return self._send(404, b"no qrcode", "text/plain; charset=utf-8")
                try:
                    data = Path(qr_path).read_bytes()
                except OSError:
                    return self._send(404, b"no qrcode", "text/plain; charset=utf-8")
                mime = "image/png" if Path(qr_path).suffix.lower() == ".png" else "image/jpeg"
                return self._send(200, data, mime, {"Cache-Control": "no-store"})
            if path in ("/", "/index.html"):
                return self._send(200, app.page_html().encode("utf-8"), "text/html; charset=utf-8")
            if path == "/mcp":
                # 本实现不提供 server→client 的 SSE 流：明确回 405 比挂一个空流诚实
                return self._send(405, b"GET not supported on /mcp", "text/plain; charset=utf-8",
                                  {"Allow": "POST, DELETE"})
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_DELETE(self) -> None:  # noqa: N802
            # 与 do_POST 同理：先把请求体读掉，再判鉴权（避免长连接错位）
            path, query = self._parsed()
            self._read_body()
            if not app.auth_ok(path, self.headers.get("Authorization", ""), query):
                return self._unauthorized()
            if path != "/mcp":
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            # 会话结束：本实现无状态，直接兜住（客户端不会因此断连）
            return self._send(200, b'{"ok":true}', "application/json; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            # ★ 先读干净请求体，再判鉴权：HTTP/1.1 是长连接，401 时若不把 body 读掉，
            #   残留字节会被当成下一个请求的请求行（日志会出现"Bad request version"，
            #   客户端那条连接也彻底错位）。这是实测踩到过的一个坑。
            path, query = self._parsed()
            raw = self._read_body()
            if not app.auth_ok(path, self.headers.get("Authorization", ""), query):
                return self._unauthorized()

            if path in ("/login", "/check", "/logout", "/cancel-login"):
                # ★ 用已经读到的 raw（请求体只能读一次：第二次读只会拿到空字节）
                form = parse_qs(raw.decode("utf-8", errors="replace"))
                account = (form.get("account") or [""])[0]
                if path == "/login":
                    app.start_login(account, headed=True)
                elif path == "/check":
                    app.start_check(account)
                elif path == "/logout":
                    confirm = (form.get("confirm") or ["0"])[0] in ("1", "true", "on")
                    app.do_logout(account, confirm=confirm)
                else:
                    app.sessions.cancel_login()
                    app.set_notice("已取消等待扫码的会话。", is_error=False)
                # 303：提交后回到状态页，刷新时不会重复提交
                return self._redirect_home()

            if path != "/mcp":
                return self._send(404, b"not found", "text/plain; charset=utf-8")

            try:
                request = json.loads(raw.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "请求体不是合法 JSON"}},
                    status=400,
                )
            if not isinstance(request, dict):
                return self._json(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "请求必须是单个 JSON 对象"}},
                    status=400,
                )

            headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
            if request.get("method") == "initialize":
                # 会话 ID：客户端会带着它回来（服务端无状态，但按规范必须给）
                headers["mcp-session-id"] = _session_id(self.headers.get("mcp-session-id"))

            response = app.mcp.handle(request)
            if response is None:
                # 通知：按 MCP 规范回 202 且无正文
                self.send_response(202)
                self.send_header("Content-Length", "0")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            return self._json(response, extra=headers)

    return Handler


def _session_id(existing: Optional[str]) -> str:
    if existing and re.match(r"^[A-Za-z0-9_-]{1,128}$", existing):
        return existing
    return "%s-%d" % (SERVER_NAME, int(time.time() * 1000))


def serve_http(cfg: SauConfig, host: str = "127.0.0.1", port: int = 18080, token: str = "") -> None:
    """起 HTTP 服务（阻塞；供 `python -m douyin_publish_mcp --http` 调用）。

    ★ 绑定非环回地址时**强制**要有 token：这个服务能直接发布内容、持有登录态，
      裸奔在局域网上等于把账号交出去。
    """
    if host not in _LOOPBACK_HOSTS and not (token or "").strip():
        raise SystemExit(
            "拒绝在 %s 上无鉴权启动：请加 --token（或设 AUTH_TOKEN 环境变量），"
            "否则局域网上任何人都能用这个服务发布内容。" % host
        )
    app = App(cfg, token=token, host=host, port=port)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    sys.stderr.write(
        "[%s] HTTP 服务已启动：http://%s:%d/mcp（状态页 http://%s:%d/%s；鉴权=%s）\n"
        % (SERVER_NAME, host, app.port, host, app.port, app.token_qs(), "开" if app.token else "关")
    )
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


__all__ = ["serve_http", "App", "make_handler"]
