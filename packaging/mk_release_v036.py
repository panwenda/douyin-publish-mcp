"""创建 Gitee v0.3.6 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python packaging\\mk_release_v036.py
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.6"
NAME = "v0.3.6：修「服务拉不起来」—— --noconsole 的兜底标准流挂成了二进制"
BODY = """一个**只影响"换个方式拉起"**的启动崩溃，但是会被误判成"日志代码写错了"。

## 症状

服务拉不起来，控制台/日志里只有这么一段：

```
File "douyin_publish_mcp\\__main__.py", line 186, in main
File "douyin_publish_mcp\\http_server.py", line 526, in serve_http
TypeError: a bytes-like object is required, not 'str'
```

traceback 指向 `http_server.serve_http` 里那句 **"HTTP 服务已启动…" 的日志** ——
看着像日志写错了。**不是。** httpd 其实**已经绑定成功**（否则走不到那一行），
是崩在写第一行日志上，进程随即退出。

## 真因

`__main__._restore_std_streams_if_needed()` 的兜底分支把**二进制流**挂给了 `sys.stderr`：

```python
except OSError:
    setattr(sys, name, open(os.devnull, mode))     # mode="wb" ⇒ BufferedWriter
```

背景：`--noconsole` 打包（修"被拉起时弹窗"用的）会把 `sys.stdin/stdout/stderr`
置成 **None**，所以要手动从 fd 接回来。当**宿主传下来的 std 句柄无效**时
（`open(fd, "wb")` 抛 `OSError: [WinError 6] 句柄无效`）走兜底 ——
而兜底挂上去的是 `BufferedWriter`，之后任何 `sys.stderr.write("字符串")` 都抛 `TypeError`。

## 为什么平时碰不到

客户端的 `local_service` 用 `Stdio::null()` 拉起（Rust 打开 NUL 设备）→
给的是**有效句柄** → 走 `open(fd)` 成功分支 → 接回来的是 `TextIOWrapper`，一切正常。
**只有宿主不给有效 std 句柄时才会踩到**（手工双击、某些启动器/服务包装）。
所以表现是"**平时好好的，报错时服务却起不来**"。

## 修法

两个分支**统一**包一层，而不是在 `except` 里直接 `setattr`：

```python
try:
    raw = open(fd, mode, buffering=0, closefd=False)
except OSError:
    try:
        raw = open(os.devnull, mode)
    except OSError:
        continue
setattr(sys, name, io.TextIOWrapper(raw, encoding="utf-8", errors="replace", write_through=True))
```

## 验证

- 本地**一字不差复现**了原报错：
  `open(99999,"wb") -> OSError: [WinError 6] 句柄无效。兜底流类型: BufferedWriter`
  → `★★ 复现成功 -> a bytes-like object is required, not 'str'`
- 新增 2 条回归测试（`tests/test_server.py::TestStdStreamRestore`）：
  ① 句柄无效时兜底流**也必须能写字符串**（必须真的 `write(str)` ——
     只断言"流不是 None"抓不住，二进制流也不是 None）；
  ② 恢复后必须是 `TextIOWrapper` 且 encoding=utf-8。
- **全量 216 条测试全过**。

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（内容未变，哈希与 v0.3.3 起一致） |

> 驱动 zip 也传了 —— `DRIVER_URL_DEFAULT` 已随版本号改成 `/download/v0.3.6/`，
> 不传的话新机"按需取驱动"会 404。
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = os.path.join(
    DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.6 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.6/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
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
    boundary = "----dyv036boundary"
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
