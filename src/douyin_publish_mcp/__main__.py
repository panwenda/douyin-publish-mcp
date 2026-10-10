"""命令行入口：一个包，三种形态。

```bash
# stdio（默认；宿主当子进程拉起，registry 里 transportType=stdio）
douyin-publish-mcp

# streamable_http（本机常驻；registry 里 transportType=streamable_http）
douyin-publish-mcp --http --port 18080 --token <secret>
```

另外有两个**隐藏入口**（不写进 --help，它们是本程序自己拉起的内部约定，
别去命令行敲）：

- `--browser-helper <spec>`：浏览器通道的宿主 —— 读页面、听页面自己发的请求；
- `--creator-helper <spec>`：创作者中心自动化 —— 扫码登录、发布视频/图文。

两个都跑在本程序自己里（冻结后就是同一个 exe），好处是 exe 里**不放**浏览器驱动
（patchright 那 ~100MB），驱动按需取、只挂在子进程的 PYTHONPATH 上。

★ 默认必须是 stdio：已有的商店配置就是按 stdio 发布的，改成"必须带 --http"
  会让那批配置在升级后静默起不来（MCP 子进程既不握手也不报错，只表现为"工具没了"）。
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .http_server import serve_http
from .config import RuntimeConfig
from .server import SERVER_NAME, Server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="douyin-publish-mcp",
        description=(
            "抖音发布与读取：发布视频/图文、扫码登录与登录态检查，"
            "并搜索视频、看作品详情与无水印直链、看用户主页/我的主页（stdio 或 streamable_http）"
        ),
    )
    p.add_argument("--http", action="store_true", help="以本机常驻 HTTP 服务运行（默认 stdio）")
    p.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址（默认只绑本机 127.0.0.1）")
    p.add_argument("--port", type=int, default=18080, help="HTTP 端口（默认 18080）")
    p.add_argument(
        "--token",
        default="",
        help="HTTP 鉴权 token（留空则读 AUTH_TOKEN 环境变量；绑定非本机地址时必填）",
    )
    p.add_argument("--version", action="version", version="%s %s" % (SERVER_NAME, __version__))
    p.add_argument("--browser-helper", metavar="SPEC", default="", help=argparse.SUPPRESS)
    p.add_argument("--creator-helper", metavar="SPEC", default="", help=argparse.SUPPRESS)
    return p


def _hide_console_window() -> None:
    """把本进程**刚被分配到的**控制台窗口藏起来（仅 Windows）。

    ★★ 为什么需要它（2026-10-10 用户实测反馈："为什么会拉起一个 cmd"）：
      本项目为了保住 stdio 模式的 stdout（那是 MCP 协议通道）用 PyInstaller
      **console 版**打包。而 console 版 bootloader 在「父进程没有控制台」时
      会**自己 AllocConsole**（好让 `print` 有地方输出）——
      于是客户端以 `CREATE_NO_WINDOW | DETACHED_PROCESS` 拉起它时，
      屏幕上会**多出一个黑窗口常驻**。

      对照：小红书那个是 Go 编的原生单进程，没有这一层，所以不会弹窗。
      客户端侧无可指责 —— 它已经传了 `CREATE_NO_WINDOW`，
      但 bootloader 分配控制台是**它自己的行为**，父进程说了不算。

    ★ 为什么不干脆用 `--noconsole` 打包：那会把 `sys.stdout` 置成 **None**，
      **直接废掉 stdio 模式** —— 而 stdout 就是 MCP 协议通道（规范要求 UTF-8
      的 JSON-RPC 走它）。服务模式（`--http`）不需要可见控制台，
      所以保留 console 子系统、运行时把窗口藏掉，两边都不牺牲。

    ★ 逃生舱：`DOUYIN_KEEP_CONSOLE=1` 时不藏（手动双击调试、
      或排查"启动即退出"这类问题时要看 `print` 输出）。

    ★ 藏窗口失败**不影响服务**（多一个窗口而已），所以整段吞异常。
    """
    if not sys.platform.startswith("win"):
        return

    keep = (os.environ.get("DOUYIN_KEEP_CONSOLE") or "").strip().lower()
    if keep not in ("", "0", "false", "no", "off"):
        return

    try:
        import ctypes

        # GetConsoleWindow 返回**本进程关联的**那个控制台窗口；
        # 没有控制台（例如宿主已经给了管道）时返回 0，此时什么都不用做。
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            SW_HIDE = 0
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


def _force_utf8_stdio() -> None:
    """把 stdin/stdout/stderr 钉成 UTF-8。

    ★ 为什么必须显式做：stdio 形态下 stdout 就是 MCP 协议通道，规范要求 UTF-8。
      但 Windows 上 Python 默认按 `locale.getpreferredencoding()` 取 **cp936**，
      而 `json.dumps(..., ensure_ascii=False)`（见 `server.serve`）会把中文**原样**写出去
      → 客户端按 UTF-8 读到的全是乱码 / `U+FFFD`（JSON 结构还在，坏的是值，
      表现成"工具列得出来、但描述和所有中文返回都是乱码"，很难第一时间联想到编码）。

    ★ 为什么连 stderr 一起钉：它不是协议通道，但**承载用户可读的中文诊断**
      （启动就绪行、"收到非 JSON 行"、"工具 xx 异常"…）。客户端会把 stderr 收进日志面板，
      那一侧按 UTF-8 解 —— 不钉的话日志全是乱码，排查时反而被误导。
      实测：只钉 stdout 后，stderr 仍然写 GBK（`\xbe\xcd\xd0\xf7` = "就绪"）。

    ★ 为什么不能只靠 `PYTHONUTF8=1`：源码方式跑时它有效，但**打包成 exe 后启动环境
      由宿主决定** —— 实测同一个 shell 里 exe 仍然写 GBK（`PYTHONIOENCODING`/
      `PYTHONUTF8` 都传了也没用）。所以只能自己钉。

    `reconfigure` 是 3.7+ 的流方法；重定向到非文本流时没有它，故用 getattr 兜住。
    对 `--creator-helper` / `--browser-helper` 同样生效（它们本来就 `ensure_ascii=True`，
    这里是双保险）。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            # 流已关闭/不可重配（极少见）：不值得让服务起不来，交给默认行为。
            pass


def main(argv=None) -> int:
    # ★ 必须最早执行：下面任何一句都可能往 stdout/stderr 写中文。
    _force_utf8_stdio()
    # ★ 紧随其后：把 console 版 bootloader 自己分配的那个黑窗口藏掉
    #   （见 _hide_console_window 的说明；对 --http / stdio / 两个 helper 一并生效）
    _hide_console_window()
    args = build_parser().parse_args(argv)
    if args.browser_helper:
        from .browser_helper import main as helper_main

        return helper_main([args.browser_helper])
    if args.creator_helper:
        from .creator_helper import main as creator_main

        return creator_main([args.creator_helper])
    cfg = RuntimeConfig.from_env()
    if args.http:
        import os

        token = (args.token or os.environ.get("AUTH_TOKEN") or "").strip()
        serve_http(cfg, host=args.host, port=args.port, token=token)
        return 0
    # stdio：与既有商店配置完全一致的行为
    sys.stderr.write(
        "[%s] stdio 就绪：素材目录=%s 账号=%s 超时=%ss\n"
        % (SERVER_NAME, cfg.media_dir or "(未设置=不限制)", cfg.account, cfg.timeout)
    )
    sys.stderr.flush()
    Server(cfg).serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
