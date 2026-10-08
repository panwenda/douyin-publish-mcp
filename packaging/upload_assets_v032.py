"""向**已存在**的 v0.3.2 Release 补传三个资产（不新建 Release）。

背景：首次执行时 Release 已建成（id=1190360），但上传因 multipart 头少了
一个换行（`\r\r\n` 应为 `\r\n\r\n`）报 HTTP 400 —— Release 空着资产。

用法（令牌从环境变量读，不落盘）：
    set GITEE_TOKEN=<你的私人令牌>
    python upload_assets_v032.py
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.2"
DRIVER_SHA = "8a6de597f21574094d93ee86a1e965330fae32b29aaec458385abd5ae880b03d"
DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
ASSETS = [
    "douyin-publish-mcp.exe",
    "douyin-publish-mcp-win64-onedir.zip",
    "douyin-browser-driver-win64-patchright-1.58.2.zip",
]

TOKEN = (os.environ.get("GITEE_TOKEN") or "").strip()
if not TOKEN:
    sys.exit("缺少 GITEE_TOKEN 环境变量")

API = "https://gitee.com/api/v5"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def upload(url, path):
    """multipart/form-data 手工拼。

    ★ header 与 body 之间必须是**空行**（`\r\n\r\n`）；写成 `\r\r\n` 会让
      Gitee 解析 multipart 失败并返回 **HTTP 400**（踩过一次）。
    """
    boundary = "----dyv032boundary"
    fn = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % fn).encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ])
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        url + sep + "access_token=" + TOKEN, data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "User-Agent": "curl/8"},
    )
    try:
        with opener.open(req, timeout=1800) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        #★ 把 Gitee 的错误体打出来，否则只看到一个光秃秃的 400
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit("上传 %s 失败：HTTP %s\n%s" % (fn, exc.code, detail))


# ── 0. 自检 ────────────────────────────────────────────────
drv = os.path.join(DIST, "douyin-browser-driver-win64-patchright-1.58.2.zip")
got = hashlib.sha256(open(drv, "rb").read()).hexdigest()
if got != DRIVER_SHA:
    sys.exit("驱动 zip 哈希与 DRIVER_SHA_DEFAULT 不一致：%s" % got)
print("驱动 zip 哈希校验通过")

rel = get("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG))
rid = rel["id"]
have = {a.get("name") for a in (rel.get("assets") or [])}
print("复用 Release id=%s tag=%s（已有资产：%s）" % (rid, rel.get("tag_name"), have or "无"))

# ── 1. 补传 ────────────────────────────────────────────────
for name in ASSETS:
    path = os.path.join(DIST, name)
    if not os.path.isfile(path):
        sys.exit("dist 里缺资产：%s" % path)
    if name in have:
        print("  跳过（已存在）%-52s" % name)
        continue
    status, _text = upload(
        "%s/repos/%s/%s/releases/%s/attach_files" % (API, OWNER, REPO, rid), path)
    print("  上传 %-52s %12d 字节  HTTP %s" % (name, os.path.getsize(path), status))

# ── 2. 回读核对 ────────────────────────────────────────────
final = get("%s/repos/%s/%s/releases/tags/%s" % (API, OWNER, REPO, TAG))
print()
print("最终资产：")
for a in final.get("assets") or []:
    n = a.get("name") or ""
    if n.endswith((".exe", "-onedir.zip", "patchright-1.58.2.zip")):
        print("  %-52s %s" % (n, a.get("browser_download_url")))
print()
print("Release 页面：https://gitee.com/%s/%s/releases/tag/%s" % (OWNER, REPO, TAG))