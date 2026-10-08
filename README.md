# douyin-publish-mcp

把抖音的**发布 + 读取**能力包成薄 MCP 服务，供本平台的「MCP 商店」安装、启停用。
发布/登录/账号检查是**本服务自带**的创作者中心自动化（选择器与流程知识参考了
[social-auto-upload](https://github.com/dreammis/social-auto-upload)（MIT，见 NOTICE 署名）），
运行时**不再调用外部 CLI**；读取则直连抖音 web 接口，见
「[读取能力怎么来](#读取能力怎么来三条通道逐级回退)」。

- **10 个工具**：账号状态、扫码登录（会话式）、重置登录态、发布视频、发布图文；
  搜索视频、作品详情、用户主页、我的主页、分享链接解析。
- **零三方依赖**：只用 Python 标准库（协议层与读取通道都自己写，见下）；
  浏览器驱动按需取、浏览器用系统 Chrome —— exe 因此能保持小。
- **发布是两步**：预检（不碰账号）→ 用户确认 → 执行（见「发布门禁」）；
  读取**不**吃 confirm —— 它只看、不动账号，所以不值得多一次往返。
- **两种形态**：`stdio`（默认，宿主起子进程）与 `streamable_http`（本机常驻 + 状态页）。

它不重写任何平台逻辑，这一层只做四件事 ——
**参数构造、素材白名单、协议适配、确认门禁**；**读取**则由本服务直连抖音 web 接口
（带上用户登录 cookie），把上游几十 KB 的响应裁成几句人真正会看的字段。

---

## 两种形态：选哪个

| | `stdio`（默认） | `streamable_http`（对齐小红书 MCP） |
| --- | --- | --- |
| 启动方式 | 宿主当子进程拉起（`kill_on_drop`） | 商店配置给 `command`，客户端 detached 常驻 |
| 端点 | stdin/stdout，一行一个 JSON-RPC | `POST http://127.0.0.1:18080/mcp` |
| 需要打包 exe | 不需要（`uvx --from <包>` 即可） | **需要**（`command` 要是可执行文件绝对路径） |
| 扫码登录 | 会话式工具：弹**真窗口**扫码，**成功后窗口自动关闭** | 同上，**外加本机状态页** `http://127.0.0.1:18080/`（发起/取消扫码、重置登录态） |
| 宿主重启 | 子进程跟着死 | 服务不重启（登录态、运行记录都在） |
| 停用 | 宿主 kill 子进程 | 客户端按**进程台账 / 端口归属**收进程（`local_service.rs`） |
| 鉴权 | 无需（父子进程） | 可选 `AUTH_TOKEN` → 客户端配 `Authorization: Bearer <token>` |
| 适合 | 只想用工具、不想装东西 | 要登录页、要长任务不被宿主重启打断 |

> 同一份工具实现，两种形态共用；**商店里只发布其中一条**（两条都发会让同一批工具在注册表里重名）。

---

## 为什么值得包一层

直连 `sau` CLI 有三个坑，这一层把它们挡在模型之外：

1. **CLI 的参数细节**（`--images` 是 nargs、`--tags` 要逗号分隔、`--schedule` 是
   `YYYY-MM-DD HH:MM` 本地时间）不该让模型记；记错的表现是"命令跑了一半失败"。
2. **素材路径**由模型给，属于不可信输入。不加闸 = 能把本机任意文件传到抖音。
3. **发布必须有人点过头**。CLI 一旦被调用就是真发布，工具层必须自己带确认门禁
   （见下），不能指望模型自觉。

---

## 前置：装个浏览器就行（**不再需要 social-auto-upload**）

v0.3.0 起登录 / 账号检查 / 发布**全部自带**（就放在本包里，跑在 `--creator-helper` 子进程里），
所以"先用另一个项目把 sau 装起来、再配两个环境变量"这一步没有了。
运行时要的只有：

- **一个 Google Chrome**（或 msedge）。默认 `channel=chrome`；
  用 `DOUYIN_CREATOR_CHANNEL` 可换成 `msedge`。不带 Chromium ——
  patchright 自带那份要另下约 170MB，与"exe 保持小"冲突。
- **浏览器驱动**（patchright）。**不进 exe**，首次用到时按需取（见
  「[浏览器通道的依赖怎么来](#浏览器通道的依赖怎么来exe-保持小--按需取驱动)」）；
  内网机器可用 `DOUYIN_BROWSER_DRIVER_ARCHIVE` 预置本地 zip。
- **登录一次**：调 `douyin_account_login` 扫码（弹真窗口，扫完自动关），
  凭据落在本服务自己的数据目录（`%LOCALAPPDATA%\douyin-publish-mcp\data\`）——
  不再依赖别人的 `cookies/` 目录。

> 旧版本把"发什么"交给机器上的 `sau` 命令，于是可用性等于别人项目的可用性。
> 现在这条路去掉了，代价是本包自己承担自动化流程的维护（选择器改版要跟着改）。

---

## 配置（环境变量）

由商店安装时填的 `envVars` 提供；也可以在 MCP 配置页补填/修改。
★ v0.3.0 起统一 `DOUYIN_` 前缀，**`SAU_*` 全部作废**。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DOUYIN_MEDIA_DIR` | **否** | **可选的**素材根目录白名单。配了 → 素材路径必须落在它里面；不配 → 不限制目录（只校验文件存在） |
| `DOUYIN_ACCOUNT` | 否 | 默认账号名（默认 `main`）；凭据文件名里用它 |
| `DOUYIN_TIMEOUT` | 否 | 单次调用超时秒数，默认 900（上传慢，别调太小） |
| `DOUYIN_PUBLISH_HEADLESS` | 否 | `1`（默认）无头 / `0` 有头 —— **只管发布**；登录**始终有头**（抖音会挑无头，且可能要输短信验证码） |
| `DOUYIN_CREATOR_CHANNEL` | 否 | 创作者中心自动化的浏览器通道，默认 `chrome`（可 `msedge`） |
| `DOUYIN_VERIFY_CODE_FILE` | 否 | 短信二次验证的验证码文件，默认 `<数据目录>/verify_code.txt` |
| `DOUYIN_DATA_DIR` | 否 | 凭据/二维码/验证码文件的数据目录，默认 `%LOCALAPPDATA%\douyin-publish-mcp\data` |
| `DOUYIN_DEBUG` | 否 | `1` = 失败时截图留档 |
| `DOUYIN_COOKIE_FILE` | 否 | 显式指定凭据文件（storage_state 或裸 Cookie 串）。**不填就用数据目录里的 `<账号>` 那份** |
| `DOUYIN_COOKIE` | 否 | 直接给整串 cookie（容器 / 临时调试用，**只能用于读取**：发布需要完整 storage_state 文件） |
| `AUTH_TOKEN` | HTTP 形态可选 | 填了就要求 `Authorization: Bearer <token>`；**绑定非本机地址时必填** |

**装完就能发，不需要先配任何变量。** `DOUYIN_MEDIA_DIR` 是**可选**的收窄开关：
不填时任何可读文件都能发布，填了就变成一个硬边界（目录外的文件一律拒绝）。
想收窄权限（比如只允许发某个素材目录）再填它。

> 早先这个变量是"发布必填"，没配就拒绝发布。但那意味着**装完不配就用不了**，
> 门槛比收益高，已改成可选（v0.3.0）。配错的代价也更小：填了个不存在的路径会明确报错，
> 不会静默当成"没配"。

### 只不装浏览器 / 只想读取行吗？

**行 —— 读取不依赖浏览器自动化。** 实测（Windows，本机，2026-10-08 实测）：

| 能力 | 需要什么 | 说明 |
| --- | --- | --- |
| 服务启动 / `/health` / HTTP 端点 | 只要 Python | 起得来 |
| 读取 5 个工具 | **一份 cookie** | 设 `DOUYIN_COOKIE_FILE`（或 `DOUYIN_COOKIE`）；实测直连 1.2 秒搜到 3 条、0.4 秒拿到"我的资料" |
| 浏览器通道（详情 / 用户作品 / 评论） | 驱动 + Chrome | 驱动按需自取、浏览器用系统 Chrome |
| 扫码登录 / 账号检查 | 自带自动化 + Chrome | 登录要真窗口；登录态检查是**直连探针**，不开浏览器 |
| 发布 5 个工具 | 上面全部 + 素材目录 | 需要完整 storage_state 文件（只给裸 cookie 不够） |

两点代价，先想清楚再走这条路：

1. **cookie 会过期** —— 过期后读取报 `no_credential` / `blocked`，得重新导出一份。
   长期用还是走 `douyin_account_login` 扫码更省事（能续期），
   这条路适合"导一次、只读一阵子"。
2. 凭据**两种格式都认**：浏览器 DevTools 里复制的整串 Cookie，或 patchright / Playwright
   的 `storage_state` JSON。给了文件就优先用它 —— 环境里的残留文件不会盖掉你明确指定的那个。

> 商店条目里 `envVars` 故意留空也是同一个原因：素材目录与 cookie 路径都是**机器相关**的，
> 猜一个写死进去，错了就是"装完直接报找不到文件"，比留空更难排查。

---

## 十个工具（5 个发布/账号 + 5 个读取）

| 工具 | 作用 | 模型该怎么用 |
| --- | --- | --- |
| `douyin_account_status(account)` | 查登录态 | 判据是**直连探针** `/aweme/v1/web/user/profile/self/`（能拿到自己的资料就是有效）；被风控挡住时如实说「判不出来」，**不猜**"没登录" |
| `douyin_account_login(account, headed=true, wait_seconds=90)` | 扫码登录（**会话式**） | 弹**真窗口**（抖音会挑无头浏览器，且可能要输短信验证码），二维码同时以图片返回；**扫完窗口自动关闭**（没人扫也最多 2 分钟）；调用等一段就返回状态，还在等就**再调一次**继续查（不会另开会话）；扫完自动检查一次 |
| `douyin_account_logout(account, confirm=false)` | 重置登录态 | ★ 两步：先回报"将要删除的凭据文件"，用户同意后才 `confirm=true` |
| `douyin_publish_video(account, file, title, description?, tags?, schedule?, thumbnail_portrait?, thumbnail_landscape?, product_link?, product_title?, declaration?, collection?, plan_id?, confirm?)` | 发布视频 | 先不传 confirm 拿计划 → 念给用户 → 带 `plan_id`+`confirm=true` 再调 |
| `douyin_publish_note(account, images[], title, note?, note_file?, bgm?, tags?, schedule?, confirm?)` | 发布图文（≤35 张，不支持 GIF） | 同上；长正文用 `note_file`（指向素材目录内的 .txt/.md） |
| `douyin_search_videos(keyword, count?, sort?, publish_time?, account?)` | 关键词搜视频 | 返回 aweme_id、作者（昵称 + sec_user_id）、互动数据、无水印直链。`sort`/`publish_time` 用**字符串**枚举（`like` / `latest`、`day` / `week` / `half_year`），不是数字 |
| `douyin_video_detail(aweme_id, account?)` | 单条作品详情 | 也能吃整条链接（内部抽 id）；报"没取到"表示 id 不对，或作品已删/私密 |
| `douyin_user_profile(sec_user_id, include_videos?, limit?, account?)` | 用户主页 + 最近作品 | sec_user_id 从搜索/详情的作者字段里拿（`MS4wLjABAAAA…`），**不要自己编** |
| `douyin_my_profile(include_videos?, limit?, account?)` | 当前登录账号自己的主页 | 回答「我这个号发了多少、粉丝多少」用它，不用传 id |
| `douyin_parse_share_link(share_text, account?)` | 分享文本 → 作品详情 | 用户贴「…复制打开抖音… https://v.douyin.com/xxxx/」时用它；链接过期或给的是主页/合集链接，会说明原因而不是静默失败 |

读取工具的 `account` 都可以省略（默认 `DOUYIN_ACCOUNT`，兜底 `main`）。
`download_url` 是**有时效**的直链（通常几小时），要保存就当场下载；本服务只返回地址，**不落盘**。

`schedule` 传本地时间 `'2026-03-24 21:30'` 即走定时发布；不传 = 立即发布。

### 发布参数（这些不是"锦上添花"）

工具参数覆盖创作者中心**真实支持**的开关：

| 场景 | 参数 | 少了的后果 |
| --- | --- | --- |
| 自定义封面 | `thumbnail_portrait`(3:4) / `thumbnail_landscape`(4:3) | 用平台自动截的帧当封面 |
| 带货 | `product_link` + `product_title`（**必须成对**，构造阶段就拦） | 商品没挂上，白发了 |
| 自主声明 | `declaration`（要平台给的原样文案） | 该声明的没声明，合规风险 |
| 合集 | `collection`（合集必须已存在） | 作品没进合集，播放路径少一条 |
| 长正文/背景音乐 | `note_file`→从文件读正文 / `bgm`（搜索词，非路径） | 正文只能塞在参数里，容易截断 |

> 封面与 `note_file` 同样是**把本机文件交给平台**，所以**配了** `DOUYIN_MEDIA_DIR` 时
> 它们也要走同一道白名单（没配则不限制目录）。

### 登录为什么是"会话"而不是一次调用

扫码登录会一直等到用户扫完码（几十秒到几分钟）。若当成同步调用，会把客户端卡住，
而且中断后没有任何地方能回答"刚才那次登录怎么了"。所以：

- 同一时刻**只保留一个待扫码会话**（开新的会关掉旧的，否则每点一次多一个浏览器）；
- 会话状态（等待中/结束/输出/二维码）留在进程里，工具与状态页读的是**同一份**；
- 登录流程结束后**自动**跑一次检查：流程走完 ≠ 扫上了，用户关心的是后者；
- 等待扫码期间**不做**检查（那时已有一个浏览器，再起一个既慢又容易让用户误以为"检查结果是没登录"）。

### 重置登录态（唯一的删除操作）

- 这条链路上"退出登录"就是删掉凭据文件：
  `<数据目录>/douyin_<账号>.json`（`DOUYIN_COOKIE_FILE` 显式指定的那份则删它）。
- 只删这一个文件：不递归、不删目录、不碰其它账号；`confirm=false` 时只看不删。

### 发布门禁（本服务最要紧的一条）

```
第一次：不带 confirm        → 只做本地预检（路径/长度/时间格式），返回计划 + plan_id，**绝不碰账号**
第二次：plan_id + confirm=true → 校验 plan_id 与本次参数算出的指纹一致，才真正执行
```

- `plan_id` 是**计划内容的哈希**（账号+类型+素材+标题+正文+标签+时间）。
- 因此"模型忘了确认直接发"和"用户确认的是 A、发出去的是 B"都发不出去。
- 但这只约束**程序流程**，不是独立的人工审批：真正的把关是工具描述里"必须先给用户看"
  这条要求，以及平台侧的工具审批（若该平台配置了）。

---

## 读取能力怎么来（三条通道，逐级回退）

读取不"一刀切用浏览器"：先**直连**，被风控挡住才回退**浏览器通道**，两者都拿不到才如实报错。

| 通道 | 用什么 | 覆盖的接口 |
| --- | --- | --- |
| ① 直连 | 标准库 `urllib` 打抖音 web 接口 | 搜索、用户主页、我的主页（实测各 5/5，亚秒级） |
| ② 浏览器 | patchright 驱动**系统 Chrome**：先导航到页面，再抓抖音**自己发的**响应 | **作品详情、用户作品**（这两个接口的墙是 uifid 设备指纹：直连 0/5、带签名同样 403）、评论 |
| ③ 都没有 | —— | 如实报 `blocked` 并给"下一步做什么"，不影响其它工具 |

★ 为什么不是"在页面里发 fetch"：实测裸 `fetch` 回放 → `403 Uifid Not Found`；
手工补上抓到的真 uifid（真值 60 字符）→ `403 Signature Not Found`。`uifid` 与 `a_bogus`
是抖音**自己的请求封装**注入的，原生 fetch 绕不过去；而它自己发起的请求
（打开 `/video/<id>`、`/user/<sec_uid>` 时触发的 XHR）全是 200。
所以浏览器通道的形态是**导航 + 抓它自己的响应**（且必须在 response 回调里**立刻**读 body，
晚一步 body 就释放了）。

★ 为什么不做 a_bogus 签名（不引 V8 引擎）：评论接口确实需要签名（实测不签名→空响应、
带签名→200），但**作品详情/用户作品那堵墙签名救不了**（第三方实现带签名同样 403）。
既然浏览器通道本来就要为它俩而建，评论就搭同一条车 —— 省掉一个 V8 引擎和 558KB 混淆 JS 的依赖。

★ 成本：一次浏览器抓取实测 0.4~5 秒（冷启 + 等页面自己发请求，同一页面上的多条需求合并成一次导航）；
直连是亚秒。所以回退只在 `blocked` 时发生，**能直连的接口不会平白多等**。
工具输出里只在真回退过时附一句"通道：…"。

### 浏览器通道的依赖怎么来（exe 保持小 + 按需取驱动）

| 组成 | 从哪来 | 体积 |
| --- | --- | --- |
| 宿主 | **本服务自己**：`douyin-publish-mcp.exe --browser-helper <spec>`（隐藏入口，不在 `--help` 里） | 0 |
| 驱动 | patchright（纯 Python，含 node driver）—— 取法见下表 | ~90MB，**按需** |
| 浏览器 | **系统 Chrome 优先**（实测 153.x 够用）；起不来退自带 chromium；都没有才下载 | 0（默认路线） |

★ 冻结（PyInstaller）后有个坑：`PYTHONPATH` 对 exe **不生效**，所以客户端用
`DOUYIN_BROWSER_DRIVER_PATH` 把驱动目录传给 helper，由 helper 自己插进 `sys.path`
（实测不这么做就是 `No module named 'patchright'`）。同理，打包时要带上整份标准库，
否则运行期挂上来的 patchright 会 `No module named 'uuid'`。

驱动的取用顺序（**只在真的要用、又真的缺**时才动；一个来源都没有就明确报错，不猜、不偷偷拉 PyPI）：

| 来源 | 怎么配 | 说明 |
| --- | --- | --- |
| 本机已有 | `DOUYIN_BROWSER_SITE_PACKAGES=<site-packages>` | 例如本机某个装了 patchright 的 venv 的 `.venv\Lib\site-packages` |
| 运行时目录 | 默认 `%LOCALAPPDATA%\douyin-publish-mcp\browser-runtime\site-packages` | 下面两种方式都落到这里，之后就不再重复取 |
| 本地归档 | `DOUYIN_BROWSER_DRIVER_ARCHIVE=<zip>` | 离线/内网预置：zip 内含 `patchright/` 目录 |
| 发布源 | 默认已内置（Gitee 附件 `douyin-browser-driver-win64-patchright-1.58.2.zip` + 哈希）；`DOUYIN_BROWSER_DRIVER_URL` / `DOUYIN_BROWSER_DRIVER_SHA256` 可覆盖 | **只信哈希**：不匹配就拒装（截断过的来源是真实踩过的坑） |

其它开关：

| 变量 | 作用 |
| --- | --- |
| `DOUYIN_BROWSER=off` | 完全关掉浏览器通道（只留直连） |
| `DOUYIN_BROWSER_CHANNEL` | `auto`（默认：先系统 Chrome）/ `chrome` / `chromium` |
| `DOUYIN_BROWSER_HEADFUL=1` | 有头运行（**调试用**：看页面到底显示了什么） |
| `DOUYIN_BROWSER_DOWNLOAD=0` | 禁止任何自动下载（驱动包 / chromium） |
| `DOUYIN_BROWSER_TIMEOUT_MS` | 等目标接口响应上限（默认 45000） |

**静默与边界**：默认无头、不弹窗口；helper 起-抓-关，不留常驻进程。
但"无感"≠"无痕" —— 调用那几秒任务管理器里会出现 `chrome.exe`（用完即退）；
本服务不做隐藏进程、不做反检测规避。

**内网机器注意**：驱动要预置（本地归档或运行时目录），浏览器要有（系统 Chrome 或自带 chromium 任一）。

### 端到端验收：`scripts/verify_flow.py`

验收分**本地段**与**真机段** —— 两者的成本差一个数量级，能分开跑就不该捆在一起：

| 段 | 环节 | 需要什么 |
| --- | --- | --- |
| 本地 | 单测 → 协议层 → 发布门禁 → HTTP 形态 | 只要 Python。**不联网、不起 helper、不碰账号** |
| 真机 | 环境前置 → 登录态 → 读取五连 → 浏览器通道 → 发布预检 | 登录态 + 外网 + Chrome（驱动按需取） |

```powershell
cd E:\项目\AI\douyin-publish-mcp
$env:PYTHONPATH="src"
& ..\.venv\Scripts\python.exe scripts\verify_flow.py --local-only   # 只有 Python 就够
& ..\.venv\Scripts\python.exe scripts\verify_flow.py                # 本地段 + 真机段
& ..\.venv\Scripts\python.exe scripts\verify_flow.py --keyword 猫 --publish-file a.mp4
```

真机段要的 `DOUYIN_*` 按现场给（例如复用一份已有 cookie）：

```powershell
$env:DOUYIN_COOKIE_FILE="<凭据 json 的绝对路径>"   # 不设就用数据目录里扫码存下的那份
$env:DOUYIN_MEDIA_DIR="<素材目录绝对路径>"          # 可选：配了才限制发布目录
```

脚本走的是和客户端**完全相同**的 `Server.handle` 路径（不是另写一套调用），每步给
`PASS` / `FAIL` / `SKIP`：

- **本地段**：全部单测；协议握手与 10 个工具的 schema；**发布门禁**（预检只出计划，
  `plan_id` 不符与内容改过都必须被拒，并断言全程 **0 个 helper 进程**）；**HTTP 形态**
  （真起服务、真发请求：`/health` 免鉴权、无 token 401、状态页有「读取通道」行、
  无凭据时读取工具给的是可读提示而不是空结果 —— 这个用例会**自己清干净凭据环境**，
  免得被真机段设的 `DOUYIN_COOKIE_FILE` 串味）。
- **真机段**：登录态（直连探针）；**读取五连**（搜索 → 详情 → 用户主页
  → 我的主页 → 分享解析，后三步用前一步真拿到的 `aweme_id` / `sec_user_id` 串起来）；
  **浏览器通道**（单独验详情 + 评论）；发布预检（用你自己的素材）。

- **默认不发布任何东西**：发布只跑到"预检 + 门禁必须拦住"。要真发得显式加 `--confirm-publish`。
- **SKIP 不等于通过**：脚本最后会单独列出"还没验过的"。本地段全绿只说明**服务的壳是对的**，
  不说明抖音认它 —— 判决点是真机段的「搜索」那一步。
- 退出码：`0` = 没有 FAIL（可以带 SKIP）；`1` = 有 FAIL。

> **实测留痕（2026-10-08，本机）**：本地段 4 PASS / 0 FAIL；带 `DOUYIN_COOKIE_FILE` +
  `DOUYIN_MEDIA_DIR` 跑完整两段 = **13 PASS / 0 FAIL / 1 SKIP**（唯一 SKIP 是"真发"，
> 因为没加 `--confirm-publish`）。读取走**直连**通道，浏览器通道 17~19 秒拿到详情 + 评论。

### 没有脚本时的手动版（等价，按顺序来）

1. `douyin_account_status(account="main")` —— 期望「已登录」（读取的前置）
2. `douyin_search_videos(keyword="美食", count=5)` —— 期望有作品；若报 blocked，按上面的排查顺序走
3. `douyin_video_detail(aweme_id="<第 2 步拿到的 id>")` —— 期望有无水印直链
4. `douyin_user_profile(sec_user_id="<作者字段里的 sec_user_id>", limit=5)`
5. `douyin_my_profile(limit=5)`

### 还没做的（第二批）

**互动与通知**：发评论 / 回复 / 点赞 / 收藏、通知未读数与通知列表。
它们的接口路径需要在真机上验过再合并 —— 没验过的路径写进来只会变成"永远空结果"，
而那是这个项目最不能接受的失败形态：看起来能用，实际读不到。

---

## streamable_http 形态

```bash
# 启动（默认只绑 127.0.0.1，port 18080）
douyin-publish-mcp --http --port 18080

# 需要局域网访问时必须给 token（不给会直接拒绝启动）
douyin-publish-mcp --http --host 0.0.0.0 --token <secret>

# 探活（永远免鉴权，供启动器使用）
curl http://127.0.0.1:18080/health

# 走一遍协议
curl -X POST http://127.0.0.1:18080/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

端点一览：

| 路由 | 说明 |
| --- | --- |
| `POST /mcp` | MCP JSON-RPC（tool 调用都走这里） |
| `GET /` | **本机状态页**：配置摘要、发起/取消扫码登录、重置登录态、最近输出 |
| `POST /login` | 发起扫码登录（会话式，页面立刻返回） |
| `POST /check` | 检查登录态（后台跑，结果落在 `/state.json`） |
| `POST /logout` | 重置登录态：`confirm=0` 只看要删什么，`confirm=1` 才删 |
| `POST /cancel-login` | 取消等待扫码的会话 |
| `GET /qr.png` | 当前登录二维码（CLI 落盘了才有；抖音这条链路通常没有，返回 404） |
| `GET /state.json` | 运行状态 JSON（页面轮询用；与工具看到的是同一份会话状态） |
| `GET /health` | 探活 |
| `GET /mcp` | 405（本实现不提供 server→client 的 SSE 流，明说比挂着空流诚实） |

### 打包成独立可执行文件

`command` 必须是可执行文件（不能是 `uvx ...` 这种父进程起子进程的包装：
停用时父进程被杀、真正监听端口的子进程会留下）：

```powershell
# 第一次：装 PyInstaller（内网可加 -PipIndexUrl <内网源>）
.\packaging\build_exe.ps1 -InstallPyInstaller

# onedir（推荐：单进程，停用时按台账就能收干净）
.\packaging\build_exe.ps1

# 或 onefile（单文件更省事，但会多一层自解压子进程 —— 靠客户端的端口归属兜底仍能收掉）
.\packaging\build_exe.ps1 -OneFile
```

产物默认拷到 `%LOCALAPPDATA%\douyin-publish-mcp\`，正好对上商店配置里的 `command`。

> ★ 脚本必须带 **UTF-8 BOM**：Windows 自带的 PowerShell 5.1 对**没有 BOM** 的 `.ps1`
> 按 ANSI(GBK) 读，中文注释会变成乱码并以语法错中断（报错里能看到 `锛堝唴缃?` 这种乱码）。
> 脚本内部调 python / PyInstaller 也统一走 `Invoke-Native` —— PS 5.1 会把原生命令写往
> **stderr** 的日志当成致命错误并中断脚本，而 PyInstaller 顺利运行时也会往 stderr 写 INFO
> （不包的话表现成"打包到一半停了，只看到一行 INFO"，很容易被误读成打包失败）。

打包后自检（本机已跑通）：

```powershell
& "$env:LOCALAPPDATA\douyin-publish-mcp\douyin-publish-mcp.exe" --http --port 18080
# /health → {"status":"ok","service":"douyin-publish-mcp","version":"0.3.1"}
# POST /mcp tools/list → 10 个工具
```

**onedir 还是 onefile**（实测结论，不是推测）：

| | onedir（默认） | onefile（Release 资产用这个） |
| --- | --- | --- |
| 产物 | 一个目录（含 `_internal`，全目录约 22.6 MB） | 单个 exe（约 10.9 MB） |
| 分发 | 整目录拷过去 | 只拷一个文件 |
| 启动 | 快（约 1s） | 首次多几百 ms（自解压） |
| 停用能否收干净 | 单进程，按台账直接收 | ★ **会留下一层同名子进程**（自解压的 bootloader），父进程被杀后它可能仍在监听端口 —— 靠客户端的**端口归属兜底**（映像名 == 配置的 exe 名）收掉 |

所以：**发布到 Release 用 onefile**（对齐小红书的"裸二进制"资产形态、用户只下一个文件）；
**本地/内网部署用 onedir** 更省心。两者工具实现完全相同。

---

## 从 Release 安装（使用者）

发布源（Gitee 镜像，本公司账号）：**https://gitee.com/pan-wenda/douyin**（当前 v0.3.1）

| 资产 | 用途 |
| --- | --- |
| `douyin-publish-mcp.exe` | 服务本体：PyInstaller **onefile** 单文件，约 10.9 MB（11,446,135 字节），**自带 Python 运行时，不用先装 Python** |
| `douyin-publish-mcp-win64-onedir.zip` | 整包形态（解压即用；onedir 少一层自解压子进程，停用时更好收） |
| `douyin-browser-driver-win64-patchright-1.58.2.zip` | 浏览器驱动（**不用手动下**：真缺时服务自己按需取，见下面「浏览器通道」一节） |

```powershell
$dst = "$env:LOCALAPPDATA\douyin-publish-mcp"
New-Item -ItemType Directory -Force $dst | Out-Null
Move-Item .\douyin-publish-mcp.exe "$dst\douyin-publish-mcp.exe" -Force
& "$dst\douyin-publish-mcp.exe" --http --port 18080     # 本机常驻，只绑 127.0.0.1
```

目录和文件名不是随便定的：商店配置里的 `command` 就是
`%LOCALAPPDATA%\douyin-publish-mcp\douyin-publish-mcp.exe` —— 形态与商店里的小红书 MCP 一致
（`%LOCALAPPDATA%\<服务名>\<服务名>.exe`，客户端 `expand_env` 会展开这个占位）。

> ★ **但 exe 不是全部**：它自带 Python 运行时、自带发布/登录自动化，却**不自带浏览器驱动**
> （patchright 那 ~100MB）与浏览器 —— 驱动首次用到时按需取，浏览器用系统 Chrome。
> **装完即可用**：不需要先配任何变量（`DOUYIN_MEDIA_DIR` 是可选的收窄开关），见上面的前置条件。

---

## 本地调试

```bash
# 单元测试（零依赖，含 HTTP 形态的端到端：真起服务再打它）
cd douyin-publish-mcp
set PYTHONPATH=src
..\.venv\Scripts\python.exe -m unittest discover -s tests

# stdio 形态手动走一遍
echo {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}} | python -m douyin_publish_mcp

# http 形态冒烟
python -m douyin_publish_mcp --http --port 18080
```

## 接进本平台（Dr.Q 客户端）

### 1. 先决定「本包从哪来」

- **http 形态**：`command` = 上一步打出来的 exe 绝对路径（如
  `%LOCALAPPDATA%\douyin-publish-mcp\douyin-publish-mcp.exe`），`args` = `--http --port 18080`。
  exe 需要分发到每台机器（拷目录 / 走你们的内部分发）。
- **stdio 形态**：`command` = `uvx`，`args` = `--from <内网包来源> douyin-publish-mcp`。
  包来源三种写法：

| 写法 | `args` | 适用 |
| --- | --- | --- |
| 内网 GitLab 归档（与商店里 `qgis` 一条同法） | `--from http://<gitlab>/<group>/douyin-publish-mcp/-/archive/main/douyin-publish-mcp-main.zip douyin-publish-mcp` | 内网可达 GitLab，不需要 git CLI、不需要 PyPI |
| 内网 PyPI | `douyin-publish-mcp`（envVars 里给 `UV_DEFAULT_INDEX=http://<内网源>/simple`） | 已把包发到内网源 |
| 本地目录 | `--from D:\path\to\douyin-publish-mcp douyin-publish-mcp` | 单机调试/离线演示 |

> ★ 三个坑（前两个踩过）：
> ① `args` 在商店表单里是**空格分隔**字符串（后端按空白切分），路径不能有空格；
> ② `command` / `args` / `cwd` **会**展开 `%LOCALAPPDATA%` 这类占位（`local_service::expand_env`，
>    未定义的变量保持原样，便于排查"变量名写错"）——所以 `command` 写
>    `%LOCALAPPDATA%\douyin-publish-mcp\douyin-publish-mcp.exe` 就能适配每台机器，
>    不必写死用户名；
> ③ 但 `envVars` 里的值**不会**展开（原样透传给子进程，由子进程自己解释），必须写绝对路径。

### 2. 发布到商店

**前置（http 形态）**：先 `.\packaging\build_exe.ps1` 打出 exe —— 商店里的 `command`
就是这个绝对路径，exe 不在，别人装完也起不来（`%LOCALAPPDATA%\douyin-publish-mcp\`
只在**打包的那台机器**上有，要分发到每台机器）。

客户端「MCP 商店」页 → 发布服务，字段照抄：

| 表单字段 | http 形态（[`store_config.json`](store_config.json)） | stdio 形态（[`store_config.stdio.json`](store_config.stdio.json)） |
| --- | --- | --- |
| 名称 | 抖音发布与读取 | 同（·stdio） |
| 传输方式 | `streamable_http` | `stdio` |
| command | `%LOCALAPPDATA%\douyin-publish-mcp\douyin-publish-mcp.exe` | `uvx` |
| args | `--http --port 18080` | `--from <内网包来源> douyin-publish-mcp` |
| url | `http://127.0.0.1:18080/mcp` | （空） |
| envVars | `DOUYIN_MEDIA_DIR` / `DOUYIN_ACCOUNT` / `DOUYIN_TIMEOUT` / `AUTH_TOKEN` | 同左，**无** AUTH_TOKEN |
| status / visibility | `2` / `public` 才会出现在商店里给所有人装 | 同左 |

- 走 `POST /api/mcp-service-config` 落库 `tb_mcp_service_config`。
- ★ **只发一条**：两个形态共用同一批工具名，同时发布会让注册表里重名。
- 发布前先跑一遍 `scripts/verify_flow.py --local-only`：确认 `tools/list` 里正好 10 个工具、
  描述完整、发布门禁拦得住 —— 商店里装的版本和这份配置是同一个东西，别把没验过的发出去。
- 读取工具用到的 `DOUYIN_COOKIE_FILE` / `DOUYIN_COOKIE`（见环境变量表）**不放进**商店默认：
  正常路径下凭据来自本服务数据目录（扫码登录时写下的那份），这两个只在
  "读取和发布不是一个账号/换机器复用凭据"时才需要，属高级用法。

### 3. 安装后的行为差异（这段决定运维怎么做）

- **http 形态**：客户端安装时先确保本机进程起来（`local_service::ensure`），再连
  `url`；进程是 detached 的，**宿主重启不影响它**。停用时客户端按
  **进程台账 + 端口归属（映像名必须等于配置的 exe 名）** 收掉进程，所以
  `command` 里的 exe 名要和真正监听端口的进程名一致（这也是不用 `uvx` 包装的原因）。
- **stdio 形态**：宿主 kill 子进程即可，没有端口、没有遗留；代价是宿主重启后要重新拉起。

### 4. 换机器/换目录

改 MCP 配置页里的 env（`DOUYIN_MEDIA_DIR` / `DOUYIN_ACCOUNT`）再重启该服务即可，不用重装。
凭据默认落在该机器的 `%LOCALAPPDATA%\douyin-publish-mcp\data\`，**换机器要重新扫码**；
想复用旧机器的登录态，把那份 json 拷过去并用 `DOUYIN_COOKIE_FILE` 指过去。

---

## 风险与边界（请连同代码一起看）

- 底层是**浏览器自动化 + 用户自己的登录态**：违反抖音平台协议，随时可能被改版/风控打断。
  真要长期稳定，走开放平台 `video.create`（合规但需资质与用户授权）。
- 本服务**不存储**账号密码，但登录态（cookie / storage_state）会落在本机数据目录里 ——
  那是**凭据**，不要提交进任何仓库。★ 读取通道**必须读到 cookie 的值**（否则发不出请求），
  但对外只回显**来源与 cookie 名**：工具输出、状态页、日志里都不会出现值。
  「重置登录态」是唯一的删除动作，且只删 `<数据目录>/douyin_<账号>.json` 一个文件。
- 读取工具会把**公开内容与你自己的账号资料**带进对话上下文（含无水印直链，几小时后失效）。
  只用它读公开内容与自己有权看的账号；把 `limit` 调大、连续多次搜索会显著提高被风控的概率。
- http 形态默认只绑 `127.0.0.1`；改绑别处必须配 token —— 这个服务能直接发布内容。
- 每次发布都会真实落到用户账号上；"提交成功"不等于"已公开可见"（平台还有审核）。
  ★ 点过「发布」但没等到成功页 → `state=uncertain`，这时**绝不自动重试**（重试就可能发两条），
  必须让人去创作者中心「作品管理」核对。
- 短视频平台的标题/正文/图片数量限制会变，本服务里的长度预检（标题 30 字、正文 1000 字）
  只用于**尽早报错**，不代表平台真实限额。

---

## 许可

[Apache License 2.0](LICENSE) —— 与商店里的小红书 MCP（[`xpzouying/xiaohongshu-mcp`](https://github.com/xpzouying/xiaohongshu-mcp)）
采用同一许可，便于两边一起用、一起改。
