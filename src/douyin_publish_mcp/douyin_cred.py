"""抖音**读取通道**的凭据：把登录态从 social-auto-upload 的凭据文件里取出来。

## 为什么复用 sau 的凭据文件，而不是再登录一次

本服务的登录态只有一个来源：`sau douyin login` 写出的
`<项目>/cookies/douyin_<账号>.json`（patchright 的 storage_state，标准
Playwright 格式）。读取工具若自带一套登录，会出现两个坏结果：
①「发布说已登录、读取说没登录」这种自相矛盾的状态，
② 多一份会过期、要单独清理的凭据。

## 只回显 cookie 名，不回显值

cookie 是凭据。工具输出与状态页里只允许出现 cookie 的**名字**
（`sessionid`、`ttwid` …），值一律不外显 —— 模型没有理由看到它，
而它会顺着工具输出进入对话上下文、本地日志和别人的截图里。

## 安全边界

本模块**只读**凭据文件：不写、不改、不删（唯一的删除动作在 `sau.logout`，
且那件事只允许删一个文件）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .sau import SauConfig, SauError, account_file_path

# ── cookie 名 ────────────────────────────────────────────────
# 有其中任意一个才算「已登录」：这些是抖音的会话凭据，匿名访问不会带。
LOGIN_COOKIE_KEYS = (
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "passport_csrf_token",
)

# ★ ttwid 是**风控设备标识**：缺它时多数 web 接口会直接
#   `{"status_code":0,"status_msg":"blocked"}`（实测：不带 cookie 请求搜索接口就是这条）。
#   所以它是「能不能读」的关键，而不是可选项。
DEVICE_COOKIE_KEYS = ("ttwid", "odin_tt", "install_id", "ttwid_ss")

# 请求抖音 web 接口时要带上的 cookie（按重要性排序，只用于拼 Cookie 头）
REQUEST_COOKIE_KEYS = (
    "ttwid",
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "odin_tt",
    "install_id",
    "msToken",
    "s_v_web_id",
    "tt_scid",
    "ttwid_ss",
)

_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.%]+$")


@dataclass
class Credential:
    """一次读取要用的凭据（含"它是从哪来的"这件事，便于排错）。

    ★ `cookie` 字段是**敏感值**：只在发起 HTTP 请求时使用，
      永远不要拼进给模型看的文本里 —— 对外描述一律用 [`describe`]。
    """

    cookie: str = ""
    source: str = ""
    path: Optional[Path] = None
    account: str = ""
    keys: List[str] = field(default_factory=list)
    has_login: bool = False
    has_device: bool = False

    @property
    def usable(self) -> bool:
        """能不能拿去发请求（至少要有个 ttwid 或登录 cookie）"""
        return bool(self.cookie) and (self.has_login or self.has_device)

    def describe(self) -> str:
        """给模型/状态页看的说明（**只出现 cookie 名，不出现值**）"""
        if not self.cookie:
            base = (
                "没有可用的抖音凭据。读取工具需要登录态：请先调 douyin_account_login "
                "让用户扫码（凭据由 sau 写进项目目录的 cookies/）。"
            )
            # ★ 必须带上"为什么定位不到"：只配了 SAU_CMD（没有项目目录）的人，
            #   光看上面那句会去扫码，而扫码在这条配置下解决不了问题
            return base + ("\n" + self.source if self.source else "")
        state = "已登录" if self.has_login else "匿名（无 sessionid 等登录 cookie）"
        device = "有" if self.has_device else "缺"
        src = self.source or "未记录来源"
        detail = "、".join(self.keys[:8]) + ("…" if len(self.keys) > 8 else "")
        return (
            f"凭据来源：{src}\n"
            f"账号：{self.account or '(未指定)'}｜登录态：{state}｜设备标识(ttwid 等)：{device}\n"
            f"cookie 名（{len(self.keys)} 个）：{detail or '(无)'}"
        )


# ── storage_state → cookie ───────────────────────────────────


def parse_storage_state(raw: str) -> List[Dict[str, Any]]:
    """从 patchright 的 storage_state JSON 里取出 cookies 数组。

    ★ 解析失败返回空列表而不是抛异常：调用方要能区分
      "文件坏了"（`parse_storage_state` 空 + 文件存在）与"压根没登录"（文件不存在），
      两者的下一步动作不一样（重扫 vs 修文件）。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        cookies = data.get("cookies")
        if isinstance(cookies, list):
            return [c for c in cookies if isinstance(c, dict)]
    if isinstance(data, list):  # 有的实现直接存 cookies 数组
        return [c for c in data if isinstance(c, dict)]
    return []


def parse_cookie_header(raw: str) -> List[Tuple[str, str]]:
    """把 `a=1; b=2` 这样的 Cookie 串解析成键值对列表（保序）。"""
    pairs: List[Tuple[str, str]] = []
    for chunk in (raw or "").split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name and _COOKIE_NAME_RE.match(name):
            pairs.append((name, value.strip()))
    return pairs


def cookie_header_from_cookies(cookies: Iterable[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """从 storage_state 的 cookies 数组里挑出抖音的 cookie（保序、去重）。"""
    seen: Dict[str, str] = {}
    for c in cookies:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        domain = str(c.get("domain") or "")
        if not name or not _COOKIE_NAME_RE.match(name):
            continue
        # 只要抖音域的：storage_state 里往往还混着别的站点（patchright 抓页面时顺带存的）
        if domain and "douyin" not in domain and "bytedance" not in domain:
            continue
        if name in seen:
            continue
        seen[name] = value
    return list(seen.items())


def build_cookie(pairs: Iterable[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """拼 Cookie 头：**常用键优先**，其余键排在后面。

    ★ 顺序不是装饰：抖音的风控会看 cookie 的组织方式，
      `ttwid`/`sessionid` 放前面比"按文件里碰巧的顺序"更稳。
    """
    table = {name: value for name, value in pairs}
    ordered: List[Tuple[str, str]] = []
    used = set()
    for key in REQUEST_COOKIE_KEYS:
        if key in table:
            ordered.append((key, table[key]))
            used.add(key)
    for name, value in pairs:
        if name not in used:
            ordered.append((name, value))
    return "; ".join("%s=%s" % (n, v) for n, v in ordered), [n for n, _ in ordered]


def credential_from_raw(raw: str, source: str, account: str = "", path: Optional[Path] = None) -> Credential:
    """把一段"可能就是 storage_state / 也可能是裸 Cookie 串"的文本变成凭据。"""
    pairs: List[Tuple[str, str]] = []
    text = (raw or "").strip()
    if text.startswith("{") or text.startswith("["):
        pairs = cookie_header_from_cookies(parse_storage_state(text))
    if not pairs:
        pairs = parse_cookie_header(text)
    cookie, keys = build_cookie(pairs)
    return Credential(
        cookie=cookie,
        source=source,
        path=path,
        account=account,
        keys=keys,
        has_login=any(k in keys for k in LOGIN_COOKIE_KEYS),
        has_device=any(k in keys for k in DEVICE_COOKIE_KEYS),
    )


# ── 凭据定位 ─────────────────────────────────────────────────


def default_account() -> str:
    """默认账号名：只读显式配置，**不猜**（与状态页的默认值同一口径）。"""
    return (os.environ.get("SAU_ACCOUNT") or os.environ.get("DOUYIN_ACCOUNT") or "main").strip()


def resolve_credential(cfg: SauConfig, account: str = "") -> Credential:
    """按优先级找一个可用的凭据：

    1. `DOUYIN_COOKIE_FILE`：显式指定的文件（storage_state 或裸 Cookie 串）——
       给"读取和发布不是一个账号/不在一个项目目录"的人留的口子；
    2. `DOUYIN_COOKIE`：直接给整串 cookie（容器/临时调试用）；
    3. `<SAU_DIR>/cookies/douyin_<账号>.json`：**正常路径**，与发布共用同一份登录态。

    ★ 顺序即优先级：显式配置永远压过推导出来的路径 —— 环境里的残留文件
      不应该盖掉用户明说的东西。
    """
    account = (account or "").strip() or default_account()

    explicit = (os.environ.get("DOUYIN_COOKIE_FILE") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise SauError(
                f"读不到 DOUYIN_COOKIE_FILE 指向的文件：{path}（{e.strerror or e}）。"
                f"请核对路径，或去掉这个环境变量改用 sau 的凭据文件。"
            ) from None
        cred = credential_from_raw(raw, f"DOUYIN_COOKIE_FILE（{path}）", account, path)
        if not cred.usable:
            raise SauError(f"{path} 里没有可用的抖音 cookie（既没有 ttwid，也没有登录标识）。")
        return cred

    inline = (os.environ.get("DOUYIN_COOKIE") or "").strip()
    if inline:
        cred = credential_from_raw(inline, "DOUYIN_COOKIE（环境变量）", account)
        if not cred.usable:
            raise SauError("DOUYIN_COOKIE 里没有可用的抖音 cookie（既没有 ttwid，也没有登录标识）。")
        return cred

    try:
        path = account_file_path(cfg, account)
    except SauError as e:
        # ★ 只用 SAU_CMD、没配 SAU_DIR 的用户会走到这里（发布能用、读取定位不到凭据）。
        #   不要把它变成一次崩溃，也不要说成"没登录"—— 如实说清"缺什么"，
        #   并给出两条可选的补救路径（显式文件 / 显式 cookie）。
        return Credential(
            source=(
                f"{e}\n"
                f"（替代做法：设 DOUYIN_COOKIE_FILE 指向凭据文件，或设 DOUYIN_COOKIE 直接给整串 cookie）"
            ),
            account=account,
        )
    if not path.is_file():
        # 不抛异常：调用方要把"没登录"讲成"去扫码"，而不是崩一次
        return Credential(source=f"sau 凭据文件不存在（{path}）", account=account, path=path)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return Credential(
            source=f"读不到 sau 凭据文件（{path}：{e.strerror or e}）", account=account, path=path
        )
    cred = credential_from_raw(raw, f"sau 凭据文件（{path}）", account, path)
    if not cred.cookie:
        cred.source = f"sau 凭据文件里没有 cookie（{path}）"
    return cred


# ── msToken ─────────────────────────────────────────────────


def random_ms_token(length: int = 128) -> str:
    """生成一个 msToken（随机值兜底）。

    ★ 抖音 web 端要求请求参数里带 `msToken`。真实浏览器里的值是它自己的
      JS 生成的（带加密逻辑），本服务不去复刻那套 —— 取随机 128 位，
      这也是社区实现普遍的做法。若平台因此拦请求，症状是
      `status_msg=blocked`，那时再考虑从浏览器里取真值。
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789=+"
    return "".join(secrets.choice(alphabet) for _ in range(max(64, length)))


__all__ = [
    "Credential",
    "LOGIN_COOKIE_KEYS",
    "DEVICE_COOKIE_KEYS",
    "REQUEST_COOKIE_KEYS",
    "parse_storage_state",
    "parse_cookie_header",
    "cookie_header_from_cookies",
    "build_cookie",
    "credential_from_raw",
    "default_account",
    "resolve_credential",
    "random_ms_token",
]
