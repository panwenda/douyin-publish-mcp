"""a_bogus 签名（**当前刻意留空**）。

## 这份文件为什么存在

抖音 web 接口的请求参数里有一个 `a_bogus` 签名。它的生成逻辑是一坨
运行在浏览器里的混淆 JS，社区做法是"用 JS 引擎跑那段代码"或"把算法逆向移植"。
本服务两条都不做，理由：

1. **零三方依赖是本包的立身条件**（见 pyproject：内网机器要能 `uvx` 直接拉起），
   引入一个 JS 引擎或几百行逆向代码，等于把"装不上"的风险引回来；
2. **带登录 cookie 时，多数读取接口不校验它** —— 带上 ttwid + sessionid 后，
   搜索/详情/用户作品都能拿到数据（这也是 douyin-mcp-node 把搜索标成
   "不需要签名"的原因）。真正被拦时的症状很明确，见下。

## 被拦时是什么样（排错线索）

    {"status_code": 0, "status_msg": "blocked"}   ← 风控拦了，通常不是签名问题

先按这个顺序排查：① cookie 里有没有 `ttwid` ② `sessionid` 是否过期
（调 `douyin_account_login` 重扫）③ 请求头 UA 与浏览器是否一致。
这三条都排掉还拦，才是签名的事 —— 那时把 [`sign_a_bogus`] 补上实现即可，
调用方（`douyin_web`）无需改动。

## 接口约定

- 返回 `""` = 本次不携带签名（当前实现即如此，属**已知状态**，不是错误）；
- 需要签名的调用方会把签名情况写进工具输出，不假装签过。
"""

from __future__ import annotations

# ★ 明确写在模块里而不是藏在注释里：补实现时改这里，调用方什么都不用动。
A_BOGUS_IMPLEMENTED = False


def sign_a_bogus(query: str, user_agent: str, post_data: str = "") -> str:
    """返回 a_bogus 值；未实现时返回空串（= 请求不带签名）。

    `query` 是**待签名**的查询串（不含 a_bogus 本身），
    `post_data` 是 POST 的表单体（GET 传空）。
    """
    return ""


def sign_status() -> str:
    """给工具输出/状态页用的一句话（让"没签名"这件事在输出里可见）"""
    if A_BOGUS_IMPLEMENTED:
        return "已携带 a_bogus 签名"
    return "未携带 a_bogus 签名（依赖登录 cookie；若返回 blocked 请按 douyin_sign.py 的说明排查）"


__all__ = ["A_BOGUS_IMPLEMENTED", "sign_a_bogus", "sign_status"]
