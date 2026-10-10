"""创建 Gitee v0.3.4 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python mk_release_v034.py
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.4"
NAME = "v0.3.4：真正修掉「被拉起时弹窗」—— 改用 --noconsole + 从 fd 接回标准流"
BODY = """v0.3.3 **没有真正修好**这个弹窗（已发的那个版本请直接升级到本版）。

## 为什么 v0.3.3 的修法无效

v0.3.3 保留 console 子系统、运行时用 `GetConsoleWindow()` + `ShowWindow(SW_HIDE)`
隐藏窗口。实测**无效**，原因是：

- **现代 Windows 的默认终端是 Windows Terminal**。分配控制台会**弹出 WT 窗口**
  （实测拿到 `CASCADIA_HOSTING_WINDOW_CLASS`，窗口标题就是本 exe 的路径）。
- 这时 `GetConsoleWindow()` 返回的是 **`PseudoConsoleWindow`（中间层）**，
  **`ShowWindow` 藏它根本关不掉 Windows Terminal 的那个窗口**。

而 v0.3.3 的"验证通过"是**漏检**：只枚举了 exe 进程**自己**的窗口
（那里确实只有不可见的 `PseudoConsoleWindow`），**没有枚举
`WindowsTerminal.exe` 的窗口** —— 真窗口一直在那儿。

## 本版改法：从根上不分配控制台

打包加 **`--noconsole`**（PE subsystem 改成 GUI → Windows 不分配控制台、不弹任何窗口）。

它有一个代价，本版一并解决：

- `--noconsole` 会让 PyInstaller 把 `sys.stdin/stdout/stderr` 置成 **None**，
  而 **stdio 模式下 stdout 就是 MCP 协议通道**（规范要求 UTF-8 的 JSON-RPC 走它）
  —— 只加 `--noconsole` 不处理，**stdio 形态会静默失效**。
- 但 stdio 模式下宿主是**用管道**连接子进程 fd 0/1/2 的，**文件描述符本身仍然有效**，
  只是没挂 Python 包装对象。于是新增
  `__main__._restore_std_streams_if_needed()`：从 fd 0/1/2 手动
  `io.open` + `TextIOWrapper(encoding="utf-8")` 接回来；
  宿主没给管道时（手动双击）挂 `os.devnull`，这样 `print` 不会崩。

**两者必须成对存在，缺一即坏** —— `build_exe.ps1` 里已写明这条约束。

## 验证（三项都实测过，用 `--noconsole` 打包的候选版）

```
① --http 模式
   基线：当前已有 2 个可见控制台窗口
   拉起后 5 秒内 —— ✅ 没有出现任何**新的**可见控制台窗口（含 Windows Terminal）
   ✅ /health → HTTP 200

② stdio 模式（--noconsole 的最大风险点）
   stdout 收到 2 行：
     {"id": 1, "result": {"protocolVersion": "2024-11-05", ...}}   ← initialize 正常
     {"id": 2, "result": {"tools": [{"name": "douyin_account_status", ...}}}
   stderr: [douyin-publish-mcp] stdio 就绪：素材目录=(未设置=不限制) 账号=main …
   ✅ stdio 通道可用 —— 标准流恢复成功
```

① 的判据特意**把 `WindowsTerminal.exe` 算进去** —— 上一版就是漏了它才误判。

单测 **186 条全过**。

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体，11,163,819 字节 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（哈希与 v0.3.3 一致，内容未变） |
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = os.path.join(
    DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.4 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.4/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
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
    boundary = "----dyv034boundary"
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
