"""PyInstaller 入口（仅打包用）。

为什么需要它：PyInstaller 的入口必须是**一个脚本文件**，而 `src/douyin_publish_mcp/__main__.py`
里有相对导入（`from . import config`），直接拿它当入口会在 `python __main__.py` 语义下失败
（那时它不是一个包内模块）。这里做一层最薄的转发，`--paths src` 保证包能被找到。
"""

from douyin_publish_mcp.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
