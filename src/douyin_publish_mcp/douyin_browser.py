"""浏览器通道（客户端侧）：直连被 uifid 墙挡住时，用真浏览器把数据取回来。

## 它解决什么

★ 2026-09-29 实测（同一份 cookie、同一台机器、连续 5 轮）：

| 接口 | 直连 | 浏览器 |
|---|---|---|
| 搜索 / 用户主页 / 我的主页 | ✅ 5/5 | ✅ |
| 作品详情 `/aweme/v1/web/aweme/detail/` | ❌ 0/5（`403 Uifid Not Found`） | ✅ 200（标题、赞、评论数、无水印直链） |
| 用户作品 `/aweme/v1/web/aweme/post/` | ❌ 0/5 | ✅ 200（18 条 + has_more） |

那两个接口的墙是 **uifid（设备指纹）**，不是签名：带 a_bogus 也照样 403
（第三方实现 `hhy5562877/douyin_mcp` 同机同 cookie 也一样）。而浏览器里
**uifid 和 a_bogus 都由页面自己生成**，所以只要页面能加载就过得去。

## 三条设计约束（"exe 保持小 + 按需取驱动"）

1. **宿主永远是我们自己**：冻结后是 `douyin-publish-mcp.exe --browser-helper <spec>`，
   开发期是 `python browser_helper.py <spec>`。exe 里不放驱动，也不放浏览器。
2. **驱动按需取**：`patchright`（纯 Python，含 node driver）只在**真的要用且真的缺**时，
   才从我们自己的发布源/本地归档解到运行时目录；也能指到本机已有的那份
   （`DOUYIN_BROWSER_SITE_PACKAGES`）。
3. **浏览器先用系统 Chrome**：实测系统 Chrome（153.x）就够，**不下载 ~170MB 的 chromium**；
   起不来（机器上没 Chrome）才退回自带的那份，自带那份也没有才去下载。

## 静默与降级

- 默认 `headless=true`：不弹窗口；helper 用完即 `browser.close()`，不留常驻进程。
- 本通道**只在直连被判 `blocked` 时**才被调用；它自己出任何问题都不影响直连
  （搜索/主页照旧），也绝不让整个服务变差。
- `DOUYIN_BROWSER=off` 可完全关掉这条通道。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .browser_helper import default_driver_dir
from .douyin_cred import Credential, parse_cookie_header, parse_storage_state
from .douyin_web import (
    DouyinWebClient,
    DouyinWebError,
    pick_user,
    user_page_from_post,
    video_from_aweme,
)
from .config import RuntimeConfig

# ── 落点与来源 ───────────────────────────────────────────────

#: 驱动/浏览器的运行时目录（与 exe 同级的用户目录，不写进仓库、不随版本分发）
#: ★ 定义只有一份（在 browser_helper 里）：helper 被直接拉起时也要能找到同一个地方
DRIVER_DIR = default_driver_dir()
RUNTIME_DIR = DRIVER_DIR.parent

#: 驱动包来源（我们自己的发布源）
DRIVER_URL_ENV = "DOUYIN_BROWSER_DRIVER_URL"
DRIVER_SHA_ENV = "DOUYIN_BROWSER_DRIVER_SHA256"
DRIVER_ARCHIVE_ENV = "DOUYIN_BROWSER_DRIVER_ARCHIVE"   # 离线预置：本地 zip/whl
DRIVER_SITE_ENV = "DOUYIN_BROWSER_SITE_PACKAGES"       # 本机已有的驱动目录
PYTHON_ENV = "DOUYIN_BROWSER_PYTHON"                   # 显式指定宿主解释器

#: 内置的发布源（Gitee 镜像的 Release 附件），环境变量可覆盖。
#: ★ 为什么要内置：新机器上「缺驱动」不该再要求用户先读文档、配两个变量 ——
#:   缺了就去取，这才是"exe 保持小 + 按需取驱动"这句话的兑现方式。
#: ★ 只信哈希：Gitee 附件实测出现过"截断后仍是合法 PE 头"的同款问题，没有哈希就不装。
#: ★ 改这里 = 换发布件，必须同步：客户端 exe_fetch.rs 的登记值、NOTICE、README 资产表。
DRIVER_URL_DEFAULT = (
    "https://gitee.com/pan-wenda/douyin/releases/download/v0.3.2/"
    "douyin-browser-driver-win64-patchright-1.58.2.zip"
)
DRIVER_SHA_DEFAULT = "8a6de597f21574094d93ee86a1e965330fae32b29aaec458385abd5ae880b03d"

_HELPER_NAME = "browser_helper.py"


def _truthy(value: str, default: bool = False) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return default
    return text not in ("0", "off", "false", "no", "disable", "disabled")


@dataclass
class BrowserConfig:
    """浏览器通道的运行参数（环境变量驱动，默认值即可用）"""

    enabled: bool = True
    headless: bool = True
    #: auto=先系统 Chrome、失败退自带 chromium；chrome=只用系统 Chrome；chromium=只用自带那份
    channel: str = "auto"
    #: 等目标接口响应的上限（秒）—— 页面自己发 XHR 的耗时全在这里
    timeout_ms: int = 45000
    navigate_timeout_ms: int = 60000
    #: 宿主子进程总上限（含导航、含首次取驱动）
    host_timeout_s: int = 300
    #: 允许自动下载（驱动包 / chromium）；DOUYIN_BROWSER_DOWNLOAD=0 可禁
    allow_download: bool = True

    @classmethod
    def from_env(cls) -> "BrowserConfig":
        channel = (os.environ.get("DOUYIN_BROWSER_CHANNEL") or "auto").strip().lower()
        if channel not in ("auto", "chrome", "chromium"):
            channel = "auto"
        return cls(
            enabled=_truthy(os.environ.get("DOUYIN_BROWSER", ""), True),
            headless=not _truthy(os.environ.get("DOUYIN_BROWSER_HEADFUL", ""), False),
            channel=channel,
            timeout_ms=int(os.environ.get("DOUYIN_BROWSER_TIMEOUT_MS") or 45000),
            navigate_timeout_ms=int(os.environ.get("DOUYIN_BROWSER_NAV_TIMEOUT_MS") or 60000),
            host_timeout_s=int(os.environ.get("DOUYIN_BROWSER_HOST_TIMEOUT_S") or 300),
            allow_download=_truthy(os.environ.get("DOUYIN_BROWSER_DOWNLOAD", ""), True),
        )


@dataclass
class Host:
    """谁来跑 helper、驱动从哪个目录来"""

    argv: List[str]
    sys_path: List[str] = field(default_factory=list)
    note: str = ""


# ── 宿主与驱动解析 ───────────────────────────────────────────


def _helper_source() -> Optional[Path]:
    path = Path(__file__).with_name(_HELPER_NAME)
    return path if path.is_file() else None


def driver_dirs(runtime: Optional[RuntimeConfig] = None) -> List[Path]:
    """驱动目录候选（按优先级）：显式指定 > 运行时目录（按需下载的落点）

    ★ v0.3.0 去掉了"顺带复用 social-auto-upload 虚拟环境里的 patchright"那条 ——
      不再依赖那个项目，也就没有"别人的 venv"可蹭了。缺驱动就按需取（见 ensure_driver）。
    """
    dirs: List[Path] = []
    explicit = (os.environ.get(DRIVER_SITE_ENV) or "").strip()
    if explicit:
        dirs.append(Path(explicit))
    dirs.append(DRIVER_DIR)
    return dirs


def resolve_host(cfg: BrowserConfig, runtime: Optional[RuntimeConfig] = None,
                 subcommand: str = "--browser-helper") -> Host:
    """决定"谁跑 helper"以及"驱动从哪来"。

    - 显式给了 `DOUYIN_BROWSER_PYTHON`：用它跑 `browser_helper.py`（开发/特殊环境用）
    - 否则：**一律用我们自己当宿主**（冻结后是 exe 的隐藏入口），驱动目录通过 PYTHONPATH
      交给子进程 —— 这样 exe 不用长大。

    `subcommand` 决定子进程干哪件事：读取用 `--browser-helper`，
    登录/发布用 `--creator-helper`（同一份驱动、同一套宿主解析，只有入口不同）。
    """
    sys_path = [str(p) for p in driver_dirs(runtime) if p.is_dir()]
    explicit_py = (os.environ.get(PYTHON_ENV) or "").strip()
    helper = _helper_source()

    if explicit_py and subcommand == "--browser-helper":
        if helper is None:  # 冻结后没有 .py 源文件，只能靠 exe 自己当宿主
            raise _unavailable(
                "设了 DOUYIN_BROWSER_PYTHON，但当前是打包后的 exe，读不到 browser_helper.py 源文件。",
                "去掉 DOUYIN_BROWSER_PYTHON（让 exe 自己当宿主），或用 DOUYIN_BROWSER_SITE_PACKAGES 指定驱动目录。",
            )
        return Host([explicit_py, str(helper)], sys_path, "DOUYIN_BROWSER_PYTHON=%s" % explicit_py)

    if getattr(sys, "frozen", False):
        return Host([sys.executable, subcommand], sys_path, "exe 自宿主")

    if helper is None:
        raise _unavailable("找不到 browser_helper.py（包不完整）。", "重新安装本服务。")
    if subcommand != "--browser-helper":
        # 开发态：创作者助手是包内入口（`python -m douyin_publish_mcp --creator-helper`）
        return Host([sys.executable, "-m", "douyin_publish_mcp", subcommand], sys_path,
                    "开发解释器 %s" % Path(sys.executable).name)
    return Host([sys.executable, str(helper)], sys_path, "开发解释器 %s" % Path(sys.executable).name)


def _unavailable(message: str, hint: str = "") -> DouyinWebError:
    """浏览器通道不可用 —— 归到 upstream（不是"没登录"，别把用户引去扫码）。"""
    return DouyinWebError("浏览器通道不可用：" + message, kind="upstream", hint=hint)


def driver_ready(cfg: BrowserConfig, runtime: Optional[RuntimeConfig] = None) -> bool:
    """驱动是否已就位（只看本地，不触发下载）—— 给状态页/自检用。"""
    return any(p.is_dir() for p in driver_dirs(runtime))


def describe(cfg: Optional[BrowserConfig] = None, runtime: Optional[RuntimeConfig] = None) -> str:
    """一行话说明"这条通道现在能不能用、驱动从哪来"（**不泄漏任何凭据**）"""
    cfg = cfg or BrowserConfig.from_env()
    if not cfg.enabled:
        return "浏览器通道：已关闭（DOUYIN_BROWSER=off）"
    dirs = [p for p in driver_dirs(runtime) if p.is_dir()]
    driver = str(dirs[0]) if dirs else "未就位（首次用到时按需取）"
    return "浏览器通道：启用（先用系统 Chrome 起不来则退自带 chromium）｜驱动：%s" % driver


# ── 按需取驱动 ───────────────────────────────────────────────


def ensure_driver(cfg: BrowserConfig, runtime: Optional[RuntimeConfig] = None) -> Optional[Path]:
    """按需把驱动准备好，返回新就位的目录（没得可装就返回 None）。

    顺序：① 本地归档（离线预置）② 发布源 URL + sha256（**内置默认**，环境变量可覆盖）
    ③ 放弃（上层给指引，告诉用户去哪儿放）。

    ★ 只信哈希：来源可能被截断（本项目在 exe 分发上就撞到过"截断后仍是合法 PE 头"），
      没有哈希就不装，宁可报错让人去配 DOUYIN_BROWSER_SITE_PACKAGES。
    ★ `DOUYIN_BROWSER_DOWNLOAD=0` 一票否决：内网机器宁可报错，也不许偷偷往外发请求。
    """
    archive_src = (os.environ.get(DRIVER_ARCHIVE_ENV) or "").strip()
    url = (os.environ.get(DRIVER_URL_ENV) or DRIVER_URL_DEFAULT).strip()
    sha = (os.environ.get(DRIVER_SHA_ENV) or DRIVER_SHA_DEFAULT).strip()
    if not archive_src and not (url and sha):
        return None
    if not cfg.allow_download and not archive_src:
        return None

    DRIVER_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="douyin-driver-") as tmp:
        tmp_path = Path(tmp) / "driver.zip"
        if archive_src:
            try:
                shutil.copyfile(archive_src, tmp_path)
            except OSError as exc:
                raise _unavailable(
                    "本地驱动包读不到：%s" % str(exc)[:160],
                    "%s 指的路径不存在？离线预置请给绝对路径。" % DRIVER_ARCHIVE_ENV,
                ) from None
        else:
            # ★ 下载失败与解包失败分开报：原先两者共用一句"解包失败"，
            #   真机上看到「解包失败: getaddrinfo failed」会一头雾水（那其实是 DNS 挂了）。
            #   报错里带上**实际用的 URL**，才能一眼看出是不是内置默认源指错了版本。
            try:
                _download(url, tmp_path)
            except Exception as exc:  # noqa: BLE001 —— 网络原因五花八门，都要变成人话
                raise _unavailable(
                    "驱动包下载失败（%s）：%s" % (type(exc).__name__, str(exc)[:160]),
                    "来源：%s\n换来源：%s；离线：先把包下好，再用 %s 指向它。"
                    % (url, DRIVER_URL_ENV, DRIVER_ARCHIVE_ENV),
                ) from None
            if _sha256(tmp_path) != sha.lower():
                raise _unavailable(
                    "驱动包哈希不匹配（来源 %s）" % url,
                    "别用这个来源：字节被换过或被截断。请核对 %s 与 %s。" % (DRIVER_URL_ENV, DRIVER_SHA_ENV),
                )
        DRIVER_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(tmp_path) as zf:
                zf.extractall(DRIVER_DIR)
        except Exception as exc:  # noqa: BLE001 —— 装不上是环境问题，不该中断调用方
            raise _unavailable(
                "驱动包解包失败：%s" % str(exc)[:200],
                "把驱动包换成 zip（内含 patchright/ 目录）后重试。",
            ) from None
    return DRIVER_DIR if DRIVER_DIR.is_dir() else None


def _download(url: str, dst: Path) -> None:
    with urllib.request.urlopen(url, timeout=180) as resp, open(dst, "wb") as fh:
        shutil.copyfileobj(resp, fh)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_browser(cfg: BrowserConfig, host: Host) -> bool:
    """系统 Chrome 起不来时，用驱动的 CLI 下载它自带的那份 chromium（等价 `patchright install chromium`）。

    ★ 这是**兜底**路线，不是默认：默认用系统 Chrome，省掉 ~170MB 的下载。
      能走到这里说明机器上没有 Chrome（或明确指定了 channel=chromium）。
    """
    if not cfg.allow_download:
        return False
    argv = list(host.argv)
    # 把 "python helper.py" 换成 "python -m patchright install chromium"（exe 宿主同理）
    if "--browser-helper" in argv:
        argv = argv[: argv.index("--browser-helper")] + ["-m", "patchright", "install", "chromium"]
    else:
        argv = argv[:1] + ["-m", "patchright", "install", "chromium"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(host),
            timeout=cfg.host_timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return True


# ── 调用 helper ──────────────────────────────────────────────


# ── 通用 helper 通道（读取用 --browser-helper，登录/发布用 --creator-helper）───


@dataclass
class HelperRun:
    """一次 helper 进程：拿得到 pid、能停、spec 文件要记得删。"""

    proc: subprocess.Popen
    spec_path: Path
    host: Host

    def terminate(self, grace: float = 5.0) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:  # noqa: BLE001 —— 收尾失败不该往上抛
            pass

    def cleanup(self) -> None:
        try:
            self.spec_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "HelperRun":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.terminate()
        self.cleanup()


def write_spec(spec: Dict[str, Any]) -> Path:
    handle, raw = tempfile.mkstemp(prefix="douyin-helper-", suffix=".json")
    os.close(handle)
    path = Path(raw)
    # ★ ensure_ascii=False + utf-8：中文文件名/标题要原样传过去（读的时候也是 utf-8）
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def spawn_helper(host: Host, spec: Dict[str, Any]) -> HelperRun:
    """起一个**长跑**的 helper（登录要等扫码、发布要传大文件，都不能用 run 等它）。"""
    spec_path = write_spec(spec)
    try:
        proc = subprocess.Popen(
            host.argv + [str(spec_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(host),
        )
    except OSError:
        try:
            spec_path.unlink()
        except OSError:
            pass
        raise
    return HelperRun(proc=proc, spec_path=spec_path, host=host)


def run_helper(host: Host, spec: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """起一个 helper 并等它那行 JSON（用于"跑完才回来"的动作，如发布）。

    返回 helper 的 payload；进程挂了/超时/没吐 JSON 都变成同形状的失败 payload，
    调用方不用分别处理"异常"和"错误 JSON"两种情况。
    """
    spec_path = write_spec(spec)
    try:
        proc = subprocess.run(
            host.argv + [str(spec_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(host),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": {
                "kind": "timeout",
                "message": "自动化进程超过 %.0f 秒没回来（发布一般 1~3 分钟，超时说明卡住了）" % timeout,
                "hint": "到创作者中心「作品管理」确认这次有没有发出去，再决定是否重试。",
            },
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": {"kind": "host_failed", "message": "起不了自动化进程：%s" % exc, "hint": ""},
        }
    finally:
        try:
            spec_path.unlink()
        except OSError:
            pass

    payload = _last_json_line(proc.stdout or "")
    if payload is None:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {
            "ok": False,
            "error": {
                "kind": "no_result",
                "message": "自动化进程没吐出结果（退出码 %s）" % proc.returncode,
                "hint": ("最后几行输出：" + " / ".join(tail)) if tail else "开 DOUYIN_DEBUG=1 重跑看细节。",
            },
        }
    return payload


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    """取最后一行 JSON（helper 的进度都走 stderr，stdout 只有标准输出那一行）。"""
    for line in reversed((text or "").splitlines()):
        candidate = line.strip()
        if candidate.startswith("{"):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _child_env(host: Host) -> Dict[str, str]:
    env = dict(os.environ)
    if host.sys_path:
        joined = os.pathsep.join(p for p in host.sys_path if p)
        # ★ 两个都设：helper 自己插 sys.path（冻结后 PYTHONPATH 不生效，必须显式传）；
        #   PYTHONPATH 留给"非冻结的解释器 + 子进程自己再拉子进程"的场景做兜底。
        env["DOUYIN_BROWSER_DRIVER_PATH"] = joined
        existing = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = joined + (os.pathsep + existing if existing else "")
    # ★ 让 helper 的 stdout 只有一行 JSON：驱动自身的日志/警告都赶到 stderr
    env.setdefault("PATCHRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _invoke(cfg: BrowserConfig, host: Host, spec: Dict[str, Any]) -> Dict[str, Any]:
    """跑一次 helper，拿回它那行 JSON。任何"没结果"的情况都变成可读的 DouyinWebError。"""
    spec_file = Path(tempfile.mkstemp(prefix="douyin-browser-", suffix=".json")[1])
    try:
        spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        try:
            proc = subprocess.run(
                host.argv + [str(spec_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(host),
                timeout=cfg.host_timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise _unavailable(
                "浏览器通道超时（%d 秒）" % cfg.host_timeout_s,
                "冷启浏览器 + 等页面自己发请求一般 10~20 秒；持续超时说明页面被风控卡住，"
                "可设 DOUYIN_BROWSER_HEADFUL=1 看看页面到底显示了什么。",
            ) from None
        except OSError as exc:
            raise _unavailable("起不了宿主进程：%s" % exc, "检查 %s 是否可执行。" % host.argv[0]) from None

        line = ""
        for candidate in (proc.stdout or "").splitlines():
            if candidate.strip().startswith("{"):
                line = candidate.strip()
        if not line:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise _unavailable(
                "宿主没吐出结果（退出码 %s）" % proc.returncode,
                "宿主 stderr 末尾：%s" % " / ".join(tail) if tail else "把 DOUYIN_BROWSER_HEADFUL=1 打开复现一次。",
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            raise _unavailable("宿主输出不是 JSON：%s" % line[:200], "可能是驱动版本不兼容。") from None
    finally:
        try:
            spec_file.unlink()
        except OSError:
            pass


def _error_of(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    err = payload.get("error") or {}
    return str(err.get("kind") or "unknown"), str(err.get("message") or ""), str(err.get("hint") or "")


def read(cfg: BrowserConfig, cred: Credential, wants: List[Dict[str, Any]],
         runtime: Optional[RuntimeConfig] = None) -> Dict[str, Any]:
    """跑一次浏览器通道，返回 helper 的 payload（含 results/misses）。

    按需取驱动与下载 chromium 的重试都收敛在这里：**只在缺东西时多跑一次**，
    已经有驱动/浏览器的情况下永远只跑一次。
    """
    if not cfg.enabled:
        raise _unavailable("已被 DOUYIN_BROWSER=off 关闭", "去掉这个环境变量即可启用。")
    host = resolve_host(cfg, runtime)
    spec = {
        "storage_state": _storage_state_for(cred),
        "headless": cfg.headless,
        "channel": cfg.channel,
        "timeout_ms": cfg.timeout_ms,
        "navigate_timeout_ms": cfg.navigate_timeout_ms,
        "wants": wants,
    }
    payload = _invoke(cfg, host, spec)

    kind, _, _ = _error_of(payload)
    if not payload.get("ok") and kind == "no_driver" and cfg.allow_download:
        new_dir = ensure_driver(cfg, runtime)
        if new_dir is not None:
            payload = _invoke(cfg, replace(host, sys_path=host.sys_path + [str(new_dir)]), spec)
            kind, _, _ = _error_of(payload)

    if not payload.get("ok") and kind == "no_browser" and cfg.allow_download:
        if install_browser(cfg, host):
            payload = _invoke(cfg, host, spec)
    return payload


def _storage_state_for(cred: Credential) -> Optional[str]:
    """给浏览器一份登录态：能直接指向 storage_state 文件就指，否则按 cookie 拼一个临时文件。

    ★ 比"让用户再登一次"省事得多，也避免出现"发布说已登录、读取说没登录"的自相矛盾。
      临时文件用完即删（其中含凭据值）。
    """
    if cred.path is not None and cred.path.is_file():
        try:
            if parse_storage_state(cred.path.read_text(encoding="utf-8", errors="replace")):
                return str(cred.path)
        except OSError:
            pass
    pairs = parse_cookie_header(cred.cookie)
    if not pairs:
        return None
    cookies = [
        {"name": name, "value": value, "domain": ".douyin.com", "path": "/", "expires": -1,
         "httpOnly": False, "secure": True, "sameSite": "Lax"}
        for name, value in pairs
    ]
    fd, path = tempfile.mkstemp(prefix="douyin-state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"cookies": cookies, "origins": []}, fh)
    return path


def _body(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    """从 payload 里取某条 want 的响应体；取不到就按 helper 的错误讲清楚。"""
    results = payload.get("results") or {}
    item = results.get(key)
    if isinstance(item, dict) and isinstance(item.get("body"), dict):
        return item["body"]
    kind, message, hint = _error_of(payload)
    misses = payload.get("misses") or {}
    detail = message or misses.get(key) or "没拿到 %s 的响应" % key
    mapped = "blocked" if kind == "no_response" or misses.get(key) else "upstream"
    raise DouyinWebError(
        "浏览器通道也没取到数据：%s" % detail,
        kind=mapped,
        hint=hint or "先确认登录态（douyin_account_status），必要时重新扫码。",
    )


def _note(payload: Dict[str, Any]) -> str:
    browser = payload.get("browser") or "浏览器"
    ms = payload.get("elapsed_ms")
    return "浏览器通道（%s%s）" % (browser, "，%dms" % ms if ms else "")


# ── 门面：直连优先，被墙才回退 ───────────────────────────────
# ★ 只在这里做"回退"这件事：能直连的接口继续走直连（亚秒、没有浏览器开销），
#   只有 kind=blocked（403/空响应/被判 blocked）才动用浏览器。


@dataclass
class Read:
    """一次读取的结果 + 它是从哪条通道来的（要如实告诉模型/用户）"""

    data: Any
    source: str

    def __iter__(self):  # 方便 old-style 解包
        return iter((self.data, self.source))


def _fallback_enabled(cfg: BrowserConfig, error: DouyinWebError) -> bool:
    return cfg.enabled and error.kind == "blocked"


def video_detail(cfg: BrowserConfig, client: DouyinWebClient, aweme_id: str,
                 cred: Credential, runtime: Optional[RuntimeConfig] = None) -> Read:
    try:
        return Read(client.video_detail(aweme_id), "直连")
    except DouyinWebError as exc:
        if not _fallback_enabled(cfg, exc):
            raise
    payload = read(cfg, cred, [{"kind": "video_detail", "aweme_id": str(aweme_id)}], runtime)
    aweme = _body(payload, "video_detail:%s" % aweme_id).get("aweme_detail")
    video = video_from_aweme(aweme) if aweme else {}
    if not video:
        raise DouyinWebError(
            "浏览器通道拿到的详情里没有可用视频信息",
            kind="parse",
            hint="接口结构可能已变，请把这条反馈上来。",
        )
    return Read(video, _note(payload))


def user_videos(cfg: BrowserConfig, client: DouyinWebClient, sec_user_id: str,
                cred: Credential, count: int = 20, runtime: Optional[RuntimeConfig] = None) -> Read:
    try:
        return Read(client.user_videos(sec_user_id, count=count), "直连")
    except DouyinWebError as exc:
        if not _fallback_enabled(cfg, exc):
            raise
    want = {"kind": "user_post", "sec_user_id": str(sec_user_id), "count": count}
    payload = read(cfg, cred, [want], runtime)
    return Read(user_page_from_post(_body(payload, "user_post:%s" % sec_user_id)), _note(payload))


def user_profile(cfg: BrowserConfig, client: DouyinWebClient, sec_user_id: str,
                 cred: Credential, runtime: Optional[RuntimeConfig] = None) -> Read:
    try:
        return Read(client.user_profile(sec_user_id), "直连")
    except DouyinWebError as exc:
        if not _fallback_enabled(cfg, exc):
            raise
    want = {"kind": "user_profile", "sec_user_id": str(sec_user_id)}
    payload = read(cfg, cred, [want], runtime)
    user = _body(payload, "user_profile").get("user")
    picked = pick_user(user) if user else {}
    if not picked:
        raise DouyinWebError("浏览器通道拿到的用户资料是空的", kind="parse", hint="接口结构可能已变。")
    return Read(picked, _note(payload))


def my_profile(cfg: BrowserConfig, client: DouyinWebClient, cred: Credential,
               runtime: Optional[RuntimeConfig] = None) -> Read:
    try:
        return Read(client.my_profile(), "直连")
    except DouyinWebError as exc:
        if not _fallback_enabled(cfg, exc):
            raise
    payload = read(cfg, cred, [{"kind": "my_profile"}], runtime)
    user = _body(payload, "my_profile").get("user")
    picked = pick_user(user) if user else {}
    if not picked:
        raise DouyinWebError("浏览器通道拿到的本人资料是空的", kind="parse", hint="接口结构可能已变。")
    return Read(picked, _note(payload))


@dataclass
class UserPage:
    """一个用户主页上的两件事（资料 + 作品）+ 各自的来源。

    ★ 为什么要合起来：它俩在同一个页面、同一次导航里就会被抖音自己请求出来，
      分成两次调用等于起两次浏览器、多等一轮 —— 一次抓全才是"无感"的前提。
    """

    user: Dict[str, Any] = field(default_factory=dict)
    videos: List[Dict[str, Any]] = field(default_factory=list)
    has_more: bool = False
    user_source: str = ""
    videos_source: str = ""


def user_page(cfg: BrowserConfig, client: DouyinWebClient, sec_user_id: str, cred: Credential,
              count: int = 20, include_profile: bool = True, include_videos: bool = True,
              runtime: Optional[RuntimeConfig] = None) -> UserPage:
    """别人的主页：资料与作品各自"直连优先"，被墙的部分合并成**一次**浏览器抓取。"""
    page = UserPage()
    wants: List[Dict[str, Any]] = []

    if include_profile:
        try:
            page.user = client.user_profile(sec_user_id)
            page.user_source = "直连"
        except DouyinWebError as exc:
            if not _fallback_enabled(cfg, exc):
                raise
            wants.append({"kind": "user_profile", "sec_user_id": str(sec_user_id)})

    if include_videos:
        try:
            data = client.user_videos(sec_user_id, count=count)
            page.videos = data.get("videos") or []
            page.has_more = bool(data.get("has_more"))
            page.videos_source = "直连"
        except DouyinWebError as exc:
            if not _fallback_enabled(cfg, exc):
                raise
            wants.append({"kind": "user_post", "sec_user_id": str(sec_user_id), "count": count})

    if wants:
        payload = read(cfg, cred, wants, runtime)
        note = _note(payload)
        if include_profile and not page.user_source:
            user = _body(payload, "user_profile").get("user")
            page.user = pick_user(user) if user else {}
            page.user_source = note
        if include_videos and not page.videos_source:
            data = user_page_from_post(_body(payload, "user_post:%s" % sec_user_id))
            page.videos = data["videos"]
            page.has_more = data["has_more"]
            page.videos_source = note
    return page


def self_page(cfg: BrowserConfig, client: DouyinWebClient, cred: Credential, count: int = 20,
              include_videos: bool = True, runtime: Optional[RuntimeConfig] = None) -> UserPage:
    """我的主页：资料 + 我的作品。

    ★ 自己的作品走的是同一个 `/aweme/post/` 接口，而 `/user/self` 页会自己带 sec_user_id 打它 ——
      所以浏览器那一次不用先知道 sec_user_id（want 里不带就是"页面打哪个算哪个"）。
    """
    page = UserPage()
    try:
        page.user = client.my_profile()
        page.user_source = "直连"
    except DouyinWebError as exc:
        if not _fallback_enabled(cfg, exc):
            raise
        page.user_source = ""

    if include_videos:
        sec_uid = str(page.user.get("sec_user_id") or "")
        if sec_uid:
            try:
                data = client.user_videos(sec_uid, count=count)
                page.videos = data.get("videos") or []
                page.has_more = bool(data.get("has_more"))
                page.videos_source = "直连"
            except DouyinWebError as exc:
                if not _fallback_enabled(cfg, exc):
                    raise

    if not page.user_source or (include_videos and not page.videos_source):
        wants: List[Dict[str, Any]] = []
        if not page.user_source:
            wants.append({"kind": "my_profile"})
        if include_videos and not page.videos_source:
            wants.append({"kind": "user_post"})
        payload = read(cfg, cred, wants, runtime)
        note = _note(payload)
        if not page.user_source:
            user = _body(payload, "my_profile").get("user")
            page.user = pick_user(user) if user else {}
            page.user_source = note
        if include_videos and not page.videos_source:
            data = user_page_from_post(_body(payload, "user_post:self"))
            page.videos = data["videos"]
            page.has_more = data["has_more"]
            page.videos_source = note
    return page


def comments(cfg: BrowserConfig, cred: Credential, aweme_id: str, count: int = 20,
             runtime: Optional[RuntimeConfig] = None) -> Read:
    """评论：**没有直连通道**，所以这条只走浏览器。

    ★ 顺带说明为什么不做 a_bogus 签名：评论接口确实需要签名（实测不签名→空响应，
      带签名→200），但作品详情/用户作品那堵 uifid 墙签名救不了 —— 既然浏览器通道
      本来就要为它俩建，评论就搭同一条车，省掉一个 V8 引擎和 558KB 混淆 JS 的依赖。
    """
    want = {"kind": "comments", "aweme_id": str(aweme_id), "count": count}
    payload = read(cfg, cred, [want], runtime)
    body = _body(payload, "comments:%s" % aweme_id)
    items = [c for c in (body.get("comments") or []) if isinstance(c, dict)]
    picked = [
        {
            "text": str(c.get("text") or ""),
            "nickname": str((c.get("user") or {}).get("nickname") or ""),
            "digg_count": int(c.get("digg_count") or 0),
            "create_time": int(c.get("create_time") or 0),
            "reply_count": int(c.get("reply_comment_total") or 0),
        }
        for c in items
    ]
    return Read(
        {"comments": picked, "has_more": bool(body.get("has_more")), "total": int(body.get("total") or 0)},
        _note(payload),
    )


__all__ = [
    "BrowserConfig",
    "DRIVER_DIR",
    "Host",
    "RUNTIME_DIR",
    "Read",
    "UserPage",
    "comments",
    "describe",
    "driver_ready",
    "ensure_driver",
    "install_browser",
    "my_profile",
    "read",
    "resolve_host",
    "self_page",
    "user_page",
    "user_profile",
    "user_videos",
    "video_detail",
]
