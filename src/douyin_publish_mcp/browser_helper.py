"""浏览器通道的**执行端**：在"有浏览器的那一端"跑，导航到目标页并抓抖音自己发出的响应。

## 为什么不是"在页面里发 fetch"

★ 2026-09-29 实测（这是本模块存在的全部理由）：

- 页面里裸 `fetch('/aweme/v1/web/aweme/detail/?aweme_id=…')` → `403 Uifid Not Found`
- 手工把抓到的真 uifid 补进 URL 再打 → `403 Signature Not Found`

`uifid`（60 字符设备指纹，站内 82 个请求都带）和 `a_bogus` 是抖音自己的请求封装注入的，
**原生 fetch 绕不过去**；而它自己发的请求（打开 `/video/<id>`、`/user/<sec_uid>` 时触发的
XHR）全是 200。所以这条通道的正确形态是「导航 + 抓它自己的响应」。

## 只在抓到响应的那一刻读 body

★ 踩过的坑：等页面跑完再 `await resp.json()` 会 `Protocol error (Network.getResponseBody)` ——
body 已经释放。必须在 `response` 回调里**立刻**读。

## 约定

- stdin/stdout 都不是协议通道：规格由命令行参数给（一个 JSON 文件），结果**一行 JSON** 打 stdout，
  日志一律走 stderr（调用方按行解析 stdout，多一个字都会解析失败）。
- 本文件被两种宿主执行：`douyin-publish-mcp.exe --browser-helper <spec>`（exe 自带，推荐）
  或 `<解释器> browser_helper.py <spec>`（开发期/显式指定解释器时）。
- **不引入任何非标准库依赖**：patchright/playwright 由宿主环境提供。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# ── 规格：kind → 打开哪个页面、抓哪个接口 ─────────────────────

VIDEO_PAGE = "https://www.douyin.com/video/%s"
USER_PAGE = "https://www.douyin.com/user/%s"
SELF_PAGE = "https://www.douyin.com/user/self"

KINDS: Dict[str, Dict[str, str]] = {
    # kind          参考页                                   要抓的接口路径
    "video_detail": {"api": "/aweme/v1/web/aweme/detail/", "page": "video"},
    "comments": {"api": "/aweme/v1/web/comment/list/", "page": "video"},
    "user_profile": {"api": "/aweme/v1/web/user/profile/other/", "page": "user"},
    "user_post": {"api": "/aweme/v1/web/aweme/post/", "page": "user"},
    "my_profile": {"api": "/aweme/v1/web/user/profile/self/", "page": "self"},
}


def _page_url(want: Dict[str, Any], aweme_id: str) -> str:
    """这一条 want 该打开哪个页面（同一个页面的多条 want 会合并成一次导航）。"""
    page = KINDS[want["kind"]]["page"]
    if page == "video":
        return VIDEO_PAGE % aweme_id
    if page == "self":
        return SELF_PAGE
    sec_user_id = str(want.get("sec_user_id") or want.get("sec_uid") or "")
    if sec_user_id:
        return USER_PAGE % sec_user_id
    return SELF_PAGE  # 自己的作品：/user/self 页会自己打 post 接口


def _matches(want: Dict[str, Any], query: Dict[str, List[str]]) -> bool:
    """响应是否就是这条 want 要的（避免把别人的 /aweme/post/ 当成目标）。"""
    kind = want["kind"]
    if kind in ("video_detail", "comments"):
        want_id = str(want.get("aweme_id") or "")
        got = (query.get("aweme_id") or [""])[0]
        return not want_id or got == want_id
    if kind == "user_post":
        want_uid = str(want.get("sec_user_id") or want.get("sec_uid") or "")
        got = (query.get("sec_user_id") or [""])[0]
        if not want_uid:
            return True   # 自己的作品：页面打哪个算哪个
        return got == want_uid
    return True


def build_plan(wants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 wants 按"打开哪个页面"归组：一次导航能服务多条 want（起一次浏览器不容易）。"""
    plan: List[Dict[str, Any]] = []
    for want in wants:
        url = _page_url(want, str(want.get("aweme_id") or ""))
        slot = next((p for p in plan if p["url"] == url), None)
        if slot is None:
            slot = {"url": url, "wants": []}
            plan.append(slot)
        slot["wants"].append(want)
    return plan


# ── 执行 ─────────────────────────────────────────────────────


class BrowserMissing(RuntimeError):
    """一个浏览器都起不来（系统 Chrome 没有、自带那份也没下载）。

    ★ 单独一个类型是为了让客户端能**分辨**"缺浏览器"与"别的问题"：
      前者可以走"按需下载 chromium"兜底，后者不该反复重试。
    """


async def _launch(playwright, prefer: str, headless: bool):
    """起浏览器：**先用系统 Chrome**（实测能过抖音那堵墙，省掉 ~170MB 的下载），
    没有再退回自带 chromium。

    ★ 这里与小红书 MCP 的取舍正好相反：它必须用"自己那份改造 Chromium"
      （fingerprint-* 自定义参数只有它认，换系统 Chrome 等于指纹静默降级）；
      我们不需要指纹伪装，所以系统 Chrome 就够 —— 这是我们能"exe 小 + 不下载浏览器"的原因。
    """
    order = {"chrome": ["chrome"], "chromium": [None], "auto": ["chrome", None]}.get(prefer, ["chrome", None])
    errors: List[str] = []
    for channel in order:
        kwargs: Dict[str, Any] = {"headless": headless}
        if channel:
            kwargs["channel"] = channel
        try:
            browser = await playwright.chromium.launch(**kwargs)
            return browser, (channel or "bundled-chromium")
        except Exception as exc:  # noqa: BLE001 —— 缺哪个浏览器是可预期的分支，不是 bug
            errors.append("%s: %s" % (channel or "bundled-chromium", str(exc).splitlines()[0][:120]))
    raise BrowserMissing(
        "起不了浏览器（按顺序都失败）：" + "；".join(errors)
        + "\n提示：装一个 Chrome 即可（本通道优先用系统 Chrome，不需要下载 chromium）；"
          "或用 DOUYIN_BROWSER_CHANNEL=chromium 指定用它自带的那份。"
    )


def _extend_sys_path() -> List[str]:
    """把驱动目录挂进 sys.path。

    ★ 为什么不能只靠 PYTHONPATH：冻结（PyInstaller）之后，本进程的 `sys.path`
      由引导器自己定，**环境变量 PYTHONPATH 不再生效** —— 实测
      `douyin-publish-mcp.exe --browser-helper` 会报 `No module named 'patchright'`。
      所以由客户端用 `DOUYIN_BROWSER_DRIVER_PATH` 把目录传进来，这里显式插入。
      （纯 Python 包走普通 PathFinder 就能导入；PyInstaller 自己的导入器只管它打包的那些。）
    """
    added: List[str] = []
    raw = os.pathsep.join(
        p for p in (
            os.environ.get("DOUYIN_BROWSER_DRIVER_PATH", ""),
            os.environ.get("DOUYIN_BROWSER_SITE_PACKAGES", ""),
        ) if p
    )
    for chunk in raw.split(os.pathsep):
        path = chunk.strip()
        if not path or not os.path.isdir(path) or path in sys.path:
            continue
        sys.path.insert(0, path)
        added.append(path)
    return added


async def _run(spec: Dict[str, Any]) -> Dict[str, Any]:
    added = _extend_sys_path()
    if added:
        sys.stderr.write("driver path: %s\n" % " | ".join(added))

    from patchright.async_api import async_playwright  # 延迟导入：宿主没装时给出清楚的报错

    wants = [w for w in (spec.get("wants") or []) if w.get("kind") in KINDS]
    if not wants:
        return {"ok": False, "error": {"kind": "spec", "message": "wants 里没有认识的 kind"}}

    plan = build_plan(wants)
    timeout_ms = int(spec.get("timeout_ms") or 45000)
    nav_timeout_ms = int(spec.get("navigate_timeout_ms") or 60000)
    headless = bool(spec.get("headless", True))
    prefer = str(spec.get("channel") or "auto").lower()

    results: Dict[str, Any] = {}
    misses: Dict[str, str] = {}
    started = time.time()
    browser_note = ""

    async with async_playwright() as playwright:
        browser, browser_note = await _launch(playwright, prefer, headless)
        try:
            context_kwargs: Dict[str, Any] = {"locale": "zh-CN", "viewport": {"width": 1440, "height": 900}}
            if spec.get("storage_state"):
                context_kwargs["storage_state"] = spec["storage_state"]
            elif spec.get("cookies"):
                context_kwargs["storage_state"] = {"cookies": spec["cookies"], "origins": []}
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            for slot in plan:
                events = {id(w): asyncio.Event() for w in slot["wants"]}

                def make_handler(nav_wants, nav_events):
                    async def handler(response):
                        path = urlparse(response.url).path
                        for want in nav_wants:
                            key = _key(want)
                            if results.get(key) or KINDS[want["kind"]]["api"] != path:
                                continue
                            try:
                                body = await response.json()   # ★ 必须此刻读：晚一步 body 就没了
                            except Exception:  # noqa: BLE001 —— 读不出来就继续等下一次响应
                                continue
                            if not _matches(want, parse_qs(urlparse(response.url).query)):
                                continue
                            results[key] = {
                                "status": response.status,
                                "url_tail": urlparse(response.url).path,
                                "body": body,
                                "read_at_ms": int((time.time() - started) * 1000),
                            }
                            nav_events[id(want)].set()

                    return handler

                handler = make_handler(slot["wants"], events)
                page.on("response", handler)
                try:
                    await page.goto(slot["url"], wait_until="domcontentloaded", timeout=nav_timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    for want in slot["wants"]:
                        misses.setdefault(_key(want), "导航失败：%s" % str(exc).splitlines()[0][:120])
                for want in slot["wants"]:
                    key = _key(want)
                    if results.get(key):
                        continue
                    try:
                        await asyncio.wait_for(events[id(want)].wait(), timeout=timeout_ms / 1000.0)
                    except asyncio.TimeoutError:
                        misses.setdefault(key, "等 %s 无响应（%d 秒）" % (KINDS[want["kind"]]["api"], timeout_ms // 1000))
                page.remove_listener("response", handler)

            try:
                title = await page.title()
            except Exception:  # noqa: BLE001
                title = ""
            await context.close()
        finally:
            await browser.close()

    payload: Dict[str, Any] = {
        "ok": bool(results),
        "browser": browser_note,
        "results": results,
        "misses": misses,
        "elapsed_ms": int((time.time() - started) * 1000),
        "page_title": title if results else title,
    }
    if not results:
        payload["error"] = {
            "kind": "no_response",
            "message": "浏览器里也没等到目标接口的响应（页面标题：%s）" % (title or "(空)"),
            "hint": (
                "多半是登录态失效或撞上风控页：① 打开浏览器确认抖音还是登录状态（必要时重扫）"
                "② 把 headless 关掉（DOUYIN_BROWSER_HEADFUL=1）看页面到底显示了什么"
                "③ 换成自带 chromium（DOUYIN_BROWSER_CHANNEL=chromium）。"
            ),
        }
    return payload


def _key(want: Dict[str, Any]) -> str:
    """结果里的键：同一 kind 可能有多目标，用参数区分（detail:id / post:uid）。"""
    kind = want["kind"]
    if kind in ("video_detail", "comments"):
        return "%s:%s" % (kind, want.get("aweme_id") or "")
    if kind == "user_post":
        return "%s:%s" % (kind, want.get("sec_user_id") or want.get("sec_uid") or "self")
    return kind


async def _amain(spec_path: str) -> int:
    try:
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {"kind": "spec", "message": str(exc)}}, ensure_ascii=False))
        return 2
    try:
        payload = await _run(spec)
    except BrowserMissing as exc:
        payload = {
            "ok": False,
            "error": {
                "kind": "no_browser",
                "message": str(exc)[:600],
                "hint": "装个 Chrome，或设 DOUYIN_BROWSER_DOWNLOAD=1 让它自己下载一份 chromium（约 170MB）。",
            },
        }
    except ImportError as exc:
        payload = {
            "ok": False,
            "error": {
                "kind": "no_driver",
                "message": "这个解释器里没有 patchright/playwright：%s" % exc,
                "hint": "把浏览器驱动放到运行时目录（DOUYIN_BROWSER_SITE_PACKAGES 可显式指定），或让 exe 自己当宿主。",
            },
        }
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都要变成一行 JSON，别让调用方解析空气
        payload = {"ok": False, "error": {"kind": "crash", "message": "%s: %s" % (type(exc).__name__, str(exc)[:400])}}
    # ★ ensure_ascii=True：stdout 只用 ASCII，跨代码页/编码都不会变成乱码
    #   （Windows 上曾被 GBK 管道把错误信息撕成 `ï¿½`，排查成本远高于几个转义符）
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("用法：browser_helper.py <spec.json>\n")
        return 2
    return asyncio.run(_amain(argv[0]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
