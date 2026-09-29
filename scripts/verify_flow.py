"""端到端验收：先跑**本地就能验**的部分，再看要不要碰真机。

## 为什么要分成"本地"与"真机"两段

这个服务的失败方式分两类，验证成本差一个数量级：

| | 本地段（不需要 sau / 不联网） | 真机段（要 sau + 登录态 + 外网） |
| --- | --- | --- |
| 协议层 | ✅ initialize / tools/list / schema | |
| 发布门禁 | ✅ 预检 + 两条拦截（**一行 CLI 都不跑**） | |
| HTTP 形态 | ✅ 真起服务、真发请求、真验鉴权 | |
| 登录态 | | ✅ `sau douyin check` |
| 读取五连 | | ✅ 搜索 / 详情 / 主页 / 分享（会真调抖音接口，并标明走的哪条通道） |
| 浏览器通道 | | ✅ 详情 + 评论（**单独验**：它平时是"被回退才出场"，只靠工具 PASS 会漏掉它坏了） |

本地段全绿只说明"这个服务的壳是对的"，**不说明抖音认它**。所以脚本最后会把
"还没验过的"单独列出来 —— SKIP 从来不算通过。

## 用法

```powershell
cd E:\\项目\\AI\\douyin-publish-mcp
$env:PYTHONPATH="src"
& ..\\.venv\\Scripts\\python.exe scripts\\verify_flow.py --local-only      # 只跑本地段
& ..\\.venv\\Scripts\\python.exe scripts\\verify_flow.py                   # 本地段 + 真机段
& ..\\.venv\\Scripts\\python.exe scripts\\verify_flow.py --keyword 猫 --publish-file D:\\media\\a.mp4
```

- **默认不发布任何东西**：发布只跑到"预检 + 门禁必须拦住"这一步；
  真发必须显式 `--confirm-publish`（且要求 `--publish-file` 给的是真素材）。
- 退出码：`0` = 没有 FAIL（可以带 SKIP）；`1` = 有 FAIL。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from douyin_publish_mcp.douyin_cred import default_account, resolve_credential  # noqa: E402
from douyin_publish_mcp.douyin_web import DouyinWebClient, DouyinWebError, WebConfig  # noqa: E402
from douyin_publish_mcp.http_server import App, make_handler  # noqa: E402
from douyin_publish_mcp.sau import SauConfig  # noqa: E402
from douyin_publish_mcp.server import TOOLS, Server  # noqa: E402

TESTS_DIR = ROOT / "tests"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
RESULTS = []  # (状态, 环节, 说明)


# ── 输出 ────────────────────────────────────────────────────


def _console_safe(text: str) -> str:
    """把控制台编码塞不下的字符换成 `?`。

    ★ 实测踩到：作品标题里带 emoji（🏠）时，Windows 控制台是 GBK，
      `print` 直接抛 UnicodeEncodeError —— 一条验收结果没打出来，脚本先崩了。
      验收脚本的职责是"如实报告"，不该被被测数据里的一个字符放倒。
    """
    enc = getattr(sys.stdout, "encoding", "") or "utf-8"
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, errors="replace").decode(enc, errors="replace")


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))
    mark = {PASS: "[PASS]", FAIL: "[FAIL]", SKIP: "[SKIP]"}[status]
    line = f"{mark} {name}"
    if detail:
        line += f"\n       {detail.replace(chr(10), chr(10) + '       ')}"
    print(_console_safe(line), flush=True)


def banner(text: str) -> None:
    print("\n" + "=" * 4 + " " + text + " " + "=" * 4, flush=True)


# ── 工具调用（与客户端同一条协议路径）────────────────────────


def call(server: Server, name: str, args: dict, req_id: int = 1):
    """调一次工具，返回 (result, 协议错误)。"""
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    if resp is None:
        return None, "服务端没有返回（通知？）"
    if "error" in resp:
        return None, f"协议层错误：{resp['error']}"
    return resp.get("result") or {}, ""


def text_of(result: dict) -> str:
    return "\n".join(str(c.get("text", "")) for c in (result or {}).get("content", []))


def is_error(result: dict) -> bool:
    return bool((result or {}).get("isError"))


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text or "")
    return m.group(1) if m else ""


def _channel_of(text: str) -> str:
    """从工具输出里读出"这次走的是哪条通道"。

    ★ 读取工具在回退过浏览器时会附一句「本次通道：…」。验收要能区分
      "直连干的"和"浏览器救回来的" —— 否则平台哪天把直连放开/收紧，
      我们看 PASS 也看不出发生了什么。
    """
    m = re.search(r"本次通道：([^）\n]+)", text or "")
    if m:
        return "通道=" + m.group(1).strip()
    m = re.search(r"通道：([^\n]+)", text or "")   # 主页那两件事合并输出时的形态
    return "通道=" + m.group(1).strip() if m else ""


# ── 本地段 ──────────────────────────────────────────────────
# ★ 本地段的共同点：**不联网、不跑 sau、不碰账号**。
#   所以它们在你还没装 social-auto-upload 的机器上也能给出结论。


def step_unit_tests() -> None:
    """把单测跑一遍（全部打桩）。这是"改完有没有碰坏别的东西"的第一道闸。"""
    if not TESTS_DIR.is_dir():
        record(SKIP, "单测", f"找不到 {TESTS_DIR}")
        return
    suite = unittest.TestLoader().discover(str(TESTS_DIR), pattern="test_*.py")
    buf = io.StringIO()
    # ★ 连 stderr 一起收走：HTTP 用例的服务访问日志走 stderr（正常行为），
    #   直接漏到屏幕上会把验收结果淹掉 —— 这里只留汇总。
    real_stderr = sys.stderr
    sys.stderr = buf
    try:
        result = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)
    finally:
        sys.stderr = real_stderr
    if result.wasSuccessful():
        record(PASS, "单测", f"{result.testsRun} 条全部通过（全打桩，不联网）")
        return
    bad = [str(c[0]) for c in (list(result.failures) + list(result.errors))][:5]
    record(
        FAIL,
        "单测",
        f"{len(result.failures)} 失败 / {len(result.errors)} 错误（共 {result.testsRun} 条）\n"
        + "\n".join("· " + b for b in bad),
    )


def step_protocol() -> None:
    """协议层：客户端第一步就会打的这两个请求。"""
    server = Server(SauConfig())
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    info = (resp or {}).get("result", {}).get("serverInfo", {})
    if not info.get("name"):
        record(FAIL, "协议：initialize", "握手没有返回 serverInfo")
        return
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    expected = {t["name"] for t in TOOLS}
    if names != expected:
        record(FAIL, "协议：tools/list", f"工具清单与实现不一致：多 {names - expected}，少 {expected - names}")
        return
    bad = [
        t["name"]
        for t in tools
        if not t.get("description", "").strip()
        or "required" not in t.get("inputSchema", {})
        or t["inputSchema"].get("type") != "object"
    ]
    if bad:
        record(FAIL, "协议：tools/list", f"这些工具的 schema 不完整：{bad}")
        return
    record(PASS, "协议：initialize + tools/list", f"{info['name']} v{info.get('version')}，{len(names)} 个工具，schema 完整")


def step_publish_gate() -> None:
    """发布门禁：**用临时素材**跑预检与两条拦截，并断言"一行 CLI 都没跑"。

    ★ 这是本地最有价值的一步：它验的是"这个服务会不会在用户没点头时动账号"。
      素材是临时造的假 mp4（预检只看路径/存在/非空），所以不需要真素材、也不需要 sau。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "media").mkdir()
        (root / "media" / "verify.mp4").write_bytes(b"\x00" * 64)
        cfg = SauConfig(cmd="sau", project_dir=str(root / "sau"), media_dir=str(root / "media"))
        server = Server(cfg)

        cli_calls = []

        def fake_run(*args, **kwargs):
            cli_calls.append(args)
            raise AssertionError("门禁阶段不该跑任何 CLI")

        with mock.patch("douyin_publish_mcp.sau.run", side_effect=fake_run):
            base = {"account": "verify", "file": "verify.mp4", "title": "验收-可忽略"}
            result, err = call(server, "douyin_publish_video", base, req_id=10)
            text = text_of(result)
            if err or "待发布的视频计划" not in text:
                record(FAIL, "发布门禁：预检", (err or text)[:400])
                return
            plan_id = _first(r'plan_id="([0-9a-f]{12})"', text)

            wrong = dict(base, plan_id="000000000000", confirm=True)
            result2, _ = call(server, "douyin_publish_video", wrong, req_id=11)
            changed = dict(base, title="验收-改过了", plan_id=plan_id, confirm=True)
            result3, _ = call(server, "douyin_publish_video", changed, req_id=12)

        ok_id = "没有发任何东西" in text_of(result2)
        ok_changed = "没有发任何东西" in text_of(result3)
        if not (ok_id and ok_changed):
            record(
                FAIL,
                "发布门禁",
                f"★ 该拦的没拦住：plan_id 不符={ok_id}，内容改过={ok_changed}\n"
                f"{text_of(result2)[:300]}\n{text_of(result3)[:300]}",
            )
            return
        if cli_calls:
            record(FAIL, "发布门禁", f"★ 门禁阶段跑了 {len(cli_calls)} 次 CLI，这是最严重的一类问题")
            return
        record(
            PASS,
            "发布门禁：预检 + 两条拦截",
            f"只返回计划（plan_id={plan_id}）；plan_id 不符与内容改过都被拒；全程 0 次 CLI 调用",
        )


def step_http() -> None:
    """HTTP 形态：真起服务、真发请求，验端点、鉴权与状态页。"""
    buf = io.StringIO()
    real_stderr = sys.stderr
    # ★ 服务的访问日志走 stderr（这是它的正常行为），但验收输出里不需要它们
    sys.stderr = buf
    try:
        _step_http_body()
    finally:
        sys.stderr = real_stderr


def _step_http_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "media").mkdir()
        cfg = SauConfig(cmd="sau", project_dir=str(root), media_dir=str(root / "media"))
        app = App(cfg, token="verify-token")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        token = {"Authorization": "Bearer verify-token"}
        checks = []

        def expect(name: str, got, want) -> None:
            checks.append((name, got == want, f"期望 {want}，实际 {got}"))

        try:
            status, _ = _http("GET", f"{url}/health")  # /health 永远免鉴权
            expect("/health 免鉴权", status, 200)

            status, _ = _http("GET", f"{url}/state.json")
            expect("无 token 访问 /state.json", status, 401)

            status, body = _http("GET", f"{url}/state.json", token)
            state = json.loads(body or b"{}")
            expect("/state.json 带 token", status, 200)
            checks.append(("state.json 有登录态字段", "loggedIn" in state, "缺 loggedIn"))

            status, body = _http("GET", f"{url}/", token)
            checks.append(("状态页含读取通道", status == 200 and "读取通道" in body.decode("utf-8", "replace"), ""))

            status, body = _http(
                "POST",
                f"{url}/mcp",
                token,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            info = (json.loads(body or b"{}").get("result") or {}).get("serverInfo", {})
            expect("/mcp initialize", (status, info.get("name")), (200, "douyin-publish-mcp"))

            status, body = _http(
                "POST",
                f"{url}/mcp",
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "douyin_my_profile", "arguments": {}},
                },
            )
            res = json.loads(body or b"{}").get("result") or {}
            checks.append(
                (
                    "无凭据时读取工具给的是可读提示（不是空结果）",
                    res.get("isError") is True and "douyin_account_login" in text_of(res),
                    text_of(res)[:200],
                )
            )

            status, _ = _http(
                "POST",
                f"{url}/mcp",
                None,
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            )
            expect("无 token 访问 /mcp", status, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()

        bad = [f"{n}（{why}）" for n, ok, why in checks if not ok]
        if bad:
            record(FAIL, "HTTP 形态", "\n".join("· " + b for b in bad))
        else:
            record(PASS, "HTTP 形态", f"{len(checks)} 项端点/鉴权检查全通过（服务真起、请求真发）")


def _http(method: str, url: str, headers=None, payload=None):
    """发一个 HTTP 请求，返回 (状态码, 响应体)；4xx/5xx 也算正常返回。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── 真机段（需要 sau + 登录态 + 外网）────────────────────────


def step_env(cfg: SauConfig, account: str) -> bool:
    problems = []
    if cfg.cmd:
        if not Path(cfg.cmd).is_file():
            problems.append(f"SAU_CMD 指向的文件不存在：{cfg.cmd}")
    elif cfg.project_dir:
        if not Path(cfg.project_dir).is_dir():
            problems.append(f"SAU_DIR 指向的目录不存在：{cfg.project_dir}")
        elif not shutil.which(cfg.uv):
            problems.append(f"SAU_DIR 已配，但找不到 uv（{cfg.uv}）—— 需要用 uv 拉起 sau")
    elif not shutil.which("sau"):
        problems.append(
            "SAU_CMD / SAU_DIR 都没配，PATH 里也没有 sau。"
            "请到 MCP 配置页填 SAU_CMD（sau.exe 绝对路径）或 SAU_DIR（项目根目录）"
        )
    if not cfg.media_dir:
        problems.append("SAU_MEDIA_DIR 没配：发布工具会拒绝执行（读取不受影响）")
    elif not Path(cfg.media_dir).is_dir():
        problems.append(f"素材目录不存在：{cfg.media_dir}")

    if problems:
        record(FAIL, "真机：环境前置", "\n".join("· " + p for p in problems))
        return False
    try:
        cred = resolve_credential(cfg, account)
    except Exception as e:  # noqa: BLE001 —— 验收脚本不该因为一个配置问题直接崩
        record(FAIL, "真机：环境前置（凭据）", f"定位凭据时出错：{e}")
        return False
    if not cred.usable:
        record(SKIP, "真机：环境前置", "配置就绪，但没有可用凭据（还没扫码登录）\n" + cred.describe())
        return False
    record(PASS, "真机：环境前置", cred.describe().replace("\n", "；"))
    return True


def step_login(server: Server, account: str) -> bool:
    result, err = call(server, "douyin_account_status", {"account": account})
    if err:
        record(FAIL, "真机：登录态", err)
        return False
    text = text_of(result)
    if "已登录，可以发布" in text:
        record(PASS, "真机：登录态", "sau douyin check 判定为已登录")
        return True
    if "未登录" in text:
        record(
            FAIL,
            "真机：登录态",
            "本地 cookie 不存在或已失效。下一步：调 douyin_account_login 让用户扫码\n"
            + text.splitlines()[0],
        )
        return False
    record(SKIP, "真机：登录态", "退出码与输出都没给出明确结论：\n" + text[:400])
    return False


def step_read(server: Server, account: str, keyword: str) -> str:
    """读取五连：搜索 → 详情 → 用户主页 → 我的主页 → 分享解析（真实 id 串起来）。

    返回搜到的那条 aweme_id（供后面的浏览器通道步骤复用；没搜到就是空串）。
    """
    result, err = call(server, "douyin_search_videos", {"keyword": keyword, "count": 5, "account": account})
    if err:
        record(FAIL, f"真机：搜索「{keyword}」", err)
        return ""
    text = text_of(result)
    if is_error(result):
        record(FAIL, f"真机：搜索「{keyword}」", "工具回的是错误（不是空结果）——这正是最该看的失败：\n" + text[:600])
        return ""
    if "搜到 0 条" in text:
        record(
            SKIP,
            f"真机：搜索「{keyword}」",
            "返回 0 条（不算错误但不正常）：换个更宽的关键词、publish_time 放宽到 all 再试",
        )
        return ""

    aweme_id = _first(r"aweme_id：(\d{15,25})", text)
    sec_user_id = _first(r"sec_user_id：(MS4w[A-Za-z0-9_-]+)", text)
    like = _first(r"赞 (\d+)", text)
    record(PASS, f"真机：搜索「{keyword}」", f"拿到作品（首条 aweme_id={aweme_id}，赞 {like}）")

    if aweme_id:
        result, err = call(server, "douyin_video_detail", {"aweme_id": aweme_id, "account": account})
        text = text_of(result)
        if err or is_error(result):
            record(FAIL, "真机：作品详情", (err or text)[:600])
        elif "无水印直链" in text:
            record(PASS, "真机：作品详情", f"拿到详情与无水印直链（{_channel_of(text)}）")
        else:
            record(SKIP, "真机：作品详情", "有返回但没有直链字段：\n" + text[:400])

        share_text = f"7.32 复制打开抖音，看看这个作品 https://www.douyin.com/video/{aweme_id}"
        result, err = call(server, "douyin_parse_share_link", {"share_text": share_text, "account": account})
        text = text_of(result)
        if err or is_error(result):
            record(FAIL, "真机：分享链接解析", (err or text)[:600])
        elif aweme_id in text:
            record(PASS, "真机：分享链接解析", f"从分享文本里解出作品并读到详情（{_channel_of(text)}）")
        else:
            record(FAIL, "真机：分享链接解析", "解析结果里没有那条作品：\n" + text[:400])

    if sec_user_id:
        result, err = call(server, "douyin_user_profile", {"sec_user_id": sec_user_id, "limit": 5, "account": account})
        text = text_of(result)
        if err or is_error(result):
            record(FAIL, "真机：用户主页", (err or text)[:600])
        elif "粉丝" in text:
            record(PASS, "真机：用户主页", "拿到资料与最近作品" + (f"（{_channel_of(text)}）" if _channel_of(text) else ""))
        else:
            record(FAIL, "真机：用户主页", "返回里没有资料字段：\n" + text[:400])
    else:
        record(SKIP, "真机：用户主页", "搜索结果里没解析到 sec_user_id，跳过")

    result, err = call(server, "douyin_my_profile", {"limit": 5, "account": account})
    text = text_of(result)
    if err or is_error(result):
        record(FAIL, "真机：我的主页", (err or text)[:600])
    elif "当前登录账号" in text:
        record(PASS, "真机：我的主页", "读到当前账号资料" + (f"（{_channel_of(text)}）" if _channel_of(text) else ""))
    else:
        record(FAIL, "真机：我的主页", "返回里没有账号资料：\n" + text[:400])
    return aweme_id


def step_browser_channel(cfg: SauConfig, account: str, aweme_id: str) -> None:
    """浏览器通道单独验一遍：**别只看那四个工具"过了"**。

    ★ 为什么必须单独验（这是它唯一会被漏掉的方式）：
      作品详情/用户作品在直连被风控挡住时会自动回退，于是"工具 PASS"其实常常是
      浏览器通道在干活 —— 可反过来，哪天直连被放开，工具照样 PASS，而浏览器通道
      坏掉了没人知道（它只是不再参与）。这一步直接调通道本身，顺便说清
      "这一轮到底是直连在干活还是浏览器在救"，以及**评论**（只有浏览器通道有）。

    取驱动/浏览器的路数都在这一步的失败说明里（`no_driver` → 报"驱动没就位"而不是
    "通道坏了"：环境没备好与功能失效要分开说）。
    """
    from douyin_publish_mcp import douyin_browser as browser  # 局部导入：--local-only 时不碰

    bcfg = browser.BrowserConfig.from_env()
    if not bcfg.enabled:
        record(SKIP, "真机：浏览器通道", "已被 DOUYIN_BROWSER=off 关闭（只留直连通道）")
        return
    if not aweme_id:
        record(SKIP, "真机：浏览器通道", "没拿到 aweme_id（先让搜索那一步过）")
        return
    try:
        cred = resolve_credential(cfg, account)
    except Exception as e:  # noqa: BLE001
        record(FAIL, "真机：浏览器通道", f"定位凭据失败：{e}")
        return
    if not cred.usable:
        record(SKIP, "真机：浏览器通道", "没有可用凭据（还没扫码登录）")
        return

    note = browser.describe(bcfg, cfg)
    started = time.time()
    try:
        payload = browser.read(
            bcfg, cred,
            # 作品详情 + 评论：两个都在同一个视频页上，一次导航抓全
            [{"kind": "video_detail", "aweme_id": aweme_id},
             {"kind": "comments", "aweme_id": aweme_id}],
            cfg,
        )
    except DouyinWebError as e:
        record(FAIL, "真机：浏览器通道", f"{e.describe()}\n{note}")
        return
    elapsed_ms = int((time.time() - started) * 1000)

    if not payload.get("ok"):
        err = payload.get("error") or {}
        kind = str(err.get("kind") or "unknown")
        detail = "\n".join(x for x in (err.get("message", ""), err.get("hint", ""), note) if x)
        # ★ 缺驱动/缺浏览器 = 环境没备好（SKIP + 怎么备），其它才是通道真的失效（FAIL）
        record(SKIP if kind in ("no_driver", "no_browser") else FAIL, "真机：浏览器通道", detail[:800])
        return

    results = payload.get("results") or {}
    detail = ((results.get("video_detail:%s" % aweme_id) or {}).get("body") or {}).get("aweme_detail") or {}
    urls = ((detail.get("video") or {}).get("play_addr") or {}).get("url_list") or []
    comments = ((results.get("comments:%s" % aweme_id) or {}).get("body") or {}).get("comments") or []
    if not detail or not urls:
        record(FAIL, "真机：浏览器通道", "抓到了响应但详情里没有可用字段（接口可能改版）\n" + note)
        return

    direct = "直连：未知"
    try:
        DouyinWebClient(cred, WebConfig()).video_detail(aweme_id)
        direct = "直连：现在也通（回退仍可用，只是这一轮没被用到）"
    except DouyinWebError as e:
        direct = f"直连：{e.kind}（所以详情/用户作品这几步是浏览器救回来的）"

    record(
        PASS,
        "真机：浏览器通道",
        f"{payload.get('browser')}｜{elapsed_ms}ms｜详情 {str(detail.get('desc') or '')[:16]!r}"
        f"｜无水印直链 {len(urls)} 个｜评论 {len(comments)} 条\n· {direct}\n· {note}",
    )


def step_publish_real(cfg: SauConfig, account: str, media_file: str, title: str, confirm_publish: bool) -> None:
    """用**用户自己的素材**跑预检（可选真发）。★ 默认只预检。"""
    if not media_file:
        record(SKIP, "真机：发布", "没给 --publish-file，跳过")
        return
    server = Server(cfg)
    base = {"account": account, "file": media_file, "title": title}
    result, err = call(server, "douyin_publish_video", base, req_id=20)
    text = text_of(result)
    if err or "待发布的视频计划" not in text:
        record(FAIL, "真机：发布预检", (err or text)[:500])
        return
    plan_id = _first(r'plan_id="([0-9a-f]{12})"', text)
    record(PASS, "真机：发布预检", f"素材在素材目录内、预检通过（plan_id={plan_id}）")

    if not confirm_publish:
        record(
            SKIP,
            "真机：真发",
            "默认不真发。要真发：加 --confirm-publish（会落到账号上，不可撤销，发完到创作者中心核对）",
        )
        return
    result, err = call(server, "douyin_publish_video", dict(base, plan_id=plan_id, confirm=True), req_id=21)
    text = text_of(result)
    if err or is_error(result):
        record(FAIL, "真机：真发", (err or text)[:800])
    else:
        record(PASS, "真机：真发", 'CLI 未报错（★ "命令成功 ≠ 已公开可见"，审核在平台侧）请到创作者中心核对')


# ── 主流程 ──────────────────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="douyin-publish-mcp 端到端验收")
    parser.add_argument("--local-only", action="store_true", help="只跑本地段（不联网、不碰 sau）")
    parser.add_argument("--account", default="", help="账号名（默认取 SAU_ACCOUNT，兜底 main）")
    parser.add_argument("--keyword", default="美食", help="搜索用关键词（默认 美食）")
    parser.add_argument("--publish-file", default="", help="用你自己的素材做发布预检（素材目录内路径）")
    parser.add_argument("--publish-title", default="验收测试-可忽略", help="预检用的标题")
    parser.add_argument("--confirm-publish", action="store_true", help="★ 真的发出去（默认只验门禁）")
    args = parser.parse_args(argv)

    cfg = SauConfig.from_env()
    account = (args.account or default_account()).strip()
    print(f"账号：{account}｜关键词：{args.keyword}｜模式：{'仅本地' if args.local_only else '本地 + 真机'}")
    print("本地段：不联网、不跑 sau、不碰账号。真机段：会真调抖音接口与 sau（默认仍不发布）。")

    banner("1/4 单测")
    step_unit_tests()

    banner("2/4 协议层")
    step_protocol()

    banner("3/4 发布门禁（本地）")
    step_publish_gate()

    banner("4/4 HTTP 形态（本地）")
    step_http()

    if not args.local_only:
        server = Server(cfg)
        banner("5/8 真机：环境前置")
        env_ok = step_env(cfg, account)
        banner("6/8 真机：登录态与读取")
        login_ok = step_login(server, account) if env_ok else _skip("真机：登录态", "环境前置没过，跳过")
        aweme_id = ""
        if env_ok and login_ok:
            aweme_id = step_read(server, account, args.keyword)
        else:
            _skip("真机：读取五连", "需要「环境就绪 + 已登录」；补上后再跑本脚本")
        banner("7/8 真机：浏览器通道")
        if env_ok and login_ok:
            step_browser_channel(cfg, account, aweme_id)
        else:
            _skip("真机：浏览器通道", "需要「环境就绪 + 已登录」；补上后再跑本脚本")
        banner("8/8 真机：发布")
        step_publish_real(cfg, account, args.publish_file, args.publish_title, args.confirm_publish)
    else:
        for name in ("真机：环境前置", "真机：登录态", "真机：读取五连",
                     "真机：浏览器通道", "真机：发布"):
            _skip(name, "本次是 --local-only；这些需要 sau 与登录态，本地验不了")

    banner("汇总" if args.local_only else "8/8 汇总")
    fails = [r for r in RESULTS if r[0] == FAIL]
    skips = [r for r in RESULTS if r[0] == SKIP]
    passes = [r for r in RESULTS if r[0] == PASS]
    print(f"PASS {len(passes)}｜FAIL {len(fails)}｜SKIP {len(skips)}")
    for status, name, detail in RESULTS:
        if status == FAIL:
            print(_console_safe(f"  [FAIL] {name}：{detail.splitlines()[0] if detail else ''}"))
    if skips:
        print("跳过项（**不是通过**）：")
        for _, name, detail in skips:
            print(_console_safe(f"  [SKIP] {name}：{detail.splitlines()[0] if detail else ''}"))
    print()
    if fails:
        print("本地段已通过的部分是真的通过了；FAIL 项按上面的说明修，再重跑本脚本。")
    elif skips:
        print("没有 FAIL，但跳过项还没验过 —— 本地段全绿只说明「服务的壳是对的」，")
        print("不说明抖音认它。要判读取通道通不通，得在装了 sau 的机器上跑不带 --local-only 的那遍。")
    else:
        print("全绿。")
    return 1 if fails else 0


def _skip(name: str, reason: str) -> bool:
    record(SKIP, name, reason)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
