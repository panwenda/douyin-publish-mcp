"""创建 Gitee v0.3.5 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python packaging\\mk_release_v035.py
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.5"
NAME = "v0.3.5：多账号 —— 同账号串行、不同账号并行"
BODY = """一个进程同时管多个抖音号。**同账号串行、不同账号并行。**

## 为什么需要它

「同一篇内容发到多个号」原来会被排成一条队列 —— 但两个号之间本来没有任何共享状态
（不同的凭据文件、不同的浏览器、不同的指纹），排在一起纯属白等。

反过来，**同一个号**的两个操作**必须**排队：

- 两个浏览器带同一份登录态并发操作，平台侧看着就是「同一设备在并发操作」；
- 登录成功与发布成功**都会回写**登录态文件，后写的会盖掉先写的
  （可能拿较旧的 cookie 覆盖较新的）。

## 怎么改的

锁挂在 `Server.handle()` 的 `tools/call` 分支 —— 那是 **stdio 与 http 两种形态唯一**的
工具分发点，所以两种形态语义一致（http 下是真并发；stdio 天然串行，锁不改变行为）。

- 锁按**账号**分桶，且键在 **唯一入口**归一化：`" work "` ≡ `work` 是同一份凭据文件，
  不归一化就会出现"同一份 cookie 被两把锁保护"，**串行静默失效**。
- 引用计数回收：账号锁用干净了就销毁，长期运行不会攒一堆空锁。
- **例外：`douyin_account_status` 不吃锁** —— 它只打一次直连探针（**不开浏览器**），
  锁住会让"扫完查一下登录态"卡在正在跑的发布后面，正好把最需要它的时候堵死。

## 登录会话也按账号

- 每个号各留一个待扫码会话：登录 `work` **不会**顶掉 `personal` 还在等扫码的会话。
- **已经在等就复用**（重复调用只是"继续查那一个"，不会把用户手里那张二维码弄失效）；
  确实要换新码时传 **`force=true`**。
- **等扫码期间不占锁**：登录要起浏览器等用户扫码（最长几分钟），占着锁会让
  "扫完再调一次确认结果"被自己堵死。

顺带修掉一个**自锁死**：`last_check()` 在持锁状态下又去算键（`_resolve_key` 内部还要拿
同一把**非重入**锁）——症状是"工具没反应"而不是报错。现在键在锁外先算。

## 验证（都是实测，不是推演）

- 单测 **214 条全过**（新增账号锁 19 条 + 多账号登录会话 9 条）。
- 本地真实测试（真起 exe、真走 MCP、真连抖音、真账号）：
  同账号 ×2 = **2.47s ≈ 2×**（基准 1.25s，串行成立）｜ 不同账号 ×2 = **1.22s ≈ 1×**（并行成立）
  ｜ 登录复用 0.2s（第二次调用不再新开会话）｜ 两号会话并存 ｜ 凭据文件未被改动。
- 发布件自检：onefile / onedir 均 `--http` 起来 `/health` 报 **0.3.5**；
  **`WindowsTerminal.exe` 可见窗口增量为 0**（上一版就是漏了它才误判"不弹窗"）。

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体，11,172,244 字节 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包，11,003,796 字节 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（哈希与 v0.3.4 一致，内容未变） |

> 驱动 zip 也传了 —— `DRIVER_URL_DEFAULT` 已随版本号改成 `/download/v0.3.5/`，
> 不传的话新机"按需取驱动"会 404。
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = os.path.join(
    DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.5 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.5/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
DRIVER_SHA = "8a6de597f21574094d93ee86a1e965330fae32b29aaec458385abd5ae880b03d"
ASSETS = [
    "douyin-publish-mcp.exe",
    "douyin-publish-mcp-win64-onedir.zip",
    "douyin-browser-driver-win64-patchright-1.58.2.zip",
]

TOKEN = (os.environ.get("GITEE_TOKEN") or "").strip()
if not TOKEN:
    sys.exit("缺少 GITEE_TOKEN 环境变量。请先 set GITEE_TOKEN=<你的 Gitee 私人令牌>")

API = "https://gitee.com/api/v5"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8", "User-Agent": "curl/8"},
    )
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def upload(url, path):
    """multipart/form-data 手工拼。

    ★★ 坑：`Content-Type: application/octet-stream` 那行后面**必须**跟一个空行
        （即 `\\r\\n\\r\\n`）—— 少一个换行 Gitee 直接 **400**，
        且症状迷惑：Release 建成、id 也返回了，看着成功其实资产一个没传上。
    """
    boundary = "----dyv035boundary"
    fn = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % fn).encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",   # ← 这个空行不能少
        data,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ])
    url = url + ("&" if "?" in url else "?") + "access_token=" + TOKEN
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "User-Agent": "curl/8"},
    )
    with opener.open(req, timeout=1800) as r:
        return r.status, r.read().decode("utf-8", "replace")


print("=== 本地资产自检 ===")
for name in ASSETS:
    p = os.path.join(DIST, name)
    if not os.path.isfile(p):
        sys.exit("资产不存在：%s" % p)
    size = os.path.getsize(p)
    sha = hashlib.sha256(open(p, "rb").read()).hexdigest()
    print("  %-52s %12d 字节" % (name, size))
    print("      sha256=%s" % sha)

got = hashlib.sha256(open(DRIVER_SRC, "rb").read()).hexdigest()
if got != DRIVER_SHA:
    sys.exit("★ 驱动 zip 哈希与 DRIVER_SHA_DEFAULT 不一致：\n  实际 %s\n  登记 %s" % (got, DRIVER_SHA))
print("  驱动 zip 哈希校验通过")

print("\n=== 创建 Release ===")
rel = post("%s/repos/%s/%s/releases?access_token=%s" % (API, OWNER, REPO, TOKEN), {
    "tag_name": TAG,
    "name": NAME,
    "body": BODY,
    "prerelease": False,
    "target_commitish": "main",
})
rid = rel["id"]
print("Release 已创建: id=%s tag=%s" % (rid, rel.get("tag_name")))

print("\n=== 上传资产 ===")
for name in ASSETS:
    path = os.path.join(DIST, name)
    status, text = upload(
        "%s/repos/%s/%s/releases/%s/attach_files" % (API, OWNER, REPO, rid), path)
    print("  上传 %-52s HTTP %s" % (name, status))
    if status not in (200, 201):
        print("    ⚠️ 响应: %s" % text[:300])

print("\n完成。Release 页面：https://gitee.com/%s/%s/releases/tag/%s" % (OWNER, REPO, TAG))
print("★ 别忘了**验证直链 HTTP 码 + 远端 sha256 与本地一致**")
