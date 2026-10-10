"""创建 Gitee v0.3.3 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python mk_release_v033.py

令牌在 Gitee → 设置 → 私人令牌 生成，至少要有 projects 权限。

★ 与 mk_release_v032.py 的差别只有版本号、说明与驱动源路径
  （驱动源改到 dist/ 下 —— build/v021/ 那份是老布局，已不在）。
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.3"
NAME = "v0.3.3：修「被客户端拉起时多一个黑窗口」（PyInstaller console bootloader 自己分配控制台）"
BODY = """用户实测反馈：「为什么会拉起一个 cmd，`douyin-publish-mcp.exe`，
小红书的都没有拉起这个 cmd」。

## 根因：console 版 bootloader 在「父进程没有控制台」时**自己分配控制台**

本项目为了保住 stdio 模式的 stdout（那是 MCP 协议通道）用 PyInstaller
**console 版**打包。而 console 版 bootloader 在父进程没有控制台时会
**自己 `AllocConsole`**（好让 `print` 有输出）——
于是客户端以 `CREATE_NO_WINDOW | DETACHED_PROCESS` 拉起它时，
屏幕上**多出一个黑窗口常驻**。

**客户端侧无可指责**：它已经传了 `CREATE_NO_WINDOW`，
但分配控制台是 bootloader 自己的行为，父进程说了不算。

对照：小红书那个是 **Go 编译的原生单进程**，没有这一层，所以不弹窗。

实测证据（以与客户端完全相同的标志手动拉起）：

```
Popen([exe], creationflags=CREATE_NO_WINDOW|DETACHED_PROCESS)
  → pid=41656 (bootloader)
  → pid=55760 (ppid=41656)      ★ 它自己 fork 的子进程
```

## 为什么不干脆用 `--noconsole` 打包

那会把 `sys.stdout` 置成 **None** —— 而 **stdio 模式下 stdout 就是
MCP 协议通道**（规范要求 UTF-8 的 JSON-RPC 走它）。加了就等于
**废掉 stdio 形态**：那批已按 stdio 发布的商店配置会静默起不来
（子进程既不握手也不报错，只表现为"工具没了"）。

## 改法：保留 console 子系统，**运行时把窗口藏掉**

新增 `__main__._hide_console_window()`：
`GetConsoleWindow()` + `ShowWindow(SW_HIDE)`，在 `main()` 最早期调用
（紧跟 `_force_utf8_stdio()`），对 `--http` / stdio / 两个 helper **一并生效**。

- **逃生舱**：`DOUYIN_KEEP_CONSOLE=1` 时不藏 —— 手动双击调试、
  或排查"启动即退出"时要看 `print` 输出
- 非 Windows 直接返回；藏窗口失败**不影响服务**（整段吞异常，顶多多一个窗口）
- `build_exe.ps1` 末尾加了注释钉住"别改回 `--noconsole`"，防后来者当优化改回去

## 验证

**直接证据**（用 `EnumWindows` 枚举该进程树的顶层窗口）：

```
=== 进程树（3 个）===
=== 顶层窗口 ===
  pid=8548   class='PyInstallerOnefileHiddenWindow'  visible=False
  pid=23912  class='PseudoConsoleWindow'              visible=False   ← 控制台存在但已隐藏
✅ 没有任何**可见的**控制台窗口 —— 弹窗修复生效
✅ 18080 已监听（服务正常启动）
```

`PseudoConsoleWindow` 仍在（说明 bootloader 确实分配了控制台），
但**不可见** —— 这正是本次修复的目标。

单测 **186 条全过**。

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体，11,167,756 字节 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（哈希与 v0.3.2 一致，内容未变） |
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = os.path.join(
    DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.3 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.3/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
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
    """multipart/form-data 手工拼（标准库没有方便的 multipart 封装）。

    ★★ 坑：`Content-Type: application/octet-stream` 那行后面**必须**跟一个空行
        （即 `\\r\\n\\r\\n`）—— 少一个换行 Gitee 直接 **400**，
        且症状迷惑：Release 建成、id 也返回了，看着成功其实资产一个没传上。
    """
    boundary = "----dyv033boundary"
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


# ── 0. 前置自检 ──
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
print("  驱动 zip 哈希校验通过（与代码里的 DRIVER_SHA_DEFAULT 一致）")

# ── 1. 建 Release ──
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

# ── 2. 传三个资产 ──
print("\n=== 上传资产 ===")
for name in ASSETS:
    path = os.path.join(DIST, name)
    status, text = upload(
        "%s/repos/%s/%s/releases/%s/attach_files" % (API, OWNER, REPO, rid), path)
    print("  上传 %-52s HTTP %s" % (name, status))
    if status not in (200, 201):
        print("    ⚠️ 响应: %s" % text[:300])

print("\n完成。Release 页面：https://gitee.com/%s/%s/releases/tag/%s" % (OWNER, REPO, TAG))
print("★ 别忘了**验证直链 HTTP 码 + 远端 sha256 与本地一致**（只看 200 不够）")
