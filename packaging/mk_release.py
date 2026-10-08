"""创建 Gitee v0.3.1 Release 并上传三个资产。

用法（令牌从环境变量读，不落盘、不进命令行历史）：
    set GITEE_TOKEN=<你的私人令牌>
    python mk_release.py

令牌在 Gitee → 设置 → 私人令牌 生成，至少要有 projects 权限。
"""
import hashlib
import json
import os
import sys
import urllib.request

OWNER = "pan-wenda"
REPO = "douyin"
TAG = "v0.3.1"
NAME = "v0.3.1：修两处发布件独有的缺陷（素材目录可选化 / 图文标题框）"
BODY = """两处都是**打包成 exe 之后才暴露**的缺陷，源码态跑不出来。

## ① 素材目录从「发布必填闸门」改成**可选白名单**

`DOUYIN_MEDIA_DIR` 之前没配就拒绝发布 —— 结果是"装完 exe 却连一张图都发不出去"，
每台机器都得先猜一个目录名填进去。现在是三态：

| `DOUYIN_MEDIA_DIR` | 行为 |
| --- | --- |
| 没配 | **不限制目录**，只校验「文件存在、是文件」 |
| 配了且目录存在 | **硬边界**，目录外一律拒 |
| 配了但目录不存在 | **明确报错**（不静默当成没配） |

## ② 图文发布的标题输入框选择器写死了视频页的 placeholder

- 视频发布页 = 「填写作品标题」，图文发布页 = 「**添加作品标题**」（实测）
- 只认前者时，发图文会在"等标题框 visible"上**卡满 120 秒超时**才报错 ——
  而那时图片已经传完，用户白等一场
- 改成多候选轮询，`*="作品标题"` 同时覆盖两种
- 顺带修掉一个潜伏 bug：`creator_steps` 漏导入 `CreatorError`
  （`creator_publish._publish` 按 `except CreatorError` 分支处理，走到那里会变 `NameError`）

## 验证

- 单测 168 → **175 条全过**（新增闸门三态 3 条 + 标题框选择器 5 条）
- 真机验证（只填表不点发布）：3 张图传完 → 标题与正文回读均正确
- 打包件验证：`/health` → `version=0.3.1`；未设素材目录时状态页显示
  「未设置 = 不限制目录」，图文预检 `isError=False`

## 资产

| 资产 | 说明 |
| --- | --- |
| `douyin-publish-mcp.exe` | onefile 服务本体，11,446,135 字节 |
| `douyin-publish-mcp-win64-onedir.zip` | onedir 整包 |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（哈希与 v0.3.0 一致，内容未变） |
"""
DIST = r"E:\项目\AI\douyin-publish-mcp\dist"
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
    boundary = "----dyv031boundary"
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
    url = url + ("&" if "?" in url else "?") + "access_token=" + TOKEN
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                 "User-Agent": "curl/8"},
    )
    with opener.open(req, timeout=1800) as r:
        return r.status, r.read().decode("utf-8", "replace")


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
