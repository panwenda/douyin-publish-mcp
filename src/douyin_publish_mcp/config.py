"""运行配置：从**我们自己的**环境变量读前提条件。

## 为什么不再有社会化的 `SAU_*`

v0.3.0 起本服务自带创作者中心自动化（登录 / 发布 / 账号检查，见 [`creator`]），
不再调用外部 CLI，因此那套 `SAU_CMD` / `SAU_DIR` / `SAU_UV` / `SAU_NO_SYNC` 全部作废。
环境变量统一到 `DOUYIN_` 前缀 —— 一个服务一个前缀，看名字就知道该配哪儿。

## 保留的那条硬约束

`DOUYIN_MEDIA_DIR` 是**允许发布的素材根目录**，也是本服务唯一的路径闸门：
发布工具的每一个文件路径都必须落在它里面。缺了它就**拒绝发布**（而不是退化成
"整个磁盘可读"）—— 宁可报"没配置"，也不能让模型顺着路径参数把任意文件发出去。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SERVER_NAME = "douyin-publish-mcp"

#: 单次调用的默认超时（发布要传大文件、还要等平台处理，别调太小）
DEFAULT_TIMEOUT = 900


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def data_dir() -> Path:
    """本服务的数据目录：凭据、二维码、验证码文件都放这儿。

    Windows 用 `%LOCALAPPDATA%\\douyin-publish-mcp\\data`（与商店配置里 exe 的落点同源），
    其它平台用 `~/.douyin-publish-mcp/data`。`DOUYIN_DATA_DIR` 可覆盖。
    """
    override = _env("DOUYIN_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = _env("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / SERVER_NAME / "data"
    return Path.home() / ".douyin-publish-mcp" / "data"


def default_account() -> str:
    """默认账号名：只读显式配置，**不猜**。"""
    return _env("DOUYIN_ACCOUNT") or "main"


def credential_path(account: str = "") -> Path:
    """登录态（storage_state）的**默认**落点：我们自己的数据目录下的 `douyin_<账号>.json`。

    ★ v0.3.0 起凭据由本服务自己管：登录工具写它 → 读取/发布工具读它 → 退出登录删它。
      一台机器上多个账号就是多个文件，互不覆盖。
    ★ 要用别的文件（比如从浏览器导出的那份），设 `DOUYIN_COOKIE_FILE` —— 显式配置永远优先。
    """
    name = (account or default_account()).strip() or "main"
    safe = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", name) or "main"
    return data_dir() / ("douyin_%s.json" % safe)


@dataclass
class RuntimeConfig:
    """一次运行需要的前提条件（全部来自环境变量 / 工具参数）。"""

    #: 允许发布的素材根目录（路径闸门，缺省=拒绝发布）
    media_dir: str = ""
    #: 登录与发布共用的账号名（凭据文件名里用它）
    account: str = "main"
    #: 单个工具调用的超时（秒）
    timeout: int = DEFAULT_TIMEOUT
    #: 发布用无头浏览器；**登录始终有头**（抖音会挑无头，且可能要输短信验证码）
    headless: bool = True
    #: 短信二次验证的验证码文件（发布过程中平台可能要二次验证）
    verify_code_file: str = ""
    #: 创作者中心自动化用的浏览器通道（默认系统 Chrome，省掉 170MB 下载）
    channel: str = "chrome"
    #: 调试：失败时截图留档
    debug: bool = False

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "RuntimeConfig":
        e = os.environ if env is None else env

        def s(key: str, default: str = "") -> str:
            return str(e.get(key, default) or "").strip()

        def b(key: str, default: bool) -> bool:
            raw = s(key)
            if not raw:
                return default
            return raw.lower() not in ("0", "false", "no", "off")

        try:
            timeout = int(s("DOUYIN_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        account = s("DOUYIN_ACCOUNT") or "main"
        verify = s("DOUYIN_VERIFY_CODE_FILE") or str(data_dir() / "verify_code.txt")
        return RuntimeConfig(
            media_dir=s("DOUYIN_MEDIA_DIR"),
            account=account,
            timeout=max(30, timeout),
            headless=b("DOUYIN_PUBLISH_HEADLESS", True),
            verify_code_file=verify,
            channel=s("DOUYIN_CREATOR_CHANNEL") or "chrome",
            debug=b("DOUYIN_DEBUG", False),
        )

    # ── 路径闸门 ────────────────────────────────────────────
    def media_root(self) -> Path:
        """素材根目录；没配就报错（而不是放行）。"""
        if not self.media_dir:
            raise ConfigError(
                "未配置素材目录（DOUYIN_MEDIA_DIR）。请到「MCP 配置」里填上**允许发布的素材根目录**"
                "（例如 D:\\\\douyin-media），发布工具的每个文件都必须落在它里面。"
            )
        root = Path(self.media_dir).expanduser()
        if not root.is_dir():
            raise ConfigError(f"素材目录不存在：{root}（请核对 DOUYIN_MEDIA_DIR）")
        return root

    def resolve_media_path(self, raw: str) -> Path:
        """把一个文件参数解析成媒体目录内的真实路径；越界/不存在都在这里拦住。

        ★ 相对路径按素材目录解析 —— 这样模型说"用 cover.png"时，指的是素材目录里那张，
          而不是服务进程的当前工作目录（那是个谁都说不准的地方）。
        """
        text = (raw or "").strip()
        if not text:
            raise ConfigError("文件参数不能为空。")
        root = self.media_root().resolve()
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            target = candidate.resolve()
        except OSError as exc:  # 路径非法（太长/含非法字符）也算配置问题
            raise ConfigError(f"路径无法解析：{text}（{exc}）") from None
        if target != root and root not in target.parents:
            raise ConfigError(
                f"拒绝访问素材目录之外的文件：{target}\n"
                f"只允许 {root} 里的文件（DOUYIN_MEDIA_DIR）。要发别的目录请先移进去。"
            )
        if not target.is_file():
            raise ConfigError(f"文件不存在：{target}")
        return target


class ConfigError(RuntimeError):
    """配置/路径错误：对模型可读，调用方应把它当成"让用户去改配置"而不是重试。"""


__all__ = [
    "DEFAULT_TIMEOUT",
    "SERVER_NAME",
    "ConfigError",
    "RuntimeConfig",
    "credential_path",
    "data_dir",
    "default_account",
]
