"""抖音 **web 数据访问层**（纯标准库，只走读取类接口）。

## 为什么是 HTTP 直连，而不是像小红书 MCP 那样开浏览器

小红书 MCP 的数据来自真实浏览器渲染（go-rod），换来的是稳、但每次操作要几秒到十几秒、
还要装一份 Chromium。本服务的底线是**零三方依赖 + 内网能 uvx 拉起**，
所以读取走抖音 web 接口直连；代价是必须带**用户的登录 cookie**（见 `douyin_cred`），
且平台改版后可能失效 —— 失效的表现与排查顺序写在 `douyin_sign` 里。

★ 这里**不做**「用浏览器兜底」的降级：半套降级会让"为什么这次慢"变得无法解释。
   真需要浏览器路线时，是换实现，不是在本模块里加分支。

## 与本服务其它部分的关系

- 凭据：`douyin_cred.resolve_credential`（与发布共用 sau 的 storage_state）；
- 门禁：本模块**只读**。写操作（评论/点赞/收藏）不在这里 —— 那些必须过
  `server.py` 的 confirm 门禁，所以它们出现在第二批工具里，而不是这里随手加一个 POST。

## 输出要裁剪

抖音接口的原始响应动辄几十 KB（含埋点、推荐位、原始结构体），
原样交给模型既烧上下文又容易被抄错字段。所以对外一律走 `pick_*` 裁剪函数，
只留"人真正会看"的字段。
"""

from __future__ import annotations

import gzip
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .douyin_cred import Credential, random_ms_token
from .douyin_sign import sign_a_bogus, sign_status

# ── 请求常量 ─────────────────────────────────────────────────
# ★ 这批固定参数是抖音 web 端的"客户端指纹"，缺了会直接 blocked。
#   数值抄自实测可用的实现（与 douyin-mcp-node 的 COMMON_PARAMS 同口径），
#   改动它们之前先想清楚是要模拟哪个客户端。
COMMON_PARAMS: Dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "cookie_enabled": "true",
    "screen_width": "1920",
    "screen_height": "1080",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "135.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "135.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "8",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "50",
}

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)

# 接口路径（★ 这四个是实测可用的，来自 douyin-mcp-node 的 client.ts；
#   新加的路径必须在真机上验过再合并进来，否则会变成"永远空结果"）
PATH_SEARCH = "/aweme/v1/web/general/search/single/"
PATH_VIDEO_DETAIL = "/aweme/v1/web/aweme/detail/"
PATH_USER_PROFILE = "/aweme/v1/web/user/profile/other/"
PATH_USER_POST = "/aweme/v1/web/aweme/post/"
PATH_MY_PROFILE = "/aweme/v1/web/user/profile/self/"

BLOCKED_MARK = "blocked"


class DouyinWebError(Exception):
    """读取失败。★ 带 `kind` 与 `hint`：调用方要把「下一步做什么」讲给用户/模型，
    而不是只回一句 `HTTP 403`。"""

    #: kind 取值：no_credential / not_login / blocked / network / upstream / parse
    def __init__(self, message: str, kind: str = "network", hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.hint = hint

    def describe(self) -> str:
        text = str(self)
        return f"{text}\n\n{self.hint}" if self.hint else text


@dataclass
class WebConfig:
    """读取通道的运行参数（默认值即可用；一般不需要动）"""

    timeout: int = 15
    retries: int = 1
    #: 翻页之间的固定间隔（秒）：批量拉取时给平台一点呼吸，降低风控概率
    page_interval: float = 1.0
    user_agent: str = DESKTOP_UA
    base_url: str = "https://www.douyin.com"


def _dig(node: Any, *path: Any, default: Any = None) -> Any:
    """安全取深层字段：任何一层缺失/类型不对都返回 default。

    ★ 上游结构说变就变，用 `a["b"]["c"]` 会在改版当天变成一片 KeyError，
      而这里最坏的结果是"这个字段没有"。
    """
    cur = node
    for key in path:
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, list) and isinstance(key, int):
            if key < 0 or key >= len(cur):
                return default
            cur = cur[key]
        else:
            return default
    return default if cur is None else cur


def _fmt_time(seconds: Any) -> str:
    """抖音的时间戳是秒（字符串或数字），统一成本地时间字符串。"""
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _first_url(node: Any) -> str:
    """取 url_list 里的第一个地址（去水印的 `playwm`→`play` 在调用处做）。"""
    urls = _dig(node, "url_list", default=[])
    if isinstance(urls, list) and urls:
        return str(urls[0])
    return ""


def _strip_watermark(url: str) -> str:
    """把播放地址从带水印换成无水印。

    ★ 只做字符串替换：抖音的 `playwm`（watermark）与 `play` 是同一份资源的两个入口，
      不做下载、不碰文件。
    """
    return (url or "").replace("playwm", "play")


class DouyinWebClient:
    """抖音 web 读接口的客户端（一次工具调用建一个，用完即弃）。

    ★ 不做全局单例：cookie 会被重新扫码替换，单例就必须自己管"何时失效"，
      而那是这类服务最容易出的一类脏 bug（改了 cookie 还在用旧的）。
    """

    def __init__(self, credential: Credential, config: Optional[WebConfig] = None):
        self.cred = credential
        self.cfg = config or WebConfig()
        self.ms_token = random_ms_token()
        self.signed = False

    # ── 请求 ────────────────────────────────────────────────

    def _check_credential(self) -> None:
        if not self.cred.cookie:
            raise DouyinWebError(
                "没有可用的抖音凭据：" + (self.cred.source or "未知来源"),
                kind="no_credential",
                hint=(
                    "读取需要登录态。请先调用 douyin_account_login（account=\"<账号名>\"）"
                    "让用户扫码；凭据由 social-auto-upload 写进项目目录的 cookies/ 后本工具即可用。"
                ),
            )
        if not self.cred.has_login and not self.cred.has_device:
            raise DouyinWebError(
                "凭据里既没有登录标识（sessionid 等），也没有设备标识（ttwid）",
                kind="no_credential",
                hint="多半是扫码没扫上或 cookie 文件被覆盖过。请让用户重新扫码登录。",
            )

    def _headers(self, is_post: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": self.cred.cookie,
            "Referer": "https://www.douyin.com/discover" if is_post else "https://www.douyin.com/",
            "User-Agent": self.cfg.user_agent,
            "sec-ch-ua": '"Chromium";v="135", "Google Chrome";v="135", "Not-A.Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if is_post:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        return headers

    def _request(
        self,
        path: str,
        params: Dict[str, Any],
        is_post: bool = False,
        post_data: str = "",
    ) -> Dict[str, Any]:
        """发一次请求并返回解析后的 JSON。

        失败一律抛 [`DouyinWebError`]（带 kind/hint），不返回"空结果"——
        「没搜到」和「被拦了」对用户是两件事，前者换关键词、后者要重新登录。
        """
        self._check_credential()

        query: Dict[str, str] = dict(COMMON_PARAMS)
        query["msToken"] = self.ms_token
        for key, value in params.items():
            if value is None:
                continue
            query[str(key)] = str(value)

        raw_query = urllib.parse.urlencode(query)
        a_bogus = sign_a_bogus(raw_query, self.cfg.user_agent, post_data)
        if a_bogus:
            self.signed = True
            query["a_bogus"] = a_bogus

        url = self.cfg.base_url + path + "?" + urllib.parse.urlencode(query)
        body = post_data.encode("utf-8") if is_post else None
        headers = self._headers(is_post)

        last_error: Optional[Exception] = None
        for attempt in range(max(1, self.cfg.retries + 1)):
            try:
                raw = self._open(url, headers, body, is_post)
                return self._parse(raw)
            except DouyinWebError:
                raise  # 已经归好类（blocked / upstream / parse），重试没有意义
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                if attempt < self.cfg.retries:
                    time.sleep(0.6 + random.random() * 0.6)  # 抖动退避：重试也别踩同一个节奏
        raise DouyinWebError(
            f"请求抖音接口失败：{last_error}",
            kind="network",
            hint=(
                "网络不通或平台拒绝连接。请确认本机能访问 douyin.com；"
                "若本服务跑在内网机器上，可能需要在配置里加代理。"
            ),
        )

    def _open(self, url: str, headers: Dict[str, str], body: Optional[bytes], is_post: bool) -> bytes:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST" if is_post else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as resp:
                return _decompress(resp.read(), resp.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError as e:
            # ★ 4xx/5xx 的响应体里往往有平台的解释，别丢掉
            detail = ""
            try:
                detail = _decompress(e.read(), e.headers.get("Content-Encoding", "")).decode(
                    "utf-8", errors="replace"
                )[:500]
            except Exception:  # noqa: BLE001
                detail = ""
            if e.code in (401, 403):
                raise DouyinWebError(
                    f"抖音返回 {e.code}（拒绝访问）{('：' + detail) if detail else ''}",
                    kind="blocked",
                    hint="多半是登录态/设备标识被风控。先调 douyin_account_status 确认登录态，必要时重新扫码。",
                ) from None
            raise DouyinWebError(
                f"抖音返回 HTTP {e.code}{('：' + detail) if detail else ''}",
                kind="network",
                hint="这不是登录问题，先看上面的响应片段；持续出现再考虑接口是否已改版。",
            ) from None

    def _parse(self, raw: bytes) -> Dict[str, Any]:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise DouyinWebError(
                "抖音返回了空响应",
                kind="blocked",
                hint="空响应通常就是风控（未登录、cookie 过期或请求指纹不对）。先确认登录态。",
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise DouyinWebError(
                f"抖音返回的不是 JSON（前 200 字）：{text[:200]}",
                kind="parse",
                hint="多半是被风控页/验证码页拦下了。让用户重新扫码登录后再试。",
            ) from None
        if not isinstance(data, dict):
            raise DouyinWebError("抖音返回的 JSON 不是对象", kind="parse", hint="接口可能已改版。")

        status_msg = str(data.get("status_msg") or "")
        if status_msg.lower() == BLOCKED_MARK:
            raise DouyinWebError(
                "抖音把这次请求判为 blocked（风控拦截）",
                kind="blocked",
                hint=(
                    "按顺序排查：① 凭据里有没有 ttwid（本工具输出里有 cookie 名清单）"
                    "② sessionid 是否过期（调 douyin_account_login 重扫）"
                    "③ 是否为「无签名直连」被拦（见 douyin_sign.py 的说明）。"
                ),
            )
        status_code = data.get("status_code")
        if status_code not in (0, None) and not _dig(data, "aweme_detail") and not _dig(data, "aweme_list"):
            raise DouyinWebError(
                f"抖音接口返回错误：status_code={status_code} status_msg={status_msg or '(无)'}",
                kind="upstream",
                hint="这通常表示参数不对（id 抄错、被删的作品）或接口已改版；换一个目标再试一次。",
            )
        return data

    # ── 读接口 ──────────────────────────────────────────────

    def search_videos(
        self,
        keyword: str,
        count: int = 10,
        offset: int = 0,
        sort_type: int = 0,
        publish_time: int = 0,
    ) -> List[Dict[str, Any]]:
        """按关键词搜视频。返回裁剪后的列表（每条：[`pick_video`] 的产物）。"""
        params: Dict[str, Any] = {
            "keyword": keyword,
            "offset": offset,
            "count": count,
            "search_channel": "aweme_general",
            "search_source": "tab_search",
            "query_correct_type": "1",
            "is_filter_search": "1" if (sort_type or publish_time) else "0",
            "sort_type": sort_type,
            "publish_time": publish_time,
            "list_type": "multi",
            "need_filter_settings": "1",
        }
        if sort_type or publish_time:
            params["filter_selected"] = json.dumps(
                {"sort_type": str(sort_type), "publish_time": str(publish_time)}
            )
        data = self._request(PATH_SEARCH, params)
        items = data.get("data")
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in items:
            # 搜索结果把作品包在 aweme_info 里（广告/直播卡片没有这个字段，跳过）
            aweme = _dig(item, "aweme_info") or item
            picked = pick_video(aweme)
            if picked:
                out.append(picked)
        return out

    def video_detail(self, aweme_id: str) -> Dict[str, Any]:
        """取单个作品详情（含无水印播放地址）。"""
        data = self._request(
            PATH_VIDEO_DETAIL,
            {"aweme_id": str(aweme_id), "aid": COMMON_PARAMS["aid"]},
        )
        aweme = _dig(data, "aweme_detail")
        if not aweme:
            raise DouyinWebError(
                f"没取到作品 {aweme_id} 的详情",
                kind="upstream",
                hint="作品可能已被删除/设为私密，或 aweme_id 不对（分享链接里的那一长串数字才是）。",
            )
        picked = pick_video(aweme, with_stats=True)
        if not picked:
            raise DouyinWebError("作品详情里没有可用的视频信息", kind="parse", hint="接口结构可能已变。")
        picked["comments_count"] = _dig(aweme, "statistics", "comment_count", default=0)
        picked["share_count"] = _dig(aweme, "statistics", "share_count", default=0)
        picked["collect_count"] = _dig(aweme, "statistics", "collect_count", default=0)
        picked["music"] = _dig(aweme, "music", "title", default="")
        return picked

    def user_profile(self, sec_user_id: str) -> Dict[str, Any]:
        """取用户主页信息（昵称、粉丝、简介、是否已关注等）。"""
        data = self._request(PATH_USER_PROFILE, {"sec_user_id": str(sec_user_id)})
        user = data.get("user")
        if not isinstance(user, dict):
            raise DouyinWebError(
                f"没取到用户 {sec_user_id} 的资料",
                kind="upstream",
                hint="sec_user_id 要么抄错，要么该用户已注销/改名。可从搜索或作品详情里重新取一个。",
            )
        return pick_user(user)

    def user_videos(self, sec_user_id: str, max_cursor: int = 0, count: int = 20) -> Dict[str, Any]:
        """取用户作品的一页（游标翻页）。"""
        data = self._request(
            PATH_USER_POST,
            {"sec_user_id": str(sec_user_id), "max_cursor": max_cursor, "count": count},
        )
        items = data.get("aweme_list") or []
        videos = [v for v in (pick_video(i) for i in items if isinstance(i, dict)) if v]
        return {
            "videos": videos,
            "has_more": int(_dig(data, "has_more", default=0) or 0) == 1,
            "max_cursor": int(_dig(data, "max_cursor", default=0) or 0),
        }

    def user_videos_all(self, sec_user_id: str, limit: int = 60) -> List[Dict[str, Any]]:
        """自动翻页，最多取 `limit` 条。

        ★ 默认给上限而不是"拉到没有为止"：全量拉取是最容易触发风控的动作，
          而一次对话里通常只看最近几十条。
        """
        out: List[Dict[str, Any]] = []
        cursor = 0
        has_more = True
        while has_more and (limit <= 0 or len(out) < limit):
            page = self.user_videos(sec_user_id, cursor, min(20, max(1, limit - len(out)) if limit > 0 else 20))
            out.extend(page["videos"])
            has_more = bool(page["has_more"])
            cursor = int(page["max_cursor"])
            if has_more and (limit <= 0 or len(out) < limit):
                time.sleep(self.cfg.page_interval)
        return out[:limit] if limit > 0 else out

    def my_profile(self) -> Dict[str, Any]:
        """取当前登录账号自己的资料（不需要 sec_user_id）。"""
        data = self._request(PATH_MY_PROFILE, {})
        user = data.get("user")
        if not isinstance(user, dict):
            raise DouyinWebError(
                "没取到当前账号的资料（多半是登录态已失效）",
                kind="not_login",
                hint="请让用户重新扫码登录（douyin_account_login），再调本工具。",
            )
        return pick_user(user)

    # ── 分享链接 → 作品 id ──────────────────────────────────

    def resolve_aweme_id(self, share_text: str) -> str:
        """把一段分享文本/链接换成作品 id。

        ★ 只跟随跳转拿最终 URL，**不解析分享页 HTML**：抖音的
          `iesdouyin.com/share/video/...` 页面已经被风控保护
          （实测裸请求返回的是 JS 挑战页，`window._ROUTER_DATA` 早就不在里面了），
          所以"解析页面 JSON"那条老路现在是死的 —— 拿到 id 后走 `video_detail` 才有数据。
        """
        text = (share_text or "").strip()
        if not text:
            raise DouyinWebError("分享内容为空", kind="upstream", hint="把用户给的分享文本原样传进来。")

        url = extract_share_url(text)
        if not url:
            # 也可能用户直接给了 id
            aweme_id = extract_aweme_id(text)
            if aweme_id:
                return aweme_id
            raise DouyinWebError(
                f"这段文本里没有链接也没有作品 id：{text[:60]}",
                kind="upstream",
                hint="分享文本应形如「…复制打开抖音… https://v.douyin.com/xxxx/」。",
            )

        direct = extract_aweme_id(url)
        if direct and ("/video/" in url or "modal_id" in url):
            return direct

        final = self._follow(url)
        aweme_id = extract_aweme_id(final)
        if not aweme_id:
            raise DouyinWebError(
                f"跟随后拿到的是 {final}，里面没有作品 id",
                kind="upstream",
                hint="链接可能已过期，或这条分享不是单个作品（主页/直播/合集的链接解析不了）。",
            )
        return aweme_id

    def _follow(self, url: str) -> str:
        """跟随短链跳转，返回最终 URL（失败时把原 URL 原样返回，由上层去判有没有 id）。"""
        request = urllib.request.Request(url, headers={"User-Agent": self.cfg.user_agent}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as resp:
                return resp.geturl()
        except urllib.error.HTTPError as e:
            return e.geturl() or url
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DouyinWebError(
                f"打不开分享链接：{e}",
                kind="network",
                hint="分享链接要能访问到 douyin.com；内网机器若无外网出口，这条链路不可用。",
            ) from None

    # ── 元信息 ──────────────────────────────────────────────

    def sign_note(self) -> str:
        return sign_status()


def _decompress(raw: bytes, encoding: str) -> bytes:
    """解压响应体（抖音会按 Accept-Encoding 回 gzip/br）。

    ★ urllib 不会自动解压：不解的话 JSON 解析会拿到一堆乱码，
      表现为"接口改版了"，其实是我们自己没解开。
    """
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            return zlib.decompress(raw)
    except (OSError, zlib.error):
        return raw  # 解不开就按原样试解析（有些代理会撒谎）
    return raw  # br/zstd 不在标准库范围：抖音不要求时不会发


# ── 分享文本解析 ────────────────────────────────────────────
# 分享文本长这样：「7.32 复制打开抖音，看看【某某某的作品】… https://v.douyin.com/xxxx/」
_SHARE_URL_RE = re.compile(r"https?://[^\s，。、；）)】\]]+")
_AWEME_ID_RE = re.compile(r"\d{15,25}")


def extract_share_url(text: str) -> str:
    """从分享文本里抽第一个链接（抽不到返回空串）。"""
    m = _SHARE_URL_RE.search(text or "")
    return m.group(0) if m else ""


def extract_aweme_id(text: str) -> str:
    """从 URL 或纯文本里抽作品 id（抽不到返回空串）。"""
    for pattern in (r"/video/(\d{15,25})", r"modal_id=(\d{15,25})", r"/(\d{15,25})"):
        m = re.search(pattern, text or "")
        if m:
            return m.group(1)
    m = _AWEME_ID_RE.search(text or "")
    return m.group(1) if m else ""


# ── DTO 裁剪 ────────────────────────────────────────────────


def pick_video(item: Any, with_stats: bool = True) -> Dict[str, Any]:
    """作品 → 给模型看的那几个字段（**不返回上游原始结构**）。"""
    if not isinstance(item, dict):
        return {}
    aweme_id = str(_dig(item, "aweme_id", default="") or "")
    if not aweme_id:
        return {}
    author = _dig(item, "author", default={}) or {}
    video = _dig(item, "video", default={}) or {}
    stats = _dig(item, "statistics", default={}) or {}
    play = _first_url(_dig(video, "play_addr"))
    out: Dict[str, Any] = {
        "aweme_id": aweme_id,
        "desc": str(_dig(item, "desc", default="") or "").strip(),
        "create_time": _fmt_time(_dig(item, "create_time")),
        "author": {
            "nickname": str(_dig(author, "nickname", default="") or ""),
            "sec_user_id": str(_dig(author, "sec_uid", default="") or ""),
            "unique_id": str(_dig(author, "unique_id", default="") or ""),
        },
        "duration_ms": int(_dig(video, "duration", default=0) or 0),
        "cover_url": _first_url(_dig(video, "cover")),
        "download_url": _strip_watermark(play),
        "share_url": f"https://www.douyin.com/video/{aweme_id}",
    }
    if with_stats:
        out["stats"] = {
            "like": int(_dig(stats, "digg_count", default=0) or 0),
            "comment": int(_dig(stats, "comment_count", default=0) or 0),
            "share": int(_dig(stats, "share_count", default=0) or 0),
            "collect": int(_dig(stats, "collect_count", default=0) or 0),
        }
    return out


def pick_user(user: Any) -> Dict[str, Any]:
    """用户 → 裁剪后的资料。"""
    if not isinstance(user, dict):
        return {}
    return {
        "nickname": str(_dig(user, "nickname", default="") or ""),
        "sec_user_id": str(_dig(user, "sec_uid", default="") or ""),
        "unique_id": str(_dig(user, "unique_id", default="") or ""),  # 抖音号
        "signature": str(_dig(user, "signature", default="") or ""),
        "ip_location": str(_dig(user, "ip_location", default="") or ""),
        "gender": {0: "未知", 1: "男", 2: "女"}.get(int(_dig(user, "gender", default=0) or 0), "未知"),
        "follower_count": int(_dig(user, "follower_count", default=0) or 0),
        "following_count": int(_dig(user, "following_count", default=0) or 0),
        "total_favorited": int(_dig(user, "total_favorited", default=0) or 0),
        "aweme_count": int(_dig(user, "aweme_count", default=0) or 0),
        "avatar_url": _first_url(_dig(user, "avatar_168x168")) or _first_url(_dig(user, "avatar_300x300")),
        "profile_url": (
            "https://www.douyin.com/user/" + str(_dig(user, "sec_uid", default="") or "")
            if _dig(user, "sec_uid")
            else ""
        ),
    }


__all__ = [
    "DouyinWebClient",
    "DouyinWebError",
    "WebConfig",
    "COMMON_PARAMS",
    "DESKTOP_UA",
    "PATH_SEARCH",
    "PATH_VIDEO_DETAIL",
    "PATH_USER_PROFILE",
    "PATH_USER_POST",
    "PATH_MY_PROFILE",
    "pick_video",
    "pick_user",
    "extract_share_url",
    "extract_aweme_id",
]
