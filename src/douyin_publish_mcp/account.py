"""账号态：登录态还算不算数、凭据文件在哪、退出登录该删什么。

## 为什么不再"开一个浏览器去看跳没跳登录页"

参考做法是打开创作者中心看有没有被踢到登录页 —— 那要起一整个浏览器（几秒到几十秒）。
本服务本来就有直连客户端：`/aweme/v1/web/user/profile/self/` 是**必须带登录态**的接口，
拿它当探针又快又准；cookie 过期/缺失它会明确回 `no_credential`。

## 三种结论不能混为一谈

- `logged_in=True`：拿到自己的资料 —— 登录态有效；
- `logged_in=False`：**确定**没登录（凭据文件不存在、或里面没有登录标识）；
- `logged_in=None`：**判不出来**（网络不通 / 被风控挡 / 接口改了）——
  这种情况绝不能对用户说"你没登录"，那会把人骗去重扫一次码。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ConfigError, RuntimeConfig
from .douyin_cred import Credential, resolve_credential
from .douyin_web import DouyinWebClient, DouyinWebError, WebConfig


@dataclass
class AccountCheck:
    """一次登录态检查的结论（工具与状态页共用同一份）"""

    account: str
    logged_in: Optional[bool]
    ok: bool
    output: str
    at: float = field(default_factory=time.time)
    #: 会话正在等扫码时会**跳过**检查：这不是失败，调用方要区别对待
    skipped: bool = False
    source: str = ""


def credential_detail(cred: Credential) -> str:
    """凭据来自哪儿、文件什么时候写的 —— 出问题时用户要拿这些去核对。"""
    lines = ["账号：%s｜来源：%s" % (cred.account or "(未指定)", cred.source or "(未知)")]
    lines.append(cred.describe())
    if cred.path and cred.path.is_file():
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(cred.path.stat().st_mtime))
            lines.append("文件：%s（最后写入 %s）" % (cred.path, mtime))
        except OSError:
            pass
    return "\n".join(lines)


def check_account(cfg: RuntimeConfig, account: str = "") -> AccountCheck:
    """查一次登录态。**不联网**判定"确定没登录"，联网只为确认"能用"。"""
    cred = resolve_credential(cfg, account)
    if not cred.usable:
        return AccountCheck(
            account=cred.account,
            logged_in=False,
            ok=True,
            source=cred.source,
            output=(
                "**未登录**：没有可用的抖音凭据。\n%s\n\n"
                "下一步：调用 douyin_account_login（account=\"%s\"）让用户扫码。" % (
                    credential_detail(cred), cred.account or "main")
            ),
        )

    client = DouyinWebClient(cred, WebConfig())
    try:
        profile = client.my_profile()
    except DouyinWebError as exc:
        if exc.kind == "no_credential":
            return AccountCheck(
                account=cred.account, logged_in=False, ok=True, source=cred.source,
                output=("**未登录**：凭据里的登录标识已失效（%s）。\n%s\n\n"
                        "下一步：调用 douyin_account_login 重新扫码。"
                        % (exc.message, credential_detail(cred))),
            )
        # ★ blocked / upstream：可能只是被风控挡了一下，别据此说"没登录"
        return AccountCheck(
            account=cred.account, logged_in=None, ok=False, source=cred.source,
            output=(
                "**判不出来**：探针接口没成功（%s：%s）。\n%s\n\n"
                "这不代表没登录 —— 可以先用 douyin_search_videos 试一次：读得通就说明登录态没问题。"
                % (exc.kind, exc.message, credential_detail(cred))
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— 探针失败不该让工具崩
        return AccountCheck(
            account=cred.account, logged_in=None, ok=False, source=cred.source,
            output="**判不出来**：检查时出错（%s: %s）。\n%s"
                   % (type(exc).__name__, str(exc)[:200], credential_detail(cred)),
        )

    nickname = ""
    if isinstance(profile, dict):
        nickname = str(profile.get("nickname") or (profile.get("user") or {}).get("nickname") or "")
    return AccountCheck(
        account=cred.account,
        logged_in=True,
        ok=True,
        source=cred.source,
        output=(
            "**已登录**（账号「%s」%s），可以发布。\n%s\n\n"
            "判据：直连拿到「我的资料」（/aweme/v1/web/user/profile/self/）。"
            % (cred.account, ("，昵称：%s" % nickname) if nickname else "", credential_detail(cred))
        ),
    )


@dataclass
class LogoutPlan:
    """退出登录的"预演/结果"：不传 confirm 时只回报要删什么。"""

    account: str
    path: Path
    existed: bool
    deleted: bool = False
    error: str = ""

    def describe(self) -> str:
        if not self.existed:
            return "账号「%s」没有本机登录态文件，无需退出。\n（找的位置：%s）" % (self.account, self.path)
        if self.deleted:
            return "已删除账号「%s」的登录态文件：%s" % (self.account, self.path)
        if self.error:
            return "删除失败：%s\n文件：%s" % (self.error, self.path)
        return "将要删除账号「%s」的登录态文件：\n%s" % (self.account, self.path)


def logout(cfg: RuntimeConfig, account: str, confirm: bool = False) -> LogoutPlan:
    """退出登录 = 删除该账号的凭据文件（两步确认：不传 confirm 只回报，不删）。

    ★ 只删**凭据**，绝不碰素材目录、作品或其它文件 —— 这是本服务唯一的删除动作，
      所以宁可多问一次（与发布同源的两步门禁）。
    """
    cred = resolve_credential(cfg, account)
    path = cred.path or _default_credential_path(account)
    plan = LogoutPlan(account=cred.account or account, path=path, existed=bool(path and path.is_file()))
    if not plan.existed or not confirm:
        return plan
    try:
        path.unlink()
        plan.deleted = True
    except OSError as exc:
        plan.error = str(exc)
    return plan


def _default_credential_path(account: str) -> Path:
    from .config import credential_path

    return credential_path(account)


__all__ = ["AccountCheck", "LogoutPlan", "check_account", "credential_detail", "logout"]
