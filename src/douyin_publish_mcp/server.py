"""薄 MCP 服务：把 social-auto-upload 的抖音能力封成 5 个工具。

工具：`douyin_account_status` / `douyin_account_login` / `douyin_account_logout` /
`douyin_publish_video` / `douyin_publish_note`。两种传输共用这一份实现
（stdio 见本文件，streamable_http 见 `http_server.py`）。

## 为什么自己写协议，而不用 fastmcp

客户端（Dr.Q / 任意 MCP 宿主）走的是 MCP **stdio 传输**：一行一个 JSON-RPC 对象，
协议版本 `2024-11-05`（见 pm-web-app 的 `tool_client/mcp_session.rs`）。握手只用到
`initialize` / `notifications/initialized` / `tools/list` / `tools/call` 四个方法 ——
这点东西用标准库写就是几十行，换来的是**零三方依赖**：内网机器上没有 PyPI 也能起，
`uvx` 首次拉起不会卡在包解析上（对比 fastmcp 会拖进一整棵依赖树）。

## 发布为什么是"两步"

MCP 工具调用是**模型自己发起**的，没有任何人工卡点：一旦模型理解偏了，
"发一条抖音"就直接落到用户账号上，而且这类操作**不可撤销**（只能去创作者中心删）。
所以发布工具做成两步，且这两步都由模型驱动：

1. 不带 `confirm`（或 `confirm=false`）→ **只做本地预检，绝不碰账号**，
   返回一份"待发布计划" + `plan_id`；
2. 带上那次的 `plan_id` 与 `confirm=true` → 才真正执行。

`plan_id` 是计划内容的哈希（见 [`plan_fingerprint`]）：它同时挡住两件事 ——
"模型忘了先确认就直接发"和"确认的是 A、发出去的是 B"。

★ 这只约束**程序流程**，不是独立的人工审批。真正的把关在工具描述里（要求模型先把
  计划念给用户、拿到明确同意再调到第二步），以及平台侧的工具审批（若配置了）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__, sau
from .douyin_cred import default_account, resolve_credential
from .douyin_web import (
    DouyinWebClient,
    DouyinWebError,
    WebConfig,
    extract_aweme_id,
    extract_share_url,
)
from .login_session import LoginSessionManager
from .sau import SauConfig, SauError

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "douyin-publish-mcp"
# ★ 版本号只有一个来源（包的 `__version__`）：两处各写一份迟早分叉，
#   而"装的是哪一版"在商店、状态页、tools/list 的 serverInfo 里都靠它。
SERVER_VERSION = __version__

# 二维码图片回传上限：超过就不塞进 tool 结果（会白烧上下文，还会拖慢一次调用）
QRCODE_MAX_BYTES = 3 * 1024 * 1024

# ── 工具描述（写给模型看，不是文档里的装饰）────────────────────
# ★ 描述里必须出现"先给用户看、得到同意再确认"这句：模型只会读这里。
PUBLISH_RULE = (
    "【必须先确认】不传 confirm 调用时，本工具**不会发布任何东西**，只返回一份待发布计划；"
    "请把这份计划（账号、素材、标题、正文、标签、发布时间）原样念给用户，"
    "得到用户明确同意后，再用同一个 plan_id 加上 confirm=true 调用一次。"
    "不要自己替用户决定发布，也不要跳过确认直接传 confirm。"
)

# 读取类工具共用的账号参数（★ 做成可选：多数场景只有一个号，
#   每次都逼模型问用户"账号名是什么"是没必要的摩擦；默认取 SAU_ACCOUNT/DOUYIN_ACCOUNT，兜底 main）
_ACCOUNT_SCHEMA_READ = {
    "type": "string",
    "description": "账号名（social-auto-upload 里登录时用的名字）。不传则用默认账号（SAU_ACCOUNT，兜底 main）。",
}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "douyin_account_status",
        "description": (
            "查询某个抖音账号在 social-auto-upload 里的登录态（本地 cookie 是否还有效）。"
            "发布/登录前先查一次，避免发到一半才发现没登录。"
            "account 是登录时用的账号名（不是抖音昵称）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "账号名（social-auto-upload 里登录时用的名字）"},
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_account_login",
        "description": (
            "让用户扫码登录抖音。"
            "★ 会弹出一个**真浏览器窗口**：登录这一步必须用真窗口 —— 抖音的反自动化会挑无头浏览器，"
            "而且可能要求短信二次验证，那只能在窗口里手动输。"
            "二维码同时也会作为**图片**返回（和窗口里是同一个码），展示给用户扫也行。"
            "**扫码成功后窗口会自动关闭**（sau 存完 cookie 就关掉浏览器并退出，约 2 秒后消失）；"
            "若一直没人扫，窗口最多活 2 分钟也会自己关，不会一直挂在用户屏幕上。"
            "调用会等一段（wait_seconds）：扫得快就一次拿到结论；"
            "还在等就返回「会话仍在等待扫码」，此时**再调一次本工具**即可继续查，不要重复发起新登录。"
            "若发布时触发短信二次验证，把验证码写进项目根目录的 verify_code.txt 再重试发布。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "账号名（自定义，用于区分多个抖音号）"},
                "headed": {
                    "type": "boolean",
                    "description": "是否弹出浏览器窗口。默认 true：登录必须用真窗口（抖音的反自动化会挑无头浏览器，且可能要输短信验证码）；扫码成功后窗口自动关闭。只在没有桌面会话（CI/服务器）时才传 false。",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "本次调用最多等多少秒（默认 90，上限 600）。超时不中断登录：会话仍在后台等扫码。",
                },
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_account_logout",
        "description": (
            "重置某个账号的登录态（相当于小红书 MCP 的 delete_cookies）。"
            "本服务**只删一个文件**：<项目>/cookies/douyin_<账号>.json —— 那是 sau 的登录凭据。"
            "★ 两步：不传 confirm 时只回报「将要删除的路径」，什么都不删；"
            "用户明确同意后才传 confirm=true。删完该账号变回未登录，发布前要重新扫码。"
            "注意这是「退出登录」不是「卸载」：项目目录、素材、已发布的作品都不受影响。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "账号名"},
                "confirm": {"type": "boolean", "description": "用户已明确同意删除凭据时传 true"},
            },
            "required": ["account"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_publish_video",
        "description": "发布一条抖音视频。" + PUBLISH_RULE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "账号名"},
                "file": {"type": "string", "description": "视频文件路径（必须在素材目录内，可用相对路径）"},
                "title": {"type": "string", "description": "作品标题（≤30 字，单行）"},
                "description": {"type": "string", "description": "正文/描述（可选）"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "话题标签（可选），如 运动、训练",
                },
                "schedule": {
                    "type": "string",
                    "description": "定时发布时间（可选），格式 'YYYY-MM-DD HH:MM'（本地时间）；不传=立即发布",
                },
                "thumbnail_portrait": {
                    "type": "string",
                    "description": "竖版封面 3:4（可选，素材目录内的路径）。与 landscape 可同时给。",
                },
                "thumbnail_landscape": {
                    "type": "string",
                    "description": "横版封面 4:3（可选，素材目录内的路径）。",
                },
                "product_link": {
                    "type": "string",
                    "description": "带货商品链接（可选）。★ 必须与 product_title 一起给，只给一个平台不认。",
                },
                "product_title": {"type": "string", "description": "带货商品标题（可选，与 product_link 成对）"},
                "declaration": {
                    "type": "string",
                    "description": "作品自主声明（可选），要填平台给出的**原样文案**（如「虚构演绎，仅供娱乐」）；不确定就别传。",
                },
                "collection": {
                    "type": "string",
                    "description": "加入已存在的合集名（可选）。合集必须已存在，否则脚本找不到就跳过。",
                },
                "plan_id": {"type": "string", "description": "上一步预检返回的 plan_id（确认发布时必填）"},
                "confirm": {"type": "boolean", "description": "用户已明确同意发布时传 true"},
            },
            "required": ["account", "file", "title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_publish_note",
        "description": "发布一条抖音图文（多图 + 标题 + 正文）。" + PUBLISH_RULE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "账号名"},
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "图片路径列表（必须在素材目录内），抖音最多 35 张、不支持 GIF",
                },
                "title": {"type": "string", "description": "图文标题（≤30 字，单行）"},
                "note": {"type": "string", "description": "图文正文（可选）。与 note_file 只能给一个。"},
                "note_file": {
                    "type": "string",
                    "description": "把正文放在文件里（可选，素材目录内的 .txt/.md 路径）。长正文建议用这个。",
                },
                "bgm": {"type": "string", "description": "背景音乐**搜索词**（可选），如「轻快 纯音乐」；不是文件路径。"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "话题标签（可选）"},
                "schedule": {"type": "string", "description": "定时发布时间（可选），格式 'YYYY-MM-DD HH:MM'"},
                "plan_id": {"type": "string", "description": "上一步预检返回的 plan_id（确认发布时必填）"},
                "confirm": {"type": "boolean", "description": "用户已明确同意发布时传 true"},
            },
            "required": ["account", "images", "title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_search_videos",
        "description": (
            "按关键词搜索抖音视频（需要已登录）。返回每条作品的 aweme_id、文案、作者"
            "（昵称 + sec_user_id）、互动数据和**无水印直链**。"
            "★ 直链有时效（几小时），要保存就当场下载；本工具只返回地址，不落盘。"
            "搜到的 aweme_id 可以喂给 douyin_video_detail 看详情，sec_user_id 可以喂给 douyin_user_profile。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "count": {"type": "number", "description": "返回条数（默认 10，最多 20）"},
                "sort": {
                    "type": "string",
                    "enum": ["general", "like", "latest"],
                    "description": "排序：general=综合（默认）、like=最多点赞、latest=最新",
                },
                "publish_time": {
                    "type": "string",
                    "enum": ["all", "day", "week", "half_year"],
                    "description": "发布时间范围：all=不限（默认）、day=一天内、week=一周内、half_year=半年内",
                },
                "account": _ACCOUNT_SCHEMA_READ,
            },
            "required": ["keyword"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_video_detail",
        "description": (
            "取单条抖音作品的详情：文案、作者、发布时间、互动数据、封面、"
            "**无水印播放直链**、以及分享页地址（需要已登录）。"
            "aweme_id 从 douyin_search_videos / douyin_user_profile / 分享链接里拿。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "aweme_id": {"type": "string", "description": "作品 id（15~25 位数字）"},
                "account": _ACCOUNT_SCHEMA_READ,
            },
            "required": ["aweme_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_user_profile",
        "description": (
            "看某个抖音用户的主页信息：昵称、抖音号、简介、IP 属地、性别、粉丝/关注/获赞/作品数，"
            "并按需返回他最近的作品列表（需要已登录）。"
            "sec_user_id 从 douyin_search_videos / douyin_video_detail 的作者字段里拿（形如 MS4wLjABAAAA…）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sec_user_id": {"type": "string", "description": "用户安全 id（MS4wLjABAAAA… 开头）"},
                "include_videos": {
                    "type": "boolean",
                    "description": "是否同时返回他的作品列表（默认 true；只看资料传 false 更快）",
                },
                "limit": {"type": "number", "description": "作品条数上限（默认 20，最多 100）"},
                "account": _ACCOUNT_SCHEMA_READ,
            },
            "required": ["sec_user_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_my_profile",
        "description": (
            "看**当前登录账号自己**的主页信息与作品列表（不需要传 sec_user_id）。"
            "适合回答「我发了多少条、最近的播放/点赞如何」「这个号登录的是谁」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_videos": {"type": "boolean", "description": "是否返回自己的作品列表（默认 true）"},
                "limit": {"type": "number", "description": "作品条数上限（默认 20，最多 100）"},
                "account": _ACCOUNT_SCHEMA_READ,
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "douyin_parse_share_link",
        "description": (
            "把用户给你的抖音分享文本/链接（形如「7.32 复制打开抖音… https://v.douyin.com/xxxx/」）"
            "解析成作品详情 + 无水印直链。内部就是「跟随短链拿 aweme_id → 读详情」，"
            "所以同样需要已登录；短链打不开时会退化成提示用户直接给作品 id。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "share_text": {"type": "string", "description": "分享文本或链接（原样贴进来即可）"},
                "account": _ACCOUNT_SCHEMA_READ,
            },
            "required": ["share_text"],
            "additionalProperties": False,
        },
    },
]

# 排序 / 时间范围：用**字符串枚举**而不是 0/1/2 数字（数字是上游口径，模型很容易记串）
SORT_MAP = {"general": 0, "like": 1, "latest": 2}
PUBLISH_TIME_MAP = {"all": 0, "day": 1, "week": 7, "half_year": 180}

MAX_NOTE_IMAGES = 35


# ── 计划指纹（两步确认的核心）────────────────────────────────


def plan_fingerprint(plan: Dict[str, Any]) -> str:
    """把"要发布什么"压成一个短指纹。

    ★ 只哈希**决定发布内容**的字段（账号/类型/素材/标题/正文/标签/时间），
      不含 confirm 之类的调用态 —— 否则模型第二次调用时字段一变指纹就不同，
      确认永远对不上。
    """
    material = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# ── 工具实现 ────────────────────────────────────────────────


def _text(content: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": content}], "isError": is_error}


def _image_item(path: Path) -> Optional[Dict[str, Any]]:
    """把本地图片读成 MCP 的 image content（客户端会把它落盘并在卡片里显示）。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > QRCODE_MAX_BYTES:
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def tool_account_status(
    cfg: SauConfig, args: Dict[str, Any], sessions: Optional["LoginSessionManager"] = None
) -> Dict[str, Any]:
    """查登录态。

    ★ 判据以 `sau douyin check` 的**退出码**为准（0=valid / 1=invalid，见 sau_cli.py），
      文本只在退出码说不出话时兜底 —— 早先版本纯靠文本里找「未登录」三个字，
      CLI 一改文案就会静默变成"猜不出来"。
    """
    account = _require_str(args, "account")
    sessions = sessions or LoginSessionManager(cfg)
    rec = sessions.check(account)
    if rec.skipped:
        # 「没查」不等于「没登录」：如实说跳过，并告诉模型下一步
        return _text(
            f"这次**没有检查**：{rec.output}\n"
            f"请让用户先用 douyin_account_login 扫码（二维码以图片返回，不弹浏览器窗口），"
            f"扫完会自动检查一次；也可以稍后再调本工具。",
            is_error=False,
        )
    state = rec.logged_in
    if state is True:
        summary = f"账号「{account}」已登录，可以发布。"
    elif state is False:
        summary = (
            f"账号「{account}」**未登录**（本地 cookie 不存在或已失效）。\n"
            f"下一步：调用 douyin_account_login（account=\"{account}\"）让用户扫码。"
        )
    elif rec.ok:
        summary = (
            "命令执行成功，但退出码与输出都没给出明确的登录态 —— 请让用户到创作者中心确认，"
            "或直接调一次 douyin_account_login 重新扫码。"
        )
    else:
        summary = (
            f"检查失败（退出码不是 0/1）。先看下面的输出：若是找不到命令，"
            f"说明本服务的 SAU_CMD / SAU_DIR 没配对；若是浏览器/网络错误，多半需要重新登录。"
        )
    argv = " ".join(cfg.command(sau.check_args(account)))
    return _text(
        f"{summary}\n\n"
        f"（判据：`sau douyin check` 的退出码优先（0=valid / 1=invalid），输出文本兜底）\n"
        f"logged_in={state}\n$ {argv}\n{rec.output or '(无输出)'}",
        is_error=not rec.ok and state is None,
    )


def tool_account_login(
    cfg: SauConfig, args: Dict[str, Any], sessions: Optional["LoginSessionManager"] = None
) -> Dict[str, Any]:
    """扫码登录：发起（或复用）一个待扫码会话，等一小段，再把状态回报。

    ★ 为什么不一次性阻塞到底：登录要等用户拿手机扫码（几十秒到几分钟），
      同步等会把客户端卡住；而且中断了就没有任何地方能查到"刚才那次登录怎么了"。
      会话留在进程里（见 login_session.py），用户可以再调一次接着查。
    """
    account = _require_str(args, "account")
    # ★ 默认**有头**（真窗口）：登录这一步抖音的反自动化会挑无头浏览器，
    #   而且可能要求短信二次验证 —— 那只能在窗口里手动输，无头做不到。
    #   窗口不会久留：sau 扫码成功后存完 cookie 就关掉浏览器并退出进程
    #   （见 douyin_cookie_gen 的 finally 里 browser.close()），没人扫也最多等 2 分钟。
    headed = bool(args.get("headed", True))
    wait_seconds = _bounded_float(args.get("wait_seconds"), default=90.0, lo=0.0, hi=600.0)
    sessions = sessions or LoginSessionManager(cfg)

    snap = sessions.start_login(account, headed=headed)
    if wait_seconds > 0 and snap.get("running"):
        snap = sessions.wait(wait_seconds)

    content: List[Dict[str, Any]] = []
    lines: List[str] = []
    if snap.get("running"):
        lines.append(
            f"账号「{account}」的登录已发起，**正在等扫码**（已等 {snap.get('elapsedSec')}s）。\n"
            "浏览器窗口已经弹在用户的屏幕上了，让用户直接在窗口里扫码。\n"
            + (
                "（同一个二维码也附在下面这张图里；不方便看窗口时扫这张也行。）\n"
                if snap.get("qrPath")
                else ""
            )
            + "**扫完窗口会自动关闭**，不用手动关。"
            f"扫完后**再调一次本工具**（或 douyin_account_status）确认结果 —— "
            f"重复调用不会另开一个登录会话，只是继续查这一个。"
        )
    elif snap.get("succeeded"):
        state = snap.get("loggedIn")
        if state is True:
            lines.append(f"账号「{account}」扫码登录成功，现在可以发布。")
        elif state is False:
            lines.append(
                "登录流程已结束，但紧接着的检查仍是**未登录** —— 多半没扫上，或扫的是别的账号。\n"
                "建议：核对 account 是否与项目里登录时用的名字一致，然后重新调本工具。"
            )
        else:
            lines.append(
                "登录流程已结束，但登录态没判出来（既有可能是成功也有可能是页面没到位）。\n"
                "请让用户到创作者中心确认，或再调一次 douyin_account_status。"
            )
    else:
        lines.append(
            f"登录流程失败（退出码 {snap.get('exitCode')}）。{snap.get('error') or ''}\n"
            f"常见原因：浏览器运行时没装好（patchright install chromium）、"
            f"或账号名与项目里登录时用的不一致。"
        )
        lines.append(f"也可让用户在本机终端手动执行：{_manual_login_cmd(cfg, account)}")

    qr_path = snap.get("qrPath") or ""
    if qr_path:
        # 少数平台/版本会把二维码落盘；有就顺手给用户一张图（不给也没有损失）
        item = _image_item(Path(qr_path))
        if item:
            content.append(item)
            lines.append("检测到二维码图片已落盘，已作为图片返回（**请直接展示给用户**，不要只念路径）。")
    if cfg.verify_code_file:
        lines.append(f"若登录/发布要求短信验证：把验证码写进 {cfg.verify_code_file} 后重试。")
    lines.append(f"最近输出：\n{snap.get('output') or '(无输出)'}")

    content.append({"type": "text", "text": "\n\n".join(lines)})
    return {"content": content, "isError": False}


def tool_account_logout(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    """重置登录态（= 删除该账号的凭据文件）。

    ★ 两步门禁与发布同源：不传 confirm 只回报「将要删除什么」，一个字节都不动。
      这是本服务唯一的**删除**操作，所以宁可多问一次。
    """
    account = _require_str(args, "account")
    confirm = bool(args.get("confirm", False))
    plan = sau.logout(cfg, account, confirm=confirm)
    lines = [plan.describe()]
    if plan.existed and not plan.deleted:
        lines.append("用户明确同意后，用同一个 account 加上 confirm=true 再调用一次，才会真的删除。")
    if plan.deleted:
        # ★ 不再重复一遍"现在是未登录"：`describe()` 里已经说了（实测输出会连说两遍）
        lines.append("下次发布前：先调 douyin_account_login 让用户重新扫码。")
    else:
        lines.append("（这是「退出登录」，不是卸载：项目目录、素材、已发布的作品都不受影响。）")
    return _text("\n\n".join(lines), is_error=False)


def _publish_tool(cfg: SauConfig, args: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """发布工具的两个阶段（预检 / 执行）共用实现。

    `kind`: "video" | "note"
    """
    confirm = bool(args.get("confirm", False))
    plan_id = str(args.get("plan_id", "") or "").strip()

    # ── 阶段一：把参数变成一份"可信的计划"（顺带把该报的错都报掉）──
    try:
        if kind == "video":
            account = _require_str(args, "account")
            file_path = sau.resolve_media_path(cfg, _require_str(args, "file"))
            title = _require_str(args, "title")
            description = str(args.get("description", "") or "")
            schedule = str(args.get("schedule", "") or "")
            # ★ 封面也走素材白名单：它同样是"把本机文件交给抖音"，只是不叫"主素材"而已
            thumbs: Dict[str, str] = {}
            for key in ("thumbnail_portrait", "thumbnail_landscape"):
                raw = _opt_str(args, key)
                if raw:
                    thumbs[key] = str(sau.resolve_media_path(cfg, raw))
            product_link = _opt_str(args, "product_link")
            product_title = _opt_str(args, "product_title")
            declaration = _opt_str(args, "declaration")
            collection = _opt_str(args, "collection")
            plan: Dict[str, Any] = {"kind": kind, "account": account, "file": str(file_path)}
            if description:
                plan["description"] = description
            if schedule:
                plan["schedule"] = sau.normalize_schedule(schedule)
            tags = args.get("tags")
            if tags:
                plan["tags"] = tags
            plan.update(thumbs)
            if product_link:
                plan["product_link"] = product_link
                plan["product_title"] = product_title
            if declaration:
                plan["declaration"] = declaration
            if collection:
                plan["collection"] = collection
            argv_args = sau.upload_video_args(
                account,
                str(file_path),
                title,
                description,
                tags,
                schedule,
                headless=cfg.headless,
                thumbnail=thumbs.get("thumbnail_portrait", ""),
                thumbnail_portrait=thumbs.get("thumbnail_portrait", ""),
                thumbnail_landscape=thumbs.get("thumbnail_landscape", ""),
                product_link=product_link,
                product_title=product_title,
                declaration=declaration,
                collection=collection,
            )
            # 标题进计划（`upload_video_args` 会顺手校验长度/单行）
            plan["title"] = title
        else:
            account = _require_str(args, "account")
            raw_images = args.get("images")
            if not isinstance(raw_images, list) or not raw_images:
                raise SauError("images 必须是非空数组（至少一张图片）")
            if len(raw_images) > MAX_NOTE_IMAGES:
                raise SauError(
                    f"图片 {len(raw_images)} 张，超过抖音图文上限 {MAX_NOTE_IMAGES} 张，请先筛选。"
                )
            images = [str(sau.resolve_media_path(cfg, str(p))) for p in raw_images]
            title = _require_str(args, "title")
            note = str(args.get("note", "") or "")
            note_file = _opt_str(args, "note_file")
            note_file_path = str(sau.resolve_media_path(cfg, note_file)) if note_file else ""
            bgm = _opt_str(args, "bgm")
            schedule = str(args.get("schedule", "") or "")
            plan = {"kind": kind, "account": account, "images": images}
            if note:
                plan["note"] = note
            if note_file_path:
                plan["note_file"] = note_file_path
            if bgm:
                plan["bgm"] = bgm
            if schedule:
                plan["schedule"] = sau.normalize_schedule(schedule)
            tags = args.get("tags")
            if tags:
                plan["tags"] = tags
            argv_args = sau.upload_note_args(
                account,
                images,
                title,
                note,
                tags,
                schedule,
                headless=cfg.headless,
                note_file=note_file_path,
                bgm=bgm,
            )
            plan["title"] = title
    except SauError as e:
        return _text(f"参数有问题，**没有发任何东西**：\n{e}", True)

    fingerprint = plan_fingerprint(plan)

    # ── 阶段二：没确认 → 只交计划；确认了 → 才执行 ──────────────
    if not confirm:
        label = "视频" if kind == "video" else "图文"
        pretty = json.dumps(plan, ensure_ascii=False, indent=2)
        return _text(
            f"这是**待发布的{label}计划**（本地预检通过，**还没有发给抖音**）：\n"
            f"```json\n{pretty}\n```\n\n"
            f"请把上面这份计划逐项念给用户确认（尤其是账号、素材、标题、正文、发布时间）。\n"
            f"用户明确同意后，用 plan_id=\"{fingerprint}\" 且 confirm=true 再调用一次；\n"
            f"任何一项要改，就重新调用本工具生成新计划（不要自己改 plan_id）。",
        )

    if plan_id != fingerprint:
        return _text(
            "确认信息对不上，**没有发任何东西**。\n"
            f"本次参数算出的 plan_id 是 {fingerprint}，而传进来的是 {plan_id or '(空)'}。\n"
            "这通常意味着「用户同意的那份内容」和「这次要发的内容」不是同一份 —— "
            "请不要重试，先把最新参数重新预检一次（不带 confirm），拿新的 plan_id 再让用户确认。",
            True,
        )

    # 真到这一步：执行 CLI
    result = sau.run(cfg, argv_args, timeout=cfg.timeout)
    text = sau.redact(result.tail())
    head = f"$ {' '.join(result.argv)}"
    if result.ok:
        body = (
            "发布命令执行成功（CLI 未报错）。\n"
            "★ 这不等于作品已公开可见：抖音侧还有审核。请让用户到创作者中心「作品管理」核对，"
            "不要把「命令成功」说成「已发布上线」。"
        )
    elif result.timed_out:
        body = result.hint
    else:
        body = (
            f"发布失败（退出码 {result.exit_code}）。常见原因：cookie 失效（先跑 "
            f"douyin_account_status / douyin_account_login）、素材格式不被接受、"
            f"或触发了短信二次验证（把验证码写进 verify_code_file 后重试）。"
        )
        if cfg.verify_code_file:
            body += f"\n短信验证码文件：{cfg.verify_code_file}"
    return _text(f"{body}\n\n{head}\n{text or '(无输出)'}", is_error=not result.ok)


# ── 读取工具（HTTP 直连；登录态与发布共用 sau 的凭据文件）──────────
#
# ★ 与发布工具的分工：发布**动账号**，所以有 confirm 门禁；读取**只看**，
#   所以不设门禁 —— 但也因此对失败要说得更清楚（空结果 vs 被风控，见 DouyinWebError.kind）。
# ★ 每个调用新建客户端：cookie 是会被重新扫码替换的，客户端跟着请求走最不容易出脏状态。


def _read_client(cfg: SauConfig, args: Dict[str, Any]) -> DouyinWebClient:
    account = _opt_str(args, "account")
    return DouyinWebClient(resolve_credential(cfg, account), WebConfig())


def _web_fail(e: DouyinWebError) -> Dict[str, Any]:
    return _text(f"读取失败（{e.kind}）：{e.describe()}", True)


def _bounded_int(value: Any, default: int, lo: int, hi: int) -> int:
    """把模型给的数字夹到区间内（越界不报错，直接夹住：条数不该让调用失败）"""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _enum_str(value: Any, table: Dict[str, Any], default: str) -> str:
    """字符串枚举：给不在表里的值时回落到默认（模型偶尔会自创一个词）"""
    key = str(value or "").strip().lower()
    return key if key in table else default


def _render_video(index: int, video: Dict[str, Any]) -> str:
    """一条作品的紧凑文本。★ 不用整段 JSON：字段固定、模型读文本比读 JSON 更少抄错。"""
    author = video.get("author") or {}
    stats = video.get("stats") or {}
    lines = [f"{index}. {video.get('desc') or '(无文案)'}"]
    lines.append(f"   aweme_id：{video.get('aweme_id')}")
    lines.append(
        f"   作者：{author.get('nickname') or '(未知)'}"
        f"｜sec_user_id：{author.get('sec_user_id') or '(无)'}"
    )
    if stats:
        lines.append(
            f"   数据：赞 {stats.get('like', 0)}｜评 {stats.get('comment', 0)}"
            f"｜转 {stats.get('share', 0)}｜藏 {stats.get('collect', 0)}"
        )
    if video.get("create_time"):
        seconds = round((video.get("duration_ms") or 0) / 1000, 1)
        lines.append(f"   发布：{video['create_time']}（时长 {seconds}s）")
    if video.get("download_url"):
        lines.append(f"   无水印直链（几小时后失效）：{video['download_url']}")
    return "\n".join(lines)


def _render_videos(videos: List[Dict[str, Any]]) -> str:
    return "\n\n".join(_render_video(i + 1, v) for i, v in enumerate(videos))


def _render_user(user: Dict[str, Any]) -> str:
    lines = [
        f"昵称：{user.get('nickname')}｜抖音号：{user.get('unique_id') or '(未设置)'}",
        f"sec_user_id：{user.get('sec_user_id')}",
        f"粉丝 {user.get('follower_count')}｜关注 {user.get('following_count')}"
        f"｜获赞 {user.get('total_favorited')}｜作品 {user.get('aweme_count')}"
        f"｜性别 {user.get('gender')}",
    ]
    if user.get("signature"):
        lines.append(f"简介：{user['signature']}")
    if user.get("ip_location"):
        lines.append(f"IP 属地：{user['ip_location']}")
    if user.get("profile_url"):
        lines.append(f"主页：{user['profile_url']}")
    return "\n".join(lines)


def tool_search_videos(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    keyword = _require_str(args, "keyword")
    count = _bounded_int(args.get("count"), 10, 1, 20)
    sort = _enum_str(args.get("sort"), SORT_MAP, "general")
    publish_time = _enum_str(args.get("publish_time"), PUBLISH_TIME_MAP, "all")
    try:
        client = _read_client(cfg, args)
        videos = client.search_videos(
            keyword,
            count=count,
            sort_type=SORT_MAP[sort],
            publish_time=PUBLISH_TIME_MAP[publish_time],
        )
    except SauError as e:
        return _text(f"读取失败：{e}", True)
    except DouyinWebError as e:
        return _web_fail(e)

    if not videos:
        return _text(
            f"关键词「{keyword}」搜到 0 条。\n"
            "这不一定出错：① 换更宽的关键词 ② 把 publish_time 放宽到 all "
            "③ 若反复为空，先调 douyin_account_status 确认登录态（空结果也可能是 cookie 过期）。",
        )
    return _text(
        f"关键词「{keyword}」搜到 {len(videos)} 条（{client.sign_note()}）：\n\n"
        + _render_videos(videos)
        + "\n\n下一步：要某条的详情/无水印直链用 douyin_video_detail(aweme_id)；"
        "要看某个作者的主页用 douyin_user_profile(sec_user_id)。",
    )


def tool_video_detail(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    raw = _require_str(args, "aweme_id")
    # 容错：模型有时会把整条链接塞进 aweme_id
    aweme_id = extract_aweme_id(raw) or raw
    try:
        client = _read_client(cfg, args)
        video = client.video_detail(aweme_id)
    except SauError as e:
        return _text(f"读取失败：{e}", True)
    except DouyinWebError as e:
        return _web_fail(e)
    return _text(_render_video(1, video) + f"\n\n（{client.sign_note()}）")


def tool_user_profile(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    sec_user_id = _require_str(args, "sec_user_id")
    include_videos = bool(args.get("include_videos", True))
    limit = _bounded_int(args.get("limit"), 20, 1, 100)
    try:
        client = _read_client(cfg, args)
        user = client.user_profile(sec_user_id)
        videos = client.user_videos_all(sec_user_id, limit) if include_videos else []
    except SauError as e:
        return _text(f"读取失败：{e}", True)
    except DouyinWebError as e:
        return _web_fail(e)

    text = _render_user(user)
    if include_videos:
        text += f"\n\n最近作品（最多 {limit} 条，实际 {len(videos)} 条）：\n\n"
        text += _render_videos(videos) if videos else "(这个号没有取到作品)"
    return _text(text)


def tool_my_profile(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    include_videos = bool(args.get("include_videos", True))
    limit = _bounded_int(args.get("limit"), 20, 1, 100)
    try:
        client = _read_client(cfg, args)
        user = client.my_profile()
        # ★ 自己的作品列表走同一个 user_videos 接口：/profile/self/ 只给资料，
        #   而"我发了哪些"是用户最常问的一句，所以这里顺手拉一次。
        videos = (
            client.user_videos_all(user["sec_user_id"], limit)
            if include_videos and user.get("sec_user_id")
            else []
        )
    except SauError as e:
        return _text(f"读取失败：{e}", True)
    except DouyinWebError as e:
        return _web_fail(e)

    text = "当前登录账号：\n\n" + _render_user(user)
    if include_videos:
        text += f"\n\n最近作品（最多 {limit} 条，实际 {len(videos)} 条）：\n\n"
        text += _render_videos(videos) if videos else "(没有取到作品)"
    return _text(text)


def tool_parse_share_link(cfg: SauConfig, args: Dict[str, Any]) -> Dict[str, Any]:
    share_text = _require_str(args, "share_text")
    try:
        client = _read_client(cfg, args)
        aweme_id = client.resolve_aweme_id(share_text)
        video = client.video_detail(aweme_id)
    except SauError as e:
        return _text(f"读取失败：{e}", True)
    except DouyinWebError as e:
        return _web_fail(e)
    return _text(
        f"从分享内容里解析到作品 {aweme_id}：\n\n" + _render_video(1, video) + f"\n\n（{client.sign_note()}）"
    )


# ── MCP 协议层 ─────────────────────────────────────────────


class Server:
    """Minimal MCP server（stdio，一行一个 JSON-RPC 对象）。

    ★ stdout 只允许出现协议消息：任何调试输出都必须走 stderr，
      否则宿主解析 JSON 会立刻失败（这条最容易在加日志时踩到）。
    """

    def __init__(self, cfg: Optional[SauConfig] = None, out=None, err=None):
        self.cfg = cfg or SauConfig.from_env()
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        # ★ 登录会话与"最近一次 check"是**进程级共享**的：HTTP 形态下状态页与工具
        #   看到的是同一份状态（同一个 Server 实例），不会出现"页面说在等扫码、
        #   工具说没有会话"这种自相矛盾。
        self.sessions = LoginSessionManager(self.cfg)
        self.tools: Dict[str, Callable[[SauConfig, Dict[str, Any]], Dict[str, Any]]] = {
            "douyin_account_status": lambda c, a: tool_account_status(c, a, self.sessions),
            "douyin_account_login": lambda c, a: tool_account_login(c, a, self.sessions),
            "douyin_account_logout": tool_account_logout,
            "douyin_publish_video": lambda c, a: _publish_tool(c, a, "video"),
            "douyin_publish_note": lambda c, a: _publish_tool(c, a, "note"),
            # 读取：不吃 confirm（只看不动账号），但每条失败都要能指到"下一步做什么"
            "douyin_search_videos": tool_search_videos,
            "douyin_video_detail": tool_video_detail,
            "douyin_user_profile": tool_user_profile,
            "douyin_my_profile": tool_my_profile,
            "douyin_parse_share_link": tool_parse_share_link,
        }

    # 请求 → 响应（返回 None 表示这是通知，不需要回）
    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if req_id is None:  # 通知（notifications/*）
            return None

        if method == "initialize":
            return _ok(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return _ok(req_id, {})
        if method == "tools/list":
            return _ok(req_id, {"tools": TOOLS})
        if method == "tools/call":
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            fn = self.tools.get(name)
            if fn is None:
                return _err(req_id, -32602, f"未知工具：{name}")
            if not isinstance(arguments, dict):
                return _err(req_id, -32602, "arguments 必须是对象")
            try:
                return _ok(req_id, fn(self.cfg, arguments))
            except SauError as e:
                # 参数/环境问题：当成工具错误回给模型（它要转述给用户去改配置）
                return _ok(req_id, _text(str(e), True))
            except Exception as e:  # noqa: BLE001 —— 兜底：别让宿主看到"服务崩了"
                print(f"[{SERVER_NAME}] 工具 {name} 异常: {e!r}", file=self.err, flush=True)
                return _ok(req_id, _text(f"工具内部错误：{e!r}", True))
        return _err(req_id, -32601, f"不支持的方法：{method}")

    def serve(self, stdin=None) -> None:
        stdin = stdin or sys.stdin
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                print(f"[{SERVER_NAME}] 收到非 JSON 行，已忽略", file=self.err, flush=True)
                continue
            response = self.handle(request)
            if response is not None:
                self.out.write(json.dumps(response, ensure_ascii=False) + "\n")
                self.out.flush()


def _ok(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _require_str(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SauError(f"缺少必填参数 {key}")
    return value.strip()


def _opt_str(args: Dict[str, Any], key: str) -> str:
    """可选字符串参数：没给 / 给空 / 给 null 都算没给（模型很爱传空串）。"""
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bounded_float(value: Any, default: float, lo: float, hi: float) -> float:
    """把模型给的数字夹到合理区间：越界不报错，直接夹住（等待时长不该让调用失败）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


def _manual_login_cmd(cfg: SauConfig, account: str) -> str:
    """给用户手敲的登录命令（模型可以把这行原样转述出去）"""
    # ★ 这里是**人工排查**路径（把命令原样交给用户，让他在自己终端里跑）：
    #   故意保留 --headed —— 那时用户就在终端前，直接看到窗口里的二维码，
    #   比让他去目录里翻二维码图片直观。工具走的默认路径是无头。
    return " ".join(cfg.command(sau.login_args(account, headed=True)))


def main() -> int:
    cfg = SauConfig.from_env()
    print(
        f"[{SERVER_NAME}] 就绪：SAU_CMD={cfg.cmd or '(未设置)'} SAU_DIR={cfg.project_dir or '(未设置)'} "
        f"素材目录={cfg.media_dir or '(未设置)'} 超时={cfg.timeout}s",
        file=sys.stderr,
        flush=True,
    )
    Server(cfg).serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
