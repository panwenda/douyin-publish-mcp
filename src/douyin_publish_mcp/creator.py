"""创作者中心自动化：登录 / 账号检查 / 发布（**自带，不再依赖外部 CLI**）。

## 出处与归属

本模块的**流程知识与选择器**参考了 social-auto-upload（MIT）里经过真机打磨的抖音实现
（`uploader/douyin_uploader/main.py`）——封面弹窗里两个同名上传框、抖音自定义组件
"click 不抛异常也不生效"、话题下拉残留浮层挡住后续点击等等，都是它踩出来的坑。
代码按本项目的结构重写（helper 进程 + 一行 JSON 协议 + 我们的错误分类），
跟随本项目的 Apache-2.0；参考来源已在 NOTICE 里署名。

## 跑在哪里

**helper 进程里**（`douyin-publish-mcp --creator-helper <spec.json>`），与浏览器通道同一套路：
主进程只负责拼 spec、起进程、读一行 JSON。好处是 patchright（约 100MB）按需取、
不进 exe，而且自动化崩了只崩一个子进程。

所以本模块**不做 import 期的重依赖**：patchright 只在函数里 import，
主进程（服务本身）没有驱动也能正常启动、只报"驱动没就位"。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CREATOR_HOME = "https://creator.douyin.com/"
CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"

#: 登录页的"我还没登录"标志：这些还在，就说明没进去
_LOGIN_MARKERS = ("扫码登录", "手机号登录", "二维码失效")

#: 二维码图片的候选选择器（抖音改过版，按命中率从高到低兜底）
_QR_SELECTORS = (
    'div#animate_qrcode_container img[src^="data:image"]',
    'div[class*="animate_qrcode_container"] img[src^="data:image"]',
    'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
    'img[aria-label="二维码"]',
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fail(kind: str, message: str, hint: str = "", **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": False, "error": {"kind": kind, "message": message, "hint": hint}}
    # ★ state 默认就是失败的种类（timeout / no_browser / not_logged_in…）：
    #   状态页与工具都靠它说"现在卡在哪一步"，不该因为失败就变成空字符串。
    payload.setdefault("state", kind)
    payload.update(extra)
    return payload


class CreatorError(RuntimeError):
    """自动化过程中的可读错误（会原样写进给模型的文本里）。"""


# ── 浏览器 ───────────────────────────────────────────────────


async def launch_context(playwright, channel: str, headless: bool, storage_state: str = ""):
    """开一个浏览器上下文。

    ★ 默认用**系统 Chrome**（channel="chrome"）：装个 Chrome 是常规操作，
      而 patchright 自带的 chromium 要另下约 170MB —— 与"exe 保持小 + 按需取"冲突。
    ★ storage_state 给了就带上登录态；文件不存在不该崩在这里（调用方负责先准备好）。
    """
    kwargs: Dict[str, Any] = {
        "headless": headless,
        "channel": channel or "chrome",
        # 与参考实现一致的常规加固：关掉自动化提示、容器里没有 sandbox 也能跑
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
    try:
        browser = await playwright.chromium.launch(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 启动失败几乎都是"没这个浏览器"
        raise CreatorError(
            "打不开浏览器（channel=%s）：%s\n"
            "装一个 Google Chrome，或用 DOUYIN_CREATOR_CHANNEL 换成其它 channel"
            "（msedge / chromium）。" % (channel or "chrome", str(exc)[:200])
        ) from None
    context = await browser.new_context(
        permissions=["geolocation"],
        **({"storage_state": storage_state} if storage_state and Path(storage_state).is_file() else {}),
    )
    return browser, context


async def is_logged_in(page) -> bool:
    """判"已经进了创作者中心"：URL 进了 creator-micro，且登录标志都不见了。

    ★ 不能只看 URL：登录页也会挂在 creator.douyin.com 下，扫不出来就会把
      "还在登录页"误判成"已登录"。
    """
    if "creator.douyin.com/creator-micro" not in page.url:
        return False
    for text in _LOGIN_MARKERS:
        try:
            marker = page.get_by_text(text, exact=True).first
            if await marker.count() and await marker.is_visible():
                return False
        except Exception:  # noqa: BLE001 —— 页面正在跳转时探测会抛，按"没看到"算
            continue
    return True


async def _save_qrcode(page, qr_path: Path) -> str:
    """把登录二维码落成 PNG，返回图片路径（拿不到就返回空串）。

    ★ 拿不到**不致命**：有头浏览器里二维码就在屏幕上，用户直接扫也行。
    """
    src = ""
    last_err = ""
    for selector in _QR_SELECTORS:
        try:
            image = page.locator(selector).first
            await image.wait_for(state="attached", timeout=8000)
            src = await image.get_attribute("src") or ""
            if src:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:120]
            continue
    if not src.startswith("data:image/"):
        if last_err:
            sys.stderr.write("[creator] 没定位到二维码元素：%s\n" % last_err)
        return ""
    try:
        _, encoded = src.split(",", 1)
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(base64.b64decode(encoded))
        return str(qr_path)
    except Exception as exc:  # noqa: BLE001 —— 二维码存不下来只是少个便利，不是失败
        sys.stderr.write("[creator] 二维码写盘失败：%s\n" % str(exc)[:120])
        return ""


async def _refresh_qrcode(page, qr_path: Path) -> str:
    """二维码过期时点一下刷新，并把新图落盘。"""
    try:
        expired = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired.count() and await expired.is_visible():
            await expired.click()
            await page.wait_for_timeout(1000)
            return await _save_qrcode(page, qr_path)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[creator] 刷新二维码失败：%s\n" % str(exc)[:120])
    return ""


async def _has_sms_prompt(page) -> bool:
    """页面上有没有短信/手机号验证输入框（有的话只能让用户在窗口里手动输）。"""
    locator = page.locator(
        'input[placeholder*="验证码"], input[type="tel"], '
        'input[placeholder*="短信"], input[placeholder*="手机号"]'
    ).first
    try:
        return bool(await locator.count()) and await locator.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def login(spec: Dict[str, Any]) -> Dict[str, Any]:
    """扫码登录：开有头浏览器 → 存二维码 → 等跳进 creator-micro → 落 storage_state。

    ★ 必须有头：抖音对无头浏览器有反自动化策略，而且扫码后可能要求短信二次验证 ——
      那只能在真窗口里手动输。窗口不会久留：成功/超时都会关掉。
    """
    from patchright.async_api import async_playwright  # 局部 import，见模块说明

    started = _now_ms()
    credential_path = Path(spec["credential_path"])
    qr_path = Path(spec.get("qr_path") or (credential_path.with_suffix(".qrcode.png")))
    account = str(spec.get("account") or "main")
    max_wait = float(spec.get("max_wait_sec") or 300)
    poll = float(spec.get("poll_sec") or 2)
    channel = str(spec.get("channel") or "chrome")

    async with async_playwright() as playwright:
        try:
            # ★ 默认有头：抖音会挑无头浏览器，而且可能要输短信验证码。
            #   headed=false 只在"有二维码图片能扫、但机器上没有桌面会话"时用。
            browser, context = await launch_context(
                playwright, channel, headless=not bool(spec.get("headed", True))
            )
        except CreatorError as exc:
            return _fail("no_browser", str(exc), "装个 Chrome 即可；无需登录也能用读取类工具。")
        try:
            page = await context.new_page()
            await page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=60000)
            qr_available = await _save_qrcode(page, qr_path)

            deadline = time.time() + max_wait
            sms_warned = False
            while time.time() < deadline:
                if await is_logged_in(page):
                    await page.wait_for_timeout(1500)   # 等 cookie 写完再抓，避免抓到半份
                    credential_path.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(credential_path))
                    ok, why = _credential_looks_valid(credential_path)
                    if not ok:
                        return _fail(
                            "cookie_invalid",
                            "扫码流程走完了，但抓到的登录态里没有 sessionid",
                            "再扫一次；或改用 DOUYIN_COOKIE_FILE 指定凭据文件。",
                            elapsedMs=_now_ms() - started,
                            url=page.url,
                        )
                    return {
                        "ok": True,
                        "action": "login",
                        "state": "success",
                        "logged_in": True,
                        "message": "抖音登录成功（账号「%s」）。" % account,
                        "url": page.url,
                        "qrPath": qr_available,
                        "credentialPath": str(credential_path),
                        "elapsedMs": _now_ms() - started,
                        "note": why,
                    }
                if await _has_sms_prompt(page) and not sms_warned:
                    sms_warned = True
                    sys.stderr.write("[creator] 检测到短信/安全验证：请在浏览器窗口里手动完成。\n")
                new_qr = await _refresh_qrcode(page, qr_path)
                if new_qr:
                    qr_available = new_qr
                await page.wait_for_timeout(int(poll * 1000))

            return _fail(
                "timeout",
                "等待扫码登录超时（%.0f 秒内没等到进入创作者中心）" % max_wait,
                "二维码可能已过期：重新调用本工具会开一个新的登录窗口。",
                elapsedMs=_now_ms() - started,
                url=page.url,
                qrPath=qr_available,
            )
        except CreatorError as exc:
            return _fail("failed", str(exc), "", elapsedMs=_now_ms() - started)
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成一行 JSON
            return _fail(
                "failed",
                "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                "有头窗口里能看到具体卡在哪一步；加 DOUYIN_DEBUG=1 会在失败时截图。",
                elapsedMs=_now_ms() - started,
            )
        finally:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass


def _credential_looks_valid(credential_path: Path) -> Tuple[bool, str]:
    """落盘的 storage_state 里有没有 sessionid —— 登录成功的最低门槛。"""
    try:
        data = json.loads(credential_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return False, "凭据文件读不了：%s" % str(exc)[:120]
    cookies = data.get("cookies") or []
    names = {c.get("name") for c in cookies if isinstance(c, dict)}
    if "sessionid" in names:
        return True, "已写入 %d 个 cookie（含 sessionid）" % len(cookies)
    return False, "cookie 里有 %s，但没有 sessionid" % sorted(n for n in names if n)


__all__ = [
    "CREATOR_HOME",
    "CREATOR_UPLOAD",
    "CreatorError",
    "is_logged_in",
    "launch_context",
    "login",
]
