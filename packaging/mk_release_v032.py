"""创建 Gitee v0.3.2 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python mk_release_v032.py

令牌在 Gitee → 设置 → 私人令牌 生成，至少要有 projects 权限。
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.2"
NAME = "v0.3.2：修「话题丢失」—— 话题必须在下拉里点选"
BODY = """用户报「发布图文时正文和话题丢失」。抓抖音**提交载荷**定位：**正文没丢**，
丢的是话题 —— 三个话题的 `hashtag_id` 全是 `0`，抖音只当它是**普通文本**，
作品页不会变成可点击的话题标签。

## 根因：填法错了，不是没填进去

旧实现打完 `#词` 直接按空格，而**话题建议下拉在按空格那一瞬间就收起**，
抖音没机会把它认成真话题：

| 时刻 | `[class*="mention-suggest"]` |
| --- | --- |
| 打完 `#音乐节` | **可见**，候选 10 条 |
| 按下 Space | **0 个**（已收起） |

现在改成「打完 → 在下拉还开着时点选候选」（新增 `creator_steps._pick_mention`）。

## 三个连带踩出来的坑（都是真机实测）

1. **不能点列表容器** —— `mention-suggest-item-container-*` 是整个 517×300 的
   候选列表，点它的中心会点中**列表中间**那条（实测「#音乐节」被点成「#音乐节穿搭」）。
   要精确到候选行里的 `span.tag-hash-view-name-*`。
2. **不能用 `native_click`** —— 它会先 `mouse.click` 再补一整套原生事件序列；
   第一次点击插入话题后**下拉立即重排/收起**，补发的第二套事件按旧坐标命中了
   **另一个**候选，于是凭空多出一个用户没要求的话题（实测「#音乐节」旁边
   冒出「我的长长长假」）。候选行是普通 DOM，普通 `click()` 就够。
3. **不能取候选第一条** —— 抖音会把**正文里已出现的实体词**也塞进候选列表，
   `.first` 经常不是你要的那个。改用 `:text-is()` 按文本精确等于话题词定位。

## 验证

- **真机拦截提交载荷**（图文 / 视频两条分支各跑一次，绝不真发）：
  3 个话题 `hashtag_id` 均为真值（`1564647725385730` / `1587591634181133` /
  `1736389391437836`），正文完整，**零多余话题**；修复前是 `0/0/0`。
- 单测 175 → **186 条全过**，含一条源码级断言：视频与图文**必须共用同一个**
  填表入口，防止将来某条分支另写一套导致修复只对一半生效。

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（哈希与 v0.3.1 一致，内容未变） |
"""

DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
DRIVER_SRC = (r"E:\项目\AI\douyin-publish-mcp\build\v021"
              r"\douyin-browser-driver-win64-patchright-1.58.2.zip")
#: 🚨 驱动 zip 也必须传到 v0.3.2 —— `DRIVER_URL_DEFAULT` 已随版本号改成
#:   `/download/v0.3.2/`，不传的话新机"按需取驱动"会 404（DRIVER_SHA 不变，内容同一份）。
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
    """multipart/form-data 手工拼（标准库没有方便的 multipart 封装）。"""
    boundary = "----dyv032boundary"
    fn = os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    body = b"".join([
        ("--%s\r\n" % boundary).encode(),
        ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % fn).encode(),
        b"Content-Type: application/octet-stream\r\r\n",
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


# ── 0. 前置自检：驱动 zip 的哈希必须与代码里登记的一致（否则新机取到就装不上）──
if not os.path.isfile(DRIVER_SRC):
    sys.exit("找不到驱动 zip：%s" % DRIVER_SRC)
got = hashlib.sha256(open(DRIVER_SRC, "rb").read()).hexdigest()
if got != DRIVER_SHA:
    sys.exit("驱动 zip 哈希与 DRIVER_SHA_DEFAULT 不一致：\n  实际 %s\n  登记 %s"
             % (got, DRIVER_SHA))
print("驱动 zip 哈希校验通过（内容与旧版一致）")

for name in ASSETS:
    p = os.path.join(DIST, name)
    if not os.path.isfile(p):
        sys.exit("dist里缺资产：%s" % p)
print("三个资产齐备")

# ── 1. 建 Release ────────────────────────────────────────
rel = post("%s/repos/%s/%s/releases?access_token=%s" % (API, OWNER, REPO, TOKEN), {
    "tag_name": TAG,
    "name": NAME,
    "body": BODY,
    "prerelease": False,
    "target_commitish": "main",
})
rid = rel["id"]
print("Release 已创建: id=%s tag=%s" % (rid, rel.get("tag_name")))

# ── 2. 传三个资产 ────────────────────────────────────────
for name in ASSETS:
    path = os.path.join(DIST, name)
    size = os.path.getsize(path)
    status, text = upload(
        "%s/repos/%s/%s/releases/%s/attach_files" % (API, OWNER, REPO, rid), path)
    print("  上传 %-52s %12d 字节  HTTP %s" % (name, size, status))

print()
print("完成。Release 页面：https://gitee.com/%s/%s/releases/tag/%s" % (OWNER, REPO, TAG))
