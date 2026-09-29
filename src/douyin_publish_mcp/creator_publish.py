"""发布编排：视频 / 图文，一步不落地把发布页该做的事做完。

与 `creator_steps` 的分工：那边是"某个控件怎么点"，这边是"按什么顺序做、失败怎么办"。
两个入口都跑在 helper 进程里，输入是一份 spec（见 `publish_video` / `publish_note` 的 docstring），
输出是一行 JSON。

★ 发布是**不可逆**动作，所以这一层最看重的不是"跑完"，而是**如实回报**：
  - 每一步都记进 `steps`，失败时能看出卡在哪；
  - 点过"发布"但没等到成功页 → `state="uncertain"`，明确让人去作品管理里确认，
    **绝不自动重试**（重试就可能发出两条）。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .creator import CREATOR_UPLOAD, CreatorError, is_logged_in, launch_context
from .creator_steps import (
    apply_collection,
    apply_self_declaration,
    clear_blocking_overlays,
    enable_sync_switch,
    fill_title_and_description,
    handle_auto_video_cover,
    logs,
    read_verify_code,
    select_bgm,
    set_product_link,
    set_schedule_time,
    set_thumbnail,
    sms_input_locator,
    submit_verify_code,
)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".flv", ".wmv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MIN_SCHEDULE_LEAD = timedelta(hours=2)
NOTE_TITLE_MAX = 20
NOTE_MAX_IMAGES = 35
NOTE_TEXT_MAX = 1000
DESC_MAX = 1000

_PUBLISH_PAGE_V1 = "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page"
_PUBLISH_PAGE_V2 = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"


class PublishAbort(RuntimeError):
    """发布前的校验失败（还没动账号，可以放心重来）"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fail(kind: str, message: str, hint: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ok": False, "error": {"kind": kind, "message": message, "hint": hint}}
    # ★ state 默认就是失败的种类：状态页/工具靠它说"卡在哪一步"，失败时不该是空字符串
    payload.setdefault("state", kind)
    payload.update(extra)
    return payload


def require_file(raw: str, allowed: set, what: str) -> str:
    if not raw:
        raise PublishAbort("%s 不能为空。" % what)
    path = Path(raw).expanduser()
    if not path.is_file():
        raise PublishAbort("%s 不存在：%s" % (what, path))
    if path.suffix.lower() not in allowed:
        raise PublishAbort(
            "%s 格式不支持：%s（支持 %s）" % (what, path.suffix, ", ".join(sorted(allowed)))
        )
    return str(path)


def normalize_schedule(raw: str) -> str:
    """校验定时发布时间，返回 `YYYY-MM-DD HH:MM`；空串表示立即发布。"""
    text = (raw or "").strip()
    if not text:
        return ""
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise PublishAbort("定时发布时间看不懂：%r（要用 `YYYY-MM-DD HH:MM`）" % text)
    if parsed <= datetime.now() + MIN_SCHEDULE_LEAD:
        raise PublishAbort("定时发布时间必须晚于当前时间 2 小时（平台要求），现在是 %s"
                           % datetime.now().strftime("%Y-%m-%d %H:%M"))
    return parsed.strftime("%Y-%m-%d %H:%M")


def check_titles(title: str, description: str, tags: List[str]) -> None:
    if not title or not title.strip():
        raise PublishAbort("标题不能为空。")
    if len(title.strip()) > 30:
        raise PublishAbort("标题最多 30 个字（当前 %d 个）。" % len(title.strip()))
    if description and len(description) > DESC_MAX:
        raise PublishAbort("正文最多 %d 个字（当前 %d 个）。" % (DESC_MAX, len(description)))
    for tag in tags:
        if any(ch.isspace() for ch in str(tag)):
            raise PublishAbort("话题里不能有空格：%r（空格会被平台当成分隔符）" % tag)


class Deadline:
    """一次发布的总时限：每个等待都用 min(自己的上限, 剩余时间)，避免单点卡死拖满全场。"""

    def __init__(self, seconds: float):
        self.seconds = max(30.0, seconds)
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def left(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    def expired(self) -> bool:
        return self.left <= 0

    def slice(self, want: float) -> float:
        return max(1000.0, min(want, self.left * 1000))


async def _goto_upload(page, deadline: Deadline) -> None:
    await page.goto(CREATOR_UPLOAD, wait_until="domcontentloaded", timeout=deadline.slice(90000))
    await page.wait_for_url(CREATOR_UPLOAD, timeout=deadline.slice(90000))
    await page.wait_for_timeout(2000)
    if not await is_logged_in(page):
        raise CreatorError(
            "登录态已失效（页面停在登录页）"
        )


async def _wait_upload_input(page, deadline: Deadline):
    """找上传框：三种选择器兜底（抖音改版改过 class 前缀）。"""
    candidates = (
        "input.upload-btn-input, div[class^='container'] input[accept]",
        "div[class^='container'] input[type='file'], div[class^='container'] input.upload-input",
        "div[class^='container'] input",
    )
    for selector in candidates:
        node = page.locator(selector).first
        if await node.count():
            await node.wait_for(state="attached", timeout=deadline.slice(60000))
            return node
    raise CreatorError("上传页上找不到文件选择框（页面结构可能又改了）")


async def _wait_publish_form(page, deadline: Deadline, note: bool = False) -> str:
    """等进入**发布表单页**（不是上传页）：视频有两种版本，图文一种。"""
    while not deadline.expired():
        for url, name in (
            ("**/creator-micro/content/post/image?**" if note else _PUBLISH_PAGE_V1, "图文/v1"),
            (_PUBLISH_PAGE_V2, "v2"),
        ):
            try:
                await page.wait_for_url(url, timeout=3000)
                return name
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(500)
    raise CreatorError("等了 %d 秒还没进到发布表单页（上传可能卡住了）" % int(deadline.elapsed))


async def _wait_video_uploaded(page, deadline: Deadline, video_path: str, steps) -> None:
    """等视频传完：出现"重新上传"就是传完了；中途见到"上传失败"就重传一次。"""
    retried = False
    while not deadline.expired():
        try:
            if await page.locator('[class^="long-card"] div:has-text("重新上传")').count():
                logs(steps, "视频上传完成")
                return
            if await page.locator('div.progress-div > div:has-text("上传失败")').count() and not retried:
                retried = True
                logs(steps, "检测到上传失败，重新上传一次", ok=False)
                await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(video_path)
        except Exception:  # noqa: BLE001 —— 上传中页面会重渲染，探测失败是常态
            pass
        await page.wait_for_timeout(2000)
    raise CreatorError("视频上传超时（%d 秒还没传完，文件是不是太大了）" % int(deadline.elapsed))


async def _click_publish_until_done(page, deadline: Deadline, success_urls, steps, debug: bool) -> str:
    """点"发布"并等成功页。

    返回 "published" 或 "uncertain"：
    ★ 点过按钮就可能是发出去了 —— 这种情况**绝不能**自动重试，只能如实回报让人去确认。
    """
    clicked = 0
    sms_prompted = False
    code_file = ""
    while not deadline.expired():
        try:
            await page.evaluate(
                "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container,"
                "[class*=\"mention-wrapper\"]').forEach(e => e.remove())"
            )
            if code_file:
                sms = await sms_input_locator(page)
                if await sms.count() and await sms.is_visible():
                    code = read_verify_code(code_file)
                    if code:
                        logs(steps, "填入短信验证码")
                        await submit_verify_code(page, sms, code, code_file)
                    elif not sms_prompted:
                        sms_prompted = True
                        logs(steps, "平台要求短信验证：请把验证码写进 %s（本工具会自己读到）" % code_file, ok=False)

            button = page.get_by_role("button", name="发布", exact=True)
            if await button.count():
                await button.click(force=True)
                clicked += 1
                logs(steps, "点击发布（第 %d 次）" % clicked)
            for url in success_urls:
                try:
                    await page.wait_for_url(url, timeout=3000)
                    return "published"
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        # 被"请设置封面后再发布"拦住时，拿推荐封面兜底
        if await handle_auto_video_cover(page):
            logs(steps, "用了平台推荐封面兜底")
        if debug:
            try:
                await page.screenshot(path=str(Path(Path.home()) / "douyin-publish-debug.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass
        await page.wait_for_timeout(500)
    return "uncertain" if clicked else "failed"


async def publish_video(spec: Dict[str, Any]) -> Dict[str, Any]:
    """发布视频。spec：

        {"credential_path", "channel", "headless", "timeout_sec", "verify_code_file", "debug",
         "video": {"path", "title", "description", "tags"[], "schedule",
                   "thumbnail_portrait", "thumbnail_landscape",
                   "product_link", "product_title", "declaration", "collection"}}
    """
    return await _publish(spec, note=False)


async def publish_note(spec: Dict[str, Any]) -> Dict[str, Any]:
    """发布图文。spec 同上，key 为 `note`：{"images"[], "title", "note", "tags"[],
    "schedule", "bgm"}。"""
    return await _publish(spec, note=True)


async def _publish(spec: Dict[str, Any], note: bool) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    warnings: List[str] = []
    started = _now_ms()
    deadline = Deadline(float(spec.get("timeout_sec") or 900))
    action = "publish_note" if note else "publish_video"
    credential_path = Path(spec["credential_path"])
    if not credential_path.is_file():
        return _fail("not_logged_in", "没有登录态文件：%s" % credential_path,
                     "先调用 douyin_account_login 扫码登录。", action=action)

    # ── 先校验，一条都不许在上传之后才报错（上传很贵）──
    try:
        if note:
            data = dict(spec.get("note") or {})
            images = [
                require_file(str(p), IMAGE_EXTENSIONS, "第 %d 张图片" % (i + 1))
                for i, p in enumerate(data.get("images") or [])
            ]
            if not images:
                raise PublishAbort("图文至少要 1 张图片。")
            if len(images) > NOTE_MAX_IMAGES:
                raise PublishAbort("图文最多 %d 张图片（当前 %d 张）。" % (NOTE_MAX_IMAGES, len(images)))
            title = str(data.get("title") or "").strip() or (str(data.get("note") or "")[:NOTE_TITLE_MAX])
            if len(title) > NOTE_TITLE_MAX:
                raise PublishAbort("图文标题最多 %d 个字（当前 %d 个）。" % (NOTE_TITLE_MAX, len(title)))
            body = str(data.get("note") or "")
            if len(body) > NOTE_TEXT_MAX:
                raise PublishAbort("图文正文最多 %d 个字（当前 %d 个）。" % (NOTE_TEXT_MAX, len(body)))
            check_titles(title, body, list(data.get("tags") or []))
            schedule = normalize_schedule(str(data.get("schedule") or ""))
        else:
            data = dict(spec.get("video") or {})
            video = require_file(str(data.get("path") or ""), VIDEO_EXTENSIONS, "视频")
            portrait = str(data.get("thumbnail_portrait") or "")
            landscape = str(data.get("thumbnail_landscape") or "")
            if portrait:
                portrait = require_file(portrait, IMAGE_EXTENSIONS, "竖版封面")
            if landscape:
                landscape = require_file(landscape, IMAGE_EXTENSIONS, "横版封面")
            link = str(data.get("product_link") or "")
            product_title = str(data.get("product_title") or "")
            if bool(link) != bool(product_title):
                raise PublishAbort("带货商品要链接和短标题**一起**给（只给一个平台不认）。")
            check_titles(str(data.get("title") or ""), str(data.get("description") or ""),
                          list(data.get("tags") or []))
            schedule = normalize_schedule(str(data.get("schedule") or ""))
    except PublishAbort as exc:
        return _fail("invalid_args", str(exc), "改完参数再调一次；这一步还没碰账号。",
                     action=action, steps=steps, elapsedMs=_now_ms() - started)

    # ── 开浏览器，按顺序做完 ──
    # ★ 驱动在这里才 import：参数/凭据的问题（最常见）根本用不到浏览器，
    #   不该让"没装驱动"盖住"标题太长"这种更该先说的错。
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        return _fail(
            "no_driver",
            "浏览器驱动没就位：%s" % exc,
            "驱动会按需下载（DOUYIN_BROWSER_SITE_PACKAGES 可指到本机已有的那份）；"
            "或先只做读取——读取不需要浏览器。",
            action=action, steps=steps, elapsedMs=_now_ms() - started,
        )

    async with async_playwright() as playwright:
        try:
            browser, context = await launch_context(
                playwright,
                str(spec.get("channel") or "chrome"),
                headless=bool(spec.get("headless", True)),
                storage_state=str(credential_path),
            )
        except CreatorError as exc:
            return _fail("no_browser", str(exc), "装个 Google Chrome 即可。",
                         action=action, steps=steps, elapsedMs=_now_ms() - started)
        published = "failed"
        try:
            page = await context.new_page()
            await _goto_upload(page, deadline)
            logs(steps, "进入上传页")

            if note:
                await page.get_by_text("发布图文", exact=True).click()
                await page.wait_for_timeout(1000)
                upload = page.locator("div[class^='container'] input[accept*='image']").first
                if not await upload.count():
                    raise CreatorError("找不到图片上传框（页面结构可能又改了）")
                await upload.set_input_files(images)
                logs(steps, "已选择 %d 张图片" % len(images))
            else:
                upload = await _wait_upload_input(page, deadline)
                await upload.set_input_files(video)
                logs(steps, "已选择视频：%s" % Path(video).name)

            version = await _wait_publish_form(page, deadline, note=note)
            logs(steps, "进入发布表单页（%s）" % version)
            await page.wait_for_timeout(1000)

            await fill_title_and_description(page, title, body if note else str(data.get("description") or ""),
                                             list(data.get("tags") or []))
            logs(steps, "已填标题、正文与话题")

            if not note:
                await _wait_video_uploaded(page, deadline, video, steps)
                link = str(data.get("product_link") or "")
                if link:
                    ok = await set_product_link(page, link, str(data.get("product_title") or ""))
                    logs(steps, "设置带货商品", ok=ok)
                    if not ok:
                        warnings.append("带货商品没设上（链接无效或页面结构变了），已按无商品继续发布。")

            declaration = str(data.get("declaration") or "").strip()
            if declaration:
                ok = await apply_self_declaration(page, declaration)
                logs(steps, "自主声明「%s」" % declaration, ok=ok)
                if not ok:
                    warnings.append("自主声明「%s」没设上，已跳过。" % declaration)

            if not note:
                collection = str(data.get("collection") or "")
                if collection:
                    ok = await apply_collection(page, collection)
                    logs(steps, "归集合集「%s」" % collection, ok=ok)
                    if not ok:
                        warnings.append("没找到合集「%s」，保持未选状态继续发布。" % collection)

            if note:
                bgm = str(data.get("bgm") or "")
                if bgm:
                    ok = await select_bgm(page, bgm)
                    logs(steps, "BGM「%s」" % bgm, ok=ok)
                    if not ok:
                        warnings.append("没搜到 BGM「%s」，已跳过。" % bgm)
            else:
                portrait = str(data.get("thumbnail_portrait") or "")
                landscape = str(data.get("thumbnail_landscape") or "")
                if portrait or landscape:
                    ok = await set_thumbnail(page, portrait, landscape)
                    logs(steps, "自定义封面", ok=ok)
                    if not ok:
                        warnings.append("自定义封面没设上（会由推荐封面兜底）。")

            sync = await enable_sync_switch(page)
            if sync is not None:
                logs(steps, "同步开关", ok=True, detail="本次点开了" if sync else "本来就是开的")

            if schedule:
                await set_schedule_time(page, schedule)
                logs(steps, "定时发布：%s" % schedule)

            success_urls = (
                ["**/creator-micro/content/manage?enter_from=publish**"]
                if note else
                ["https://creator.douyin.com/creator-micro/content/manage**"]
            )
            published = await _click_publish_until_done(page, deadline, success_urls, steps, bool(spec.get("debug")))

            if published == "published":
                # 发布成功的顺手把 cookie 存回去：平台会在这时轮换会话 cookie，
                # 不更新下次很可能就"登录态失效"了。
                await context.storage_state(path=str(credential_path))
                logs(steps, "已更新登录态文件")
                return {
                    "ok": True,
                    "action": action,
                    "state": "published",
                    "message": ("图文" if note else "视频") + "已发布：%s" % title,
                    "url": page.url,
                    "steps": steps,
                    "warnings": warnings,
                    "elapsedMs": _now_ms() - started,
                }

            if published == "uncertain":
                return _fail(
                    "uncertain",
                    "点过发布按钮，但没等到成功页面跳转 —— **这条可能已经发出去了**。",
                    "先到创作者中心「作品管理」确认有没有这条；有就别再发，没有再加长超时重试。"
                    "本工具不会自动重试，避免发出两条。",
                    action=action, steps=steps, warnings=warnings,
                    url=page.url, elapsedMs=_now_ms() - started,
                )
            return _fail(
                "publish_failed", "一直没点动发布按钮（按钮被浮层挡住或页面还在处理）",
                "到「作品管理」确认没有这次发布，然后再试一次；DOUYIN_DEBUG=1 会在失败时截图。",
                action=action, steps=steps, warnings=warnings,
                url=page.url, elapsedMs=_now_ms() - started,
            )
        except CreatorError as exc:
            kind = "not_logged_in" if "登录态已失效" in str(exc) else "failed"
            hint = ("重新调用 douyin_account_login 扫码。" if kind == "not_logged_in"
                    else "多为上传/页面结构问题；DOUYIN_DEBUG=1 会在失败时截图。")
            return _fail(kind, str(exc), hint, action=action, steps=steps,
                         warnings=warnings, elapsedMs=_now_ms() - started)
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成一行 JSON
            return _fail(
                "failed",
                "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                "加 DOUYIN_DEBUG=1 会截图，便于看卡在哪一步。",
                action=action, steps=steps, warnings=warnings, elapsedMs=_now_ms() - started,
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


__all__ = ["publish_note", "publish_video"]
