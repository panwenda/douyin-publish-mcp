"""命令行入口：一个包，两种形态。

```bash
# stdio（默认；宿主当子进程拉起，registry 里 transportType=stdio）
douyin-publish-mcp

# streamable_http（本机常驻；registry 里 transportType=streamable_http）
douyin-publish-mcp --http --port 18080 --token <secret>
```

★ 默认必须是 stdio：已有的商店配置就是按 stdio 发布的，改成"必须带 --http"
  会让那批配置在升级后静默起不来（MCP 子进程既不握手也不报错，只表现为"工具没了"）。
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .http_server import serve_http
from .sau import SauConfig
from .server import SERVER_NAME, Server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="douyin-publish-mcp",
        description=(
            "抖音发布与读取（social-auto-upload）：发布视频/图文，"
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
    # ★ 隐藏入口：浏览器通道的宿主就是本程序自己（`douyin-publish-mcp.exe --browser-helper <spec>`）。
    #   这样 exe 里不用放浏览器驱动（patchright 那 ~90MB），驱动按需取、挂在子进程的 PYTHONPATH 上。
    #   刻意不写进 --help：它不是给用户敲的，是 douyin_browser.py 拉起的内部约定。
    p.add_argument("--browser-helper", metavar="SPEC", default="", help=argparse.SUPPRESS)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.browser_helper:
        from .browser_helper import main as helper_main

        return helper_main([args.browser_helper])
    cfg = SauConfig.from_env()
    if args.http:
        import os

        token = (args.token or os.environ.get("AUTH_TOKEN") or "").strip()
        serve_http(cfg, host=args.host, port=args.port, token=token)
        return 0
    # stdio：与既有商店配置完全一致的行为
    sys.stderr.write(
        "[%s] stdio 就绪：SAU_CMD=%s SAU_DIR=%s 素材目录=%s\n"
        % (SERVER_NAME, cfg.cmd or "(未设置)", cfg.project_dir or "(未设置)", cfg.media_dir or "(未设置)")
    )
    sys.stderr.flush()
    Server(cfg).serve()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
