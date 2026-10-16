"""创建 Gitee v0.3.7 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python packaging\\mk_release_v037.py
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.7"
NAME = "v0.3.7：查登录态支持一次多个账号 —— douyin_account_status 的 accounts / all"
BODY = """`douyin_account_status` 支持**一次查询多个账号**的登录态与当前登录用户（多账号运营不用再一个个调）。

## 怎么用

`account` 从必填改为**可选**，并新增可选参数 `accounts`（数组）：

| 传法 | 行为 |
| --- | --- |
| `accounts: ["main", "acc2"]` | 一次查这两个号，逐行回报登录态与昵称 |
| `accounts: ["all"]`（或 `"*"`） | 按凭据文件**自动展开**本机全部已配置账号 |
| `account` / 都不传 | 与 v0.3.6 一致：单账号（不传=默认账号 `DOUYIN_ACCOUNT`） |

返回逐行：`- main：已登录（昵称：张三）。` ／ `- side：未登录 —— 下一步：douyin_account_login(...)`。

## 要点

- **仍不吃账号锁**：`douyin_account_status` 本来就只打一次直连探针
  （`/aweme/v1/web/user/profile/self/`，**不开浏览器**），批量也保持同一口径。
- **账号发现**：`account.list_accounts()` 按**凭据文件**枚举（`douyin_*.json`；
  默认账号还认 `DOUYIN_COOKIE` / `DOUYIN_COOKIE_FILE`），没有凭据的账号**不猜**。
- **三种结论的语义不变**：已登录 / 确定未登录 / **判不出来**（被风控挡时如实说，不猜"没登录"）。
- 用户名（昵称）现在会随结论结构化带回（`AccountCheck.nickname`），批量清单里逐行显示。

## 验证

- 全量 **219** 条测试全过（新增 3 条：批量多号 / `all` 展开 / `account` 可省略）
- 真实冒烟（两个真实账号一次调用）：

```
共查 2 个账号：已登录 2，未登录 0。
- main：已登录（昵称：小潘同学）。
- acc2：已登录（昵称：好好吃饭）。
```

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（内容未变，哈希与 v0.3.3 起一致） |

> 驱动 zip 也传了 —— `DRIVER_URL_DEFAULT` 已随版本号改成 `/download/v0.3.7/`，
> 不传的话新机"按需取驱动"会 404。
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = os.path.join(
    DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.7 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.7/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
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
    boundary = "----dyv037boundary"
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
