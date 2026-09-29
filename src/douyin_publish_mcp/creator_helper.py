"""`--creator-helper <spec.json>` 的入口：读 spec → 干一件事 → 吐**一行** JSON。

与 `browser_helper` 同一套约定：

- 进度/日志一律写 stderr（调用方拿来显示"现在到哪一步了"）；
- stdout 只有最后那一行 JSON，`ensure_ascii=True`（跨代码页不会变乱码）；
- 退出码 0 表示成功、1 表示失败 —— 但**结论以 JSON 为准**，退出码只是给 shell 看的。

这样主进程（服务本身）不需要浏览器驱动：exe 保持小，驱动按需取。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict


def _fail(kind: str, message: str, hint: str = "") -> Dict[str, Any]:
    return {"ok": False, "error": {"kind": kind, "message": message, "hint": hint}}


async def _run(spec: Dict[str, Any]) -> Dict[str, Any]:
    action = str(spec.get("action") or "")
    if action == "login":
        from .creator import login

        return await login(spec)
    if action == "publish_video":
        from .creator_publish import publish_video

        return await publish_video(spec)
    if action == "publish_note":
        from .creator_publish import publish_note

        return await publish_note(spec)
    return _fail("bad_spec", "不认识的 action：%r" % action, "这是内部约定，升级本服务即可。")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("用法：--creator-helper <spec.json>\n")
        return 2
    payload: Dict[str, Any]
    try:
        # utf-8-sig：调用方是我们自己（不带 BOM），但手写/记事本改过的 spec 可能带，
        # 带 BOM 会让 json.loads 直接报"Unexpected UTF-8 BOM" —— 不值得为它浪费一次排查。
        spec = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 —— spec 坏了也要给出一行 JSON
        payload = _fail("bad_spec", "读不了 spec：%s" % str(exc)[:200])
    else:
        try:
            payload = asyncio.run(_run(spec))
        except ImportError as exc:
            from .browser_helper import default_driver_dir

            payload = _fail(
                "no_driver",
                "这个解释器里没有 patchright/playwright：%s" % exc,
                "浏览器驱动没就位。落到 %s（zip 内含 patchright/ 即可），"
                "或用 DOUYIN_BROWSER_SITE_PACKAGES 指到本机已有的那份。" % default_driver_dir(),
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都要变成一行 JSON
            payload = _fail("crash", "%s: %s" % (type(exc).__name__, str(exc)[:400]))

    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
