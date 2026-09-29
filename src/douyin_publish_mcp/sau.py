"""social-auto-upload 的 `sau` CLI 封装。

三条纪律，缺一条都会出问题：

1. **命令构造与执行分开**。构造是纯函数（`*_args`），可离线单测；执行（`run`）才
   碰进程与磁盘。发布路径上的参数错误应该在构造阶段就被拦下，而不是等 CLI 报一半。
2. **素材路径必须落在白名单目录内**（[`resolve_media_path`]）。路径由模型给，
   属于不可信输入 —— 不加闸就等于"把本机任意文件传到抖音"。
3. **这里不判断"该不该发布"**。那件事只有一个门禁：`server.py` 的 `confirm`
   参数（见 `tool_publish_video` 的文档）。本模块只负责"确认之后怎么发"。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# ── 本地预检用的长度上限 ────────────────────────────────────────
# ★ 这两个数字是**抖音创作者中心**的口径（标题 30 字、正文 1000 字），
#   与 liaogx/douyin-mcp 的本地限制一致。它们只是"尽早报错"用的预检值，
#   不是本项目的承诺 —— 真实限额以当前创作者中心为准（改了这里不会更准），
#   所以超限时给的是**可执行的修改建议**，而不是一句"参数错误"。
TITLE_MAX_CHARS = 30
DESC_MAX_CHARS = 1000

# 定时发布的时间格式：CLI 文档里是 "2026-03-24 21:30"（本地时间）。
# 也接受 ISO 的 T 分隔写法，统一成空格分隔再交给 CLI（少一种输入分歧）。
SCHEDULE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?$")

# 二维码候选文件名（登录时 CLI 落盘的临时图片；见 CLI.md「登录二维码说明」）
QRCODE_HINTS = ("qrcode", "qr_code", "二维码", "erweima")


class SauError(ValueError):
    """参数/环境错误（对模型可读，调用方应把它当成"让用户去改配置"而不是重试）"""


@dataclass
class SauConfig:
    """从环境变量读来的运行前提（由商店安装时填的 envVars 提供）。

    三种调用方式，优先级从高到低：

    1. `SAU_CMD`：直接给定 `sau` 可执行文件（最稳，推荐）——
       如 `C:\\Users\\me\\.local\\bin\\sau.exe`；
    2. `SAU_DIR`：social-auto-upload 的项目根目录 → 用 `uv run --directory <dir> sau ...`
       （不需要 PATH 里有 sau，但首次会同步依赖，慢）；
    3. 都不给 → 直接用 PATH 里的 `sau`。
    """

    cmd: str = ""
    project_dir: str = ""
    uv: str = "uv"
    # ★ 允许发布的素材根目录。默认取项目目录（`SAU_DIR`），两者都缺时**拒绝发布**
    #   （宁可报"没配置"也不能退化成"整个磁盘可读"）。
    media_dir: str = ""
    timeout: int = 900
    headless: bool = True
    no_sync: bool = False
    # 抖音短信二次验证：CLI 优先读项目根目录的 verify_code.txt（见 CLI.md）
    verify_code_file: str = ""

    @staticmethod
    def from_env(env: Optional[dict] = None) -> "SauConfig":
        e = os.environ if env is None else env

        def s(key: str, default: str = "") -> str:
            return str(e.get(key, default) or "").strip()

        def b(key: str, default: bool) -> bool:
            raw = s(key)
            if not raw:
                return default
            return raw.lower() not in ("0", "false", "no", "off")

        project_dir = s("SAU_DIR")
        media_dir = s("SAU_MEDIA_DIR") or project_dir
        verify = s("SAU_VERIFY_CODE_FILE") or (
            str(Path(project_dir) / "verify_code.txt") if project_dir else ""
        )
        try:
            timeout = int(s("SAU_TIMEOUT") or 900)
        except ValueError:
            timeout = 900
        return SauConfig(
            cmd=s("SAU_CMD"),
            project_dir=project_dir,
            uv=s("SAU_UV") or "uv",
            media_dir=media_dir,
            timeout=max(30, timeout),
            headless=b("SAU_HEADLESS", True),
            no_sync=b("SAU_NO_SYNC", False),
            verify_code_file=verify,
        )

    # ── 命令拼装（纯函数，单测直接打这两条） ──────────────────

    def base_argv(self) -> List[str]:
        """`sau` 之前的那一段（可执行文件 + 运行方式）"""
        if self.cmd:
            return [self.cmd]
        if self.project_dir:
            argv = [self.uv, "run", "--directory", self.project_dir]
            if self.no_sync:
                # 不加 = 每次调用都同步依赖（慢但不会"环境没装好"）；加了更快，
                # 代价是 venv 必须先 `uv sync` 过 —— 由安装者自己保证。
                argv.append("--no-sync")
            argv.append("sau")
            return argv
        # 兜底：PATH 里的 sau。装不上时 setup 阶段就失败（见 doctor 提示）
        return ["sau"]

    def command(self, args: Sequence[str]) -> List[str]:
        return self.base_argv() + list(args)


@dataclass
class SauResult:
    """一次 CLI 调用的结果（不抛异常：CLI 的失败也是**结果**，要如实回给模型）"""

    argv: List[str]
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    hint: str = ""

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0

    def tail(self, limit: int = 4000) -> str:
        """给模型看的输出尾部（CLI 的结论通常在最后几行）"""
        text = (self.stdout or "").strip()
        err = (self.stderr or "").strip()
        if err and err not in text:
            text = f"{text}\n{err}".strip()
        return text[-limit:]


# ── 参数构造 ────────────────────────────────────────────────


def check_args(account: str) -> List[str]:
    return ["douyin", "check", "--account", _clean_account(account)]


def login_args(account: str, headed: bool) -> List[str]:
    args = ["douyin", "login", "--account", _clean_account(account)]
    args.append("--headed" if headed else "--headless")
    return args


def upload_video_args(
    account: str,
    file: str,
    title: str,
    description: str = "",
    tags: Optional[Iterable[str]] = None,
    schedule: str = "",
    headless: bool = True,
    thumbnail: str = "",
    thumbnail_landscape: str = "",
    thumbnail_portrait: str = "",
    product_link: str = "",
    product_title: str = "",
    declaration: str = "",
    collection: str = "",
) -> List[str]:
    """视频发布参数。

    ★ 这里覆盖的是 CLI **真实支持**的那批开关（见 sau_cli.py 的
      `douyin upload-video` 定义）：封面三种尺寸、带货商品、自主声明、合集。
      少一个的代价不是"发不出去"，而是"发出去的作品缺了该有的东西"——
      那种失败用户往往过几天才发现。
    """
    args = [
        "douyin",
        "upload-video",
        "--account",
        _clean_account(account),
        "--file",
        file,
        "--title",
        _clean_title(title),
    ]
    if description:
        args += ["--desc", _clean_text(description, "正文", DESC_MAX_CHARS)]
    tag_str = _join_tags(tags)
    if tag_str:
        args += ["--tags", tag_str]
    if schedule:
        args += ["--schedule", normalize_schedule(schedule)]
    if thumbnail:
        args += ["--thumbnail", thumbnail]
    if thumbnail_landscape:
        args += ["--thumbnail-landscape", thumbnail_landscape]
    if thumbnail_portrait:
        args += ["--thumbnail-portrait", thumbnail_portrait]
    # ★ 菜单元组：只有链接没有标题（或反之）会被平台当成"没填完整"，
    #   在构造阶段就拦住，比让浏览器脚本跑到一半失败省钱得多。
    if bool(product_link) != bool(product_title):
        raise SauError("带货商品要链接和标题**一起**给（product_link + product_title），只给一个平台不认。")
    if product_link:
        args += ["--product-link", product_link, "--product-title", _one_line(product_title)]
    if declaration:
        args += ["--declaration", _one_line(declaration)]
    if collection:
        args += ["--collection", _one_line(collection)]
    args.append("--headless" if headless else "--headed")
    return args


def upload_note_args(
    account: str,
    images: Sequence[str],
    title: str,
    note: str = "",
    tags: Optional[Iterable[str]] = None,
    schedule: str = "",
    headless: bool = True,
    note_file: str = "",
    bgm: str = "",
) -> List[str]:
    if not images:
        raise SauError("图文发布至少要一张图片（images 不能为空）")
    if note and note_file:
        raise SauError(
            "正文只能给一种：note（直接给文本）或 note_file（从文件读，对应 CLI 的 --notef）。"
            "两个都给会让用户不知道实际发出去的是哪一份。"
        )
    args = [
        "douyin",
        "upload-note",
        "--account",
        _clean_account(account),
        "--images",
        *images,
        "--title",
        _clean_title(title),
    ]
    if note:
        args += ["--note", _clean_text(note, "图文正文", DESC_MAX_CHARS)]
    if note_file:
        args += ["--notef", note_file]
    tag_str = _join_tags(tags)
    if tag_str:
        args += ["--tags", tag_str]
    if schedule:
        args += ["--schedule", normalize_schedule(schedule)]
    if bgm:
        # BGM 是**搜索词**（CLI 会在音乐库里搜第一个匹配），不是文件路径，所以不过白名单
        args += ["--bgm", _one_line(bgm)]
    args.append("--headless" if headless else "--headed")
    return args


def _clean_account(account: str) -> str:
    value = (account or "").strip()
    if not value:
        raise SauError("缺少账号名（account）。账号名是 social-auto-upload 里登录时用的名字，不是抖音昵称。")
    if any(c in value for c in " \t\n\r\"'&|;<>"):
        # 命令行参数是**数组**传递（不经 shell），但仍拒绝可疑字符：
        # 这些字符几乎一定是模型把账号名和别的东西拼在一起了。
        raise SauError(f"账号名含非法字符：{value!r}。只允许字母、数字、下划线和连字符。")
    return value


def _clean_title(title: str) -> str:
    value = _one_line(title)
    if not value:
        raise SauError("标题不能为空（抖音作品必须有标题）")
    if len(value) > TITLE_MAX_CHARS:
        raise SauError(
            f"标题 {len(value)} 字，超过抖音的 {TITLE_MAX_CHARS} 字上限：{value[:20]}…\n"
            f"请改写为 {TITLE_MAX_CHARS} 字以内（把要点前置，不要只是截断）。"
        )
    return value


def _clean_text(text: str, label: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) > limit:
        raise SauError(
            f"{label} {len(value)} 字，超过本地预检上限 {limit} 字。\n"
            f"（这只是预检，真实限额以创作者中心为准；但这么长基本会被平台拒。）"
        )
    return value


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _join_tags(tags: Optional[Iterable[str]]) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):  # 容错：模型有时传 "a,b" 而不是 ["a","b"]
        parts = re.split(r"[,，\s]+", tags)
    else:
        parts = [str(t) for t in tags]
    cleaned = [re.sub(r"[,\s]+", "", t) for t in parts]
    return ",".join([t for t in cleaned if t])


def normalize_schedule(schedule: str) -> str:
    """校验并规范化定时发布时间（CLI 要的是本地时间的 'YYYY-MM-DD HH:MM'）"""
    value = (schedule or "").strip()
    m = SCHEDULE_RE.match(value)
    if not m:
        raise SauError(
            f"定时发布时间格式不对：{value!r}。应为 'YYYY-MM-DD HH:MM'（本地时间），"
            f"例如 '2026-03-24 21:30'。想立即发布就不要传 schedule。"
        )
    y, mo, d, h, mi = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    return f"{y}-{mo}-{d} {h}:{mi}"


# ── 素材白名单 ──────────────────────────────────────────────


def resolve_media_path(cfg: SauConfig, raw: str) -> Path:
    """把模型给的素材路径解析成绝对路径，并确保它在白名单目录内。

    ★ 这是本服务**唯一**的路径闸门：发布工具只能用落在 `SAU_MEDIA_DIR` 里的文件。
      理由和「客户端读本地图片要过目录白名单」一样（见 dsl/screenshot.rs）：
      路径来自模型，不校验就等于开放了任意文件上传。
    """
    if not (raw or "").strip():
        raise SauError("素材路径不能为空")
    if not cfg.media_dir:
        raise SauError(
            "未配置素材目录（SAU_MEDIA_DIR / SAU_DIR）。请到 MCP 配置页填上 "
            "social-auto-upload 的项目目录，再重试 —— 否则本服务无法确认"
            "你要发的文件是不是允许的那批。"
        )
    root = Path(cfg.media_dir).expanduser().resolve()
    if not root.exists():
        raise SauError(f"素材目录不存在：{root}（请核对 SAU_MEDIA_DIR / SAU_DIR）")

    raw_path = Path(raw.strip())
    # 相对路径按素材目录解析 —— 模型给的通常是相对路径
    candidate = raw_path if raw_path.is_absolute() else (root / raw_path)
    resolved = candidate.expanduser().resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise SauError(
            f"素材必须是素材目录内的文件：{raw!r} 不在 {root} 之内。\n"
            f"请把要发布的内容放进该目录（或把路径写成目录内的相对路径）。"
        ) from None

    if not resolved.is_file():
        raise SauError(f"素材文件不存在：{resolved}")
    if resolved.stat().st_size <= 0:
        raise SauError(f"素材文件是空的：{resolved}")
    return resolved


# ── 执行 ────────────────────────────────────────────────────


def _child_env() -> dict:
    """交给 sau 子进程的环境变量。

    ★ 编码：Windows 上 CLI 的中文输出默认走 GBK，解码错就会变成一片问号
      （用户/模型都读不出错在哪）。这里显式要求子进程用 UTF-8，
      并按 UTF-8 解码、出错也不中断（`errors="replace"`）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def spawn(cfg: SauConfig, args: Sequence[str]) -> subprocess.Popen:
    """起一个**要等很久**的 sau 进程（目前只有登录用得上），返回 Popen。

    为什么不能用 [`run`]：`sau douyin login` 会一直等到用户扫完码
    （几十秒到几分钟），阻塞式 run 会让调用方连"现在还在等"都没法回报。
    调用方负责读走 stdout/stderr —— **不读会填满管道把子进程卡死**，
    见 [`login_session.LoginSession`] 里的两个读取线程。
    """
    return subprocess.Popen(
        cfg.command(args),
        cwd=cfg.project_dir or None,
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run(cfg: SauConfig, args: Sequence[str], timeout: Optional[int] = None) -> SauResult:
    """执行一次 `sau` 调用（同步阻塞；失败也返回结果，不抛异常）。

    ★ 编码：Windows 上 CLI 的中文输出默认走 GBK，解码错就会变成一片问号
      （用户/模型都读不出错在哪）。这里显式要求子进程用 UTF-8，
      并按 UTF-8 解码、出错也不中断（`errors="replace"`）。
    """
    argv = cfg.command(args)
    env = _child_env()

    cwd = cfg.project_dir or None
    limit = int(timeout or cfg.timeout)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=limit,
            check=False,
        )
    except FileNotFoundError as e:
        return SauResult(
            argv=argv,
            exit_code=None,
            stdout="",
            stderr=str(e),
            hint=_not_found_hint(cfg, argv[0]),
        )
    except subprocess.TimeoutExpired as e:
        return SauResult(
            argv=argv,
            exit_code=None,
            stdout=_decode(e.stdout),
            stderr=_decode(e.stderr),
            timed_out=True,
            hint=(
                f"等待超过 {limit} 秒仍未结束，已中止本次调用。"
                "发布可能仍在浏览器里进行，请让用户先到创作者中心核对"
                "「作品管理」里有没有这条 —— 不要直接重试发布，会出重复作品。"
            ),
        )

    return SauResult(
        argv=argv,
        exit_code=proc.returncode,
        stdout=_decode(proc.stdout),
        stderr=_decode(proc.stderr),
    )


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def _not_found_hint(cfg: SauConfig, exe: str) -> str:
    if cfg.cmd:
        return (
            f"找不到可执行文件 {exe!r}。请核对 MCP 配置里的 SAU_CMD（要填绝对路径，"
            f"如 %USERPROFILE%\\.local\\bin\\sau.exe），或改用 SAU_DIR 指向项目目录。"
        )
    if cfg.project_dir:
        if not shutil.which(cfg.uv):
            return (
                f"找不到 {cfg.uv!r}。social-auto-upload 的 CLI 要用 uv 运行："
                f"请把 MCP 配置里的 SAU_UV 指向 uv.exe 的完整路径。"
            )
        return f"经 uv 启动 sau 失败（找不到 {exe!r}）：请确认 {cfg.project_dir} 是项目根目录、且已 `uv sync`。"
    return (
        f"找不到 {exe!r}。请二选一：把 SAU_CMD 指向 sau.exe 的绝对路径，"
        f"或把 SAU_DIR 指向 social-auto-upload 的项目目录（本服务会用 uv 调起它）。"
    )


# ── 账号凭据（登录态）──────────────────────────────────────
#
# sau 的登录态就是**一个 json 文件**：<项目>/cookies/douyin_<account>.json
# （见 sau_cli.py 的 resolve_account_file + douyin_setup(storage_state=...)）。
# 没有独立的「退出登录」子命令（douyin 的动作只有 login/check/upload-video/upload-note），
# 所以"重置登录态"在这条链路上就只有一件事可做：删掉那个文件。

ACCOUNT_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _account_slug(account: str) -> str:
    """账号名的**文件名安全**形式（比 `_clean_account` 更严：它还要当文件名用）"""
    value = _clean_account(account)
    if not ACCOUNT_SLUG_RE.match(value):
        raise SauError(
            f"账号名 {value!r} 不能用作凭据文件名：只允许字母、数字、下划线、连字符（1~64 位）。"
        )
    return value


def account_file_path(cfg: SauConfig, account: str) -> Path:
    """该账号的凭据文件绝对路径（不存在也返回路径，由调用方判断）。"""
    if not cfg.project_dir:
        raise SauError(
            "未配置 SAU_DIR（social-auto-upload 项目根目录），无法定位账号凭据文件。"
            "请到 MCP 配置页填上项目目录。"
        )
    return (Path(cfg.project_dir).expanduser().resolve() / "cookies" / f"douyin_{_account_slug(account)}.json")


@dataclass
class LogoutPlan:
    """「重置登录态」的结果：**删了什么 / 为什么没删** 都写在里面。"""

    account: str
    path: Path
    existed: bool
    deleted: bool = False

    def describe(self) -> str:
        if not self.existed:
            return (
                f"账号 {self.account} 没有凭据文件（{self.path}）——本来就是未登录状态，无需重置。"
            )
        if self.deleted:
            return (
                f"已删除账号 {self.account} 的凭据文件：{self.path}\n"
                f"该账号现在是未登录状态，下次发布前要重新扫码登录。"
            )
        return f"确认后才会删除这个文件：{self.path}（账号 {self.account} 的登录态）"


def logout(cfg: SauConfig, account: str, confirm: bool = False) -> LogoutPlan:
    """重置登录态 = 删除该账号的凭据文件。

    ★ 三条硬约束（这是本服务唯一的**删除**操作，写死在这里）：
      1. 只删 `<项目>/cookies/douyin_<账号>.json` **这一个文件** —— 不删目录、不递归、
         不碰同目录下其它账号的凭据；
      2. 路径再一次做「父目录必须是 cookies/、文件名必须严格匹配」的校验，防止
         账号名花样绕过（`_account_slug` 已经挡了一层）；
      3. `confirm=False` 时**只看不删**，把要删的路径原样回报，由用户点头。
    """
    slug = _account_slug(account)
    path = account_file_path(cfg, slug)
    cookies_dir = (Path(cfg.project_dir).expanduser().resolve() / "cookies")
    if path.parent.resolve() != cookies_dir.resolve() or path.name != f"douyin_{slug}.json":
        raise SauError("内部校验未通过：拒绝删除非凭据文件（只允许删 <项目>/cookies/douyin_<账号>.json）。")
    if not path.is_file():
        return LogoutPlan(account=slug, path=path, existed=False)
    if not confirm:
        return LogoutPlan(account=slug, path=path, existed=True)
    path.unlink()
    return LogoutPlan(account=slug, path=path, existed=True, deleted=True)


# ── 输出解读（尽力而为，猜不出来就如实说猜不出来）─────────────────


def parse_login_state(result: SauResult) -> Optional[bool]:
    """判登录态：**退出码优先**，文本兜底。

    ★ 为什么退出码优先：`sau douyin check` 的实现是
      `print("valid" if is_valid else "invalid")` + `return 0 if is_valid else 1`
      （见 sau_cli.py 的 dispatch）。退出码是**结构化事实**，
      比在一堆日志里找"未登录"两个字可靠得多 —— 后者在 CLI 改文案时会静默失效。
      但旧版可能"退出码 0 却在输出里说 invalid"，所以文本说 invalid 时仍以文本为准。
    """
    if result.timed_out:
        return None
    if result.exit_code == 0:
        return False if parse_logged_in(result.tail()) is False else True
    if result.exit_code == 1:
        return False
    return parse_logged_in(result.tail())


def parse_logged_in(text: str) -> Optional[bool]:
    """从 `sau douyin check` 的输出里粗判登录态。

    ★ 猜不出来时返回 `None`（而不是 `False`）：把"没看懂"说成"没登录"，
      会让用户白白去重扫一次码。
    """
    t = (text or "").lower()
    if not t.strip():
        return None
    negative = ("not logged", "未登录", "login failed", "logged_in=false", "logged_in: false", "'logged_in': false")
    positive = ("logged in", "已登录", "login success", "登录成功", "logged_in=true", "logged_in: true", "'logged_in': true")
    if any(k in t for k in negative):
        return False
    if any(k in t for k in positive):
        return True
    return None


def parse_qrcode_path(text: str, extra_roots: Sequence[str] = ()) -> Optional[Path]:
    """从 CLI 输出里找二维码图片路径（找到且存在就是它）。"""
    for m in re.finditer(r"([A-Za-z]:\\[^\n\"']+?\.(?:png|jpg|jpeg)|/[^\s\"']+?\.(?:png|jpg|jpeg))", text or ""):
        p = Path(m.group(1).strip())
        if p.exists() and any(h in p.name.lower() for h in QRCODE_HINTS):
            return p
    for root in extra_roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.png")) + sorted(base.glob("*.jpg")):
            if any(h in p.name.lower() for h in QRCODE_HINTS):
                return p
    return None


def redact(text: str) -> str:
    """抹掉输出里可能出现的凭据（附带的防御：正常路径下 CLI 不打印 cookie）。"""
    out = text or ""
    out = re.sub(
        r"(sessionid|passport_csrf_token|ttwid|session_key|sid_tt|api_?key|token)\s*[=:]\s*[^\s;\"']+",
        r"\1=***",
        out,
        flags=re.IGNORECASE,
    )
    return out


__all__ = [
    "SauConfig",
    "SauResult",
    "SauError",
    "LogoutPlan",
    "check_args",
    "login_args",
    "upload_video_args",
    "upload_note_args",
    "normalize_schedule",
    "resolve_media_path",
    "account_file_path",
    "logout",
    "run",
    "spawn",
    "parse_login_state",
    "parse_logged_in",
    "parse_qrcode_path",
    "redact",
    "TITLE_MAX_CHARS",
    "DESC_MAX_CHARS",
]
