"""发布页上的那些"点不动/挡住了/要等"的步骤 —— 移植自参考实现的真机经验。

这些函数看着啰嗦，但每一行都对应一个真实踩过的坑（模块内就地注明）。
它们只依赖 patchright 的 Page，不依赖本服务的其它模块，便于单测里用假 page 顶替。

参考来源：social-auto-upload（MIT）的 `uploader/douyin_uploader/main.py`，
见 NOTICE 的署名。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: 抖音自定义组件对普通 click 常常"既不抛异常也不生效"，必须派发完整原生事件序列
_NATIVE_CLICK_JS = """({x, y}) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return;
    const opts = {bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,view:window,
                  pointerId:1,pointerType:'mouse',isPrimary:true,button:0,buttons:1};
    for (const t of ['pointerover','pointerenter','pointerdown','mousedown','pointerup','mouseup','click']) {
        const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
        try { el.dispatchEvent(new C(t, opts)); }
        catch (e) { try { el.dispatchEvent(new MouseEvent(t, opts)); } catch (_) {} }
    }
}"""

#: 会拦住后续点击的浮层：新手引导(shepherd)、话题/@提及下拉。清掉它们不误伤模态框。
_CLEAR_OVERLAYS_JS = """() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e => e.remove());
    document.querySelectorAll('[class*="mention-wrapper"]').forEach(e => {
        const p = e.closest('.semi-portal'); (p || e).remove();
    });
    document.querySelectorAll('.semi-portal').forEach(e => {
        if (!e.querySelector('.semi-modal, .semi-modal-content')) e.remove();
    });
}"""


def logs(steps: List[Dict[str, Any]], step: str, ok: bool = True, detail: str = "") -> None:
    """把每一步记下来：发布是不可逆动作，出问题时"卡在哪一步"比"失败了"有用得多。"""
    steps.append({"step": step, "ok": bool(ok), "detail": detail[:400]})
    sys.stderr.write("[publish] %s%s%s\n" % (step, "： " if detail else "", detail[:300]))


async def native_click(page, locator) -> bool:
    """"真人级"点击：真实鼠标 + 完整事件序列。

    抖音的身份验证组件与不少自定义控件只认这整套事件，单纯 `click()` 会静默失效。
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    try:
        box = await locator.bounding_box()
    except Exception:  # noqa: BLE001
        box = None
    if not box:
        try:
            await locator.click(timeout=8000)
            return True
        except Exception:  # noqa: BLE001
            return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await page.wait_for_timeout(150)
        await page.mouse.click(x, y)
        await page.wait_for_timeout(200)
        await page.evaluate(_NATIVE_CLICK_JS, {"x": x, "y": y})
        return True
    except Exception:  # noqa: BLE001
        return False


async def clear_blocking_overlays(page) -> None:
    """清掉会拦点击的浮层（填完话题后尤其明显），并让输入框失焦。"""
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.evaluate(_CLEAR_OVERLAYS_JS)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(400)


async def fill_title_and_description(page, title: str, description: str, tags: Sequence[str]) -> None:
    """标题 + 正文 + 话题。

    ★ 标题输入框要等到**视频传完**才渲染（实测约 40 秒，大文件更久），所以等待给到 120 秒。
    ★ 正文是 contenteditable 的富文本容器，不是 textarea：先清空再逐字打，
      话题按 " #tag + 空格" 触发下拉，最后按 Escape 收起下拉 —— 否则浮层会挡住后面的点击。
    """
    title_input = page.locator('input[placeholder*="填写作品标题"]').first
    await title_input.wait_for(state="visible", timeout=120000)
    await title_input.fill(str(title)[:30])

    editor = page.locator('div.zone-container[contenteditable="true"]').first
    await editor.wait_for(state="visible", timeout=120000)
    await editor.click()
    await page.keyboard.press("Control+KeyA")
    await page.keyboard.press("Delete")
    if description and description.strip():
        await page.keyboard.type(description.strip())
    for tag in tags or []:
        await page.keyboard.type(" #" + str(tag))
        await page.keyboard.press("Space")
    await page.keyboard.press("Escape")


async def set_schedule_time(page, when: str) -> None:
    """定时发布：选"定时发布"单选 → 填 `YYYY-MM-DD HH:MM`。"""
    radio = page.locator("[class^='radio']:has-text('定时发布')")
    await native_click(page, radio)
    await page.wait_for_timeout(1000)
    field = page.locator('.semi-input[placeholder="日期和时间"]')
    await native_click(page, field)
    await page.keyboard.press("Control+KeyA")
    await page.keyboard.type(when)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1000)


async def apply_self_declaration(page, declaration: str) -> bool:
    """自主声明：点开弹窗 → 单选声明类型 → 确定。

    ★ 入口常被话题下拉残留浮层盖住，先清浮层再点。
    ★ 返回 False 表示"没设上"（调用方决定是跳过还是终止）。
    """
    wanted = (declaration or "").strip()
    if not wanted:
        return True
    try:
        await clear_blocking_overlays(page)
        entry = None
        for text in ("请选择自主声明", "请选择声明类型", "添加自主声明", "自主声明", "作品声明"):
            candidate = page.get_by_text(text).first
            if await candidate.count():
                entry = candidate
                break
        if entry is None:
            return False
        try:
            await entry.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        if not await native_click(page, entry):
            return False
        await page.wait_for_timeout(1200)

        dialog = page.locator(".semi-modal-content").filter(has_text="请选择声明类型").first
        if await dialog.count() == 0:
            dialog = page.locator(".semi-modal-body").filter(has_text="请选择声明类型").first
        if await dialog.count() == 0:
            return False
        await dialog.first.wait_for(state="visible", timeout=6000)

        option = dialog.locator("label.semi-radio").filter(
            has=page.locator('.semi-radio-addon:text-is("%s")' % wanted)
        ).first
        if await option.count() == 0:
            option = dialog.locator("label.semi-radio").filter(has_text=wanted).first
        if await option.count():
            if not await native_click(page, option):
                return False
        else:
            try:
                await dialog.get_by_text(wanted, exact=True).first.click(timeout=6000, force=True)
            except Exception:  # noqa: BLE001
                return False
        await page.wait_for_timeout(400)

        confirm = dialog.locator("button.semi-button-primary").filter(has_text="确定").first
        if await confirm.count() == 0:
            confirm = dialog.get_by_role("button", name="确定").first
        if await confirm.count() == 0:
            confirm = page.get_by_role("button", name="确定").first
        if await confirm.count():
            if not await native_click(page, confirm):
                return False
        try:
            await dialog.first.wait_for(state="hidden", timeout=6000)
        except Exception:  # noqa: BLE001 —— 没关掉也算设完了，别为这个中断发布
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[publish] 自主声明设置失败：%s\n" % str(exc)[:160])
        return False


async def apply_collection(page, name: str) -> bool:
    """归集到某个合集（字节 Semi 组件）。

    ★ 合集名没有 label，只能按文本精确匹配；触发器用专属 class `select-collection-*`。
    ★ 找不到同名合集时**不阻断发布**：界面允许留空，宁可少归集也不能发不出去。
    """
    wanted = (name or "").strip()
    if not wanted:
        return True
    try:
        await clear_blocking_overlays(page)
        trigger = page.locator('[class*="select-collection-"]').first
        if await trigger.count() == 0:
            return False
        if not await native_click(page, trigger.locator(".semi-select-selection")):
            return False
        await page.wait_for_timeout(800)
        option = page.locator(".semi-select-option.collection-option").filter(
            has=page.locator('[class*="option-title-"]:text-is("%s")' % wanted)
        )
        if await option.count() == 0:
            await page.keyboard.press("Escape")
            return False
        clicked = await native_click(page, option.first)
        await page.wait_for_timeout(500)
        return clicked
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[publish] 归集合集失败（跳过）：%s\n" % str(exc)[:160])
        return False


async def set_thumbnail(page, portrait: str, landscape: str) -> bool:
    """自定义封面（竖版/横版各一张）。

    ★ 三个坑（都是参考实现用 F12 挖出来的）：
      1. 打开封面弹窗要 hover 出"编辑封面/选择封面"入口，普通 click 常静默失效 → 重试 + native_click；
      2. 弹窗里有**两个** `input.semi-upload-hidden-input`：第一个是"AI 封面参考图"，
         第二个才是"上传封面"。用 `.first` 会把封面塞进参考图槽 → 检测一直转、"完成"关不掉；
         这里按拖拽区文案 `semi-upload-drag-area-main-text` 精确定位，取不到才 `.last` 兜底；
      3. 图片没处理完时"完成"是 disabled，点了无效 → 先等它解禁，点完还要**验证弹窗真的消失**，
         没消失就升级 native_click / 处理二次确认 / Esc 兜底。
    """
    if not portrait and not landscape:
        return True
    cover_modal = page.locator("div.dy-creator-content-modal").first
    try:
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container')"
            ".forEach(e => e.remove())"
        )
        cover_area = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await cover_area.count():
            cover_area = page.locator('[class*="cover"]').first
        try:
            await cover_area.wait_for(state="visible", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1500)

        opened = False
        for _ in range(5):
            trigger = None
            for _ in range(3):
                try:
                    await cover_area.hover(force=True)
                    await page.wait_for_timeout(600)
                except Exception:  # noqa: BLE001
                    pass
                for text in ("编辑封面", "选择封面", "设置封面"):
                    candidate = page.get_by_text(text, exact=True).first
                    if await candidate.count() and await candidate.is_visible():
                        trigger = candidate
                        break
                if trigger is not None:
                    break
            await native_click(page, trigger if trigger is not None else cover_area)
            try:
                await page.wait_for_selector("div.dy-creator-content-modal", timeout=5000)
                opened = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not opened:
            return False

        await page.wait_for_timeout(1500)
        upload = cover_modal.locator(
            '.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input'
        ).first
        if await upload.count() == 0:
            upload = cover_modal.locator("input.semi-upload-hidden-input").last

        if portrait:
            try:
                await cover_modal.get_by_text("设置竖封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001 —— 默认就在竖封面页，点不到不算错
                pass
            await upload.set_input_files(portrait)
        elif landscape:
            try:
                await cover_modal.get_by_text("设置横封面", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
            await upload.set_input_files(landscape)
        await page.wait_for_timeout(3000)

        async def finish_button():
            button = cover_modal.get_by_role("button", name="完成", exact=True).first
            if await button.count():
                return button
            return cover_modal.locator("button.semi-button").filter(has_text="完成").first

        for _ in range(30):  # 等图片处理完、按钮解禁（最多 ~15s）
            try:
                button = await finish_button()
                if await button.count():
                    klass = await button.get_attribute("class") or ""
                    if "semi-button-disabled" not in klass:
                        break
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(500)

        for _ in range(4):
            button = await finish_button()
            if await button.count() and await button.is_visible():
                try:
                    await button.click(timeout=4000)
                except Exception:  # noqa: BLE001
                    pass
                await page.wait_for_timeout(1500)
                if await cover_modal.count() == 0:
                    return True
                await native_click(page, button)
                await page.wait_for_timeout(1500)
                if await cover_modal.count() == 0:
                    return True
            for name in ("确定", "确认", "仍然完成", "仍要完成", "继续"):
                confirm = page.locator(".semi-modal-content").get_by_role("button", name=name, exact=True).first
                if await confirm.count() and await confirm.is_visible():
                    await native_click(page, confirm)
                    await page.wait_for_timeout(1500)
                    break
            if await cover_modal.count() == 0:
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
            if await cover_modal.count() == 0:
                return True
        return False
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[publish] 设置封面失败（交给推荐封面兜底）：%s\n" % str(exc)[:160])
        return False


async def handle_auto_video_cover(page) -> bool:
    """发布被"请设置封面后再发布"拦住时，选第一个推荐封面兜底。"""
    try:
        if not await page.get_by_text("请设置封面后再发布").first.is_visible():
            return False
        recommended = page.locator('[class^="recommendCover-"]').first
        if await recommended.count() == 0:
            return False
        await recommended.click()
        await page.wait_for_timeout(1000)
        if await page.get_by_text("是否确认应用此封面？").first.is_visible():
            await page.get_by_role("button", name="确定").click()
            await page.wait_for_timeout(1000)
        return True
    except Exception:  # noqa: BLE001
        return False


async def set_product_link(page, link: str, title: str) -> bool:
    """带货商品：标签下拉选"购物车" → 粘链接 → 添加链接 → 填商品短标题 → 完成编辑。"""
    try:
        await page.wait_for_timeout(2000)
        await page.wait_for_selector("text=添加标签", timeout=10000)
        dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
        if await dropdown.count() == 0:
            return False
        await dropdown.click()
        await page.wait_for_selector('[role="listbox"]', timeout=5000)
        await page.locator('[role="option"]:has-text("购物车")').click()

        await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
        await page.locator('input[placeholder="粘贴商品链接"]').fill(link)
        add_button = page.locator('span:has-text("添加链接")')
        klass = await add_button.get_attribute("class") or ""
        if "disable" in klass:
            return False
        await add_button.click()
        await page.wait_for_timeout(2000)

        if await page.locator("text=未搜索到对应商品").count():
            confirm = page.locator('button:has-text("确定")')
            if await confirm.count():
                await confirm.click()
            return False

        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        await page.locator('input[placeholder="请输入商品短标题"]').fill(title[:10])
        await page.wait_for_timeout(1000)
        finish = page.locator('button:has-text("完成编辑")')
        klass = await finish.get_attribute("class") or ""
        if "disabled" in klass:
            cancel = page.locator('button:has-text("取消")')
            if await cancel.count():
                await cancel.click()
            else:
                await page.locator(".semi-modal-close").click()
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return False
        await finish.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[publish] 设置商品链接失败（继续发布）：%s\n" % str(exc)[:160])
        return False


async def select_bgm(page, name: str) -> bool:
    """图文选背景音乐（可选步骤：搜不到就跳过，不阻断发布）。"""
    wanted = (name or "").strip()
    if not wanted:
        return True
    try:
        entry = page.locator('text="选择音乐"').nth(1)
        if not await entry.count():
            entry = page.locator('text="选择音乐"').first
        await entry.wait_for(state="visible", timeout=10000)
        await entry.click()

        sidesheet = page.locator(".semi-sidesheet-content").first
        await sidesheet.wait_for(state="visible", timeout=8000)
        search = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
        await search.wait_for(state="visible", timeout=5000)
        await search.fill(wanted)
        await search.press("Enter")
        await asyncio.sleep(2)

        card = sidesheet.locator(".card-container-tmocjc").first
        try:
            await card.wait_for(state="visible", timeout=8000)
        except Exception:  # noqa: BLE001
            await _close_sidesheet(page)
            return False
        # ★「使用」按钮的 visibility:hidden，普通 click 无效，只能走 JS
        apply_button = card.locator(".apply-btn-LUPP0D").first
        await apply_button.evaluate("el => el.click()")
        try:
            await sidesheet.wait_for(state="hidden", timeout=5000)
        except Exception:  # noqa: BLE001
            await _close_sidesheet(page)
        return True
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[publish] 选 BGM 失败（跳过）：%s\n" % str(exc)[:160])
        try:
            await _close_sidesheet(page)
        except Exception:  # noqa: BLE001
            pass
        return False


async def _close_sidesheet(page) -> None:
    try:
        close = page.locator(".semi-sidesheet-close").first
        if await close.count() and await close.is_visible():
            await close.click()
            await asyncio.sleep(1)
    except Exception:  # noqa: BLE001
        pass


async def enable_sync_switch(page) -> Optional[bool]:
    """发布页上的同步开关（如"同步到头条"）：没开就点开。

    ★ 组件是 semi-switch：点了 `div` 不一定生效，得点里面那个原生 input。
      也**必须**先判断当前状态 —— 无脑点会把用户本来开着的开关关掉。
    """
    selector = '[class^="info"] > [class^="first-part"] div div.semi-switch'
    try:
        node = page.locator(selector)
        if await node.count() == 0:
            return None
        klass = await page.eval_on_selector(selector, "div => div.className")
        if "semi-switch-checked" in klass:
            return False
        control = node.locator("input.semi-switch-native-control")
        if await control.count():
            await control.click()
        return True
    except Exception:  # noqa: BLE001
        return None


# ── 短信二次验证 ─────────────────────────────────────────────


def read_verify_code(code_file: str) -> str:
    """读验证码文件（发布时平台可能要求短信二次验证）。"""
    try:
        if code_file and os.path.exists(code_file):
            with open(code_file, encoding="utf-8") as handle:
                return handle.read().strip()
    except OSError:
        pass
    return ""


async def submit_verify_code(page, sms_input, code: str, code_file: str) -> bool:
    await sms_input.click()
    await sms_input.fill(code)
    await page.wait_for_timeout(500)
    button = page.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")').first
    if await button.count() and await button.is_visible():
        try:
            await button.click(force=True)
        except Exception:  # noqa: BLE001
            await page.eval_on_selector("div.uc-ui-verify_sms-verify_button", "el => el.click()")
    else:
        by_text = page.get_by_text("验证", exact=True).first
        if await by_text.count():
            await by_text.click(force=True)
        else:
            await page.keyboard.press("Enter")
    try:
        if code_file and os.path.exists(code_file):
            os.remove(code_file)   # 用完即删：里面是一次性验证码
    except OSError:
        pass
    await page.wait_for_timeout(3000)
    return True


async def sms_input_locator(page):
    return page.locator(
        'input[placeholder*="验证码"], input[type="tel"], '
        'input[placeholder*="短信"], input[placeholder*="手机号"]'
    ).first


__all__ = [
    "apply_collection",
    "apply_self_declaration",
    "clear_blocking_overlays",
    "enable_sync_switch",
    "fill_title_and_description",
    "handle_auto_video_cover",
    "logs",
    "native_click",
    "read_verify_code",
    "select_bgm",
    "set_product_link",
    "set_schedule_time",
    "set_thumbnail",
    "sms_input_locator",
    "submit_verify_code",
]
