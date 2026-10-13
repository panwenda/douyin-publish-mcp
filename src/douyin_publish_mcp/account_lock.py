"""账号级互斥：**同账号串行、不同账号并行**（进程内）。

## 为什么需要它

本服务是**一个常驻进程**（`--http` 形态下 `ThreadingHTTPServer`，每个请求一个线程），
而每个账号只有**一份** `douyin_<账号>.json`。同一账号的两个操作同时跑有两个后果：

1. **平台风控**：两个浏览器带同一份登录态、同一套指纹并发操作，平台侧看着就是
   "同一设备在并发操作"；
2. **凭据互相覆盖**：登录成功与发布成功**都会回写** storage_state（见 `creator.py`
   与 `creator_publish.py`），后写的会把先写的盖掉 —— 可能拿较旧的 cookie 覆盖较新的。

## 为什么不是一把全局锁

不同账号之间没有共享状态（凭据文件、指纹 seed 各自独立），锁在一起会让
"多账号发同样内容"退化成一条队列。小红书 MCP（`account_lock.go`）用的是同一口径，
两边保持一致的语义。

## 已知边界：等扫码**不占**锁

登录会起一个浏览器等用户扫码（最长几分钟）。那段时间**不占锁** ——
否则"扫完再调一次确认结果"会被自己堵死，正在跑的发布也会把轮询卡在后面。
（小红书 MCP 的 `accountSession.detach()` 是同一个取舍。）
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Dict, Iterator

from .config import account_key


class _Entry:
    """一个账号的锁 + 引用计数。"""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


class AccountLocks:
    """按账号分桶的互斥锁。

    ★ 引用计数归零就把条目摘掉：账号名是调用方给的，写错一个就多一条，
      常驻进程里不回收就是慢性泄漏。
    """

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._entries: Dict[str, _Entry] = {}

    @contextmanager
    def hold(self, account: str = "") -> Iterator[None]:
        """取到该账号的独占权；退出 `with` 块时释放。

        惯用写法：`with locks.hold(account): ...`

        ★ **键在这里统一归一化**（口径 = `config.account_key`），不推给每个调用方
          自己记得 —— 漏一处的后果是"同一份凭据被两把锁保护"，串行静默失效。
        ★ 等待期间不持有 `self._mu`，所以不同账号互不阻塞。
        """
        key = account_key(account)

        with self._mu:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            entry.refs += 1

        # ★ 必须放在 `self._mu` 之外：否则一个账号排队会把所有账号都堵住。
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            # 引用归零才摘除：摘早了会让后来者拿到一把**新**锁，
            # 与仍在排队的人并行 —— 串行就漏了。
            with self._mu:
                entry.refs -= 1
                if entry.refs == 0:
                    self._entries.pop(key, None)

    def tracked(self) -> int:
        """当前还在表里的账号数（测试与排查用）。"""
        with self._mu:
            return len(self._entries)


__all__ = ["AccountLocks"]
