<#
.SYNOPSIS
  把 douyin-publish-mcp 打成「本机常驻服务」用的独立可执行文件（小红书 MCP 那种形态）。

.DESCRIPTION
  打个包只为一件事：让商店配置里的 `command` 能指向**一个可执行文件的绝对路径**。
  http 家族的启动/停用都靠进程本身（客户端按进程台账 + 端口归属收进程），
  所以必须是真 exe，而不是 `uvx ...` 这种"父进程起子进程"的包装
  —— 那种停用时父进程被杀、真正监听端口的子进程会留下。

  默认用 **onedir**（一个目录 + 一个 exe，单进程）：停用时按台账就能收干净。
  `-OneFile` 也能用（单文件更省事），但 onefile 会多一层自解压子进程，
  杀父进程未必能带走子进程 —— 好在客户端的「端口归属兜底」认的是**映像名**，
  子进程映像名同样是 douyin-publish-mcp.exe，仍能被收掉（见 local_service.rs::stop_launched）。

.EXAMPLE
  # 第一次：装 PyInstaller（默认走内网 pip 源，需要显式确认）
  .\packaging\build_exe.ps1 -InstallPyInstaller

  # 常规：onedir 打包 + 拷到 %LOCALAPPDATA%\douyin-publish-mcp
  .\packaging\build_exe.ps1

  # 单文件版
  .\packaging\build_exe.ps1 -OneFile
#>
param(
  [string]$OutDir = "$env:LOCALAPPDATA\douyin-publish-mcp",
  [string]$Python = "",
  [switch]$OneFile,
  [switch]$InstallPyInstaller,
  [string]$PipIndexUrl = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # 仓库根（packaging 的上一层）

<#
★ 为什么要包一层 Invoke-Native：PowerShell 5.1（Windows 自带那个）里，
  原生命令往 stderr 写日志时，$ErrorActionPreference='Stop' 会把它变成
  NativeCommandError 并**中断整个脚本** —— 而 PyInstaller 顺利运行时也会
  往 stderr 写 INFO 日志。不包的话表现成"打包到一半停了，只看到一行 INFO"，
  很容易被误读成打包失败。
#>
function Invoke-Native {
  param([string]$Exe, [string[]]$Arguments, [string]$What)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Exe @Arguments
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($code -ne 0) { throw "$What 失败（退出码 $code）" }
}

# ── 1. 选解释器 ─────────────────────────────────────────────
if (-not $Python) {
  $candidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
    (Join-Path (Split-Path -Parent $root) ".venv\Scripts\python.exe")
  )
  $Python = ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
  if (-not $Python) { $Python = "python" }
}
Write-Host "使用解释器：$Python" -ForegroundColor Cyan
& $Python -c "import sys; print(sys.version)"

# ── 2. （可选）装 PyInstaller ───────────────────────────────
if ($InstallPyInstaller) {
  Write-Host "安装 PyInstaller（打包工具，不是本服务的运行依赖）…" -ForegroundColor Yellow
  # ★ 变量名别用 $args：那是 PowerShell 的自动变量（脚本参数数组）
  $pipArgs = @("-m", "pip", "install", "--upgrade", "pyinstaller")
  if ($PipIndexUrl) { $pipArgs += @("-i", $PipIndexUrl) }
  Invoke-Native -Exe $Python -Arguments $pipArgs -What "安装 PyInstaller"
}

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  & $Python -c "import PyInstaller" 2>$null
  $hasPyInstaller = ($LASTEXITCODE -eq 0)
} finally {
  $ErrorActionPreference = $prev
}
if (-not $hasPyInstaller) {
  throw "没装 PyInstaller。请先执行： .\packaging\build_exe.ps1 -InstallPyInstaller （内网可加 -PipIndexUrl <内网源>）"
}

# ── 3. 打包 ────────────────────────────────────────────────
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$work = Join-Path $root "build\pyinstaller"
$dist = Join-Path $root "dist"
Push-Location $root
try {
  Invoke-Native -Exe $Python -What "PyInstaller 打包" -Arguments @(
    "-m", "PyInstaller", "--noconfirm", "--clean", $mode,
    "--name", "douyin-publish-mcp",
    "--paths", (Join-Path $root "src"),
    "--workpath", $work, "--specpath", $work, "--distpath", $dist,
    (Join-Path $root "packaging\launcher.py")
  )
} finally {
  Pop-Location
}

# ── 4. 放到部署目录 ────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if ($OneFile) {
  $exe = Join-Path $dist "douyin-publish-mcp.exe"
  Copy-Item $exe (Join-Path $OutDir "douyin-publish-mcp.exe") -Force
  $exePath = Join-Path $OutDir "douyin-publish-mcp.exe"
} else {
  Copy-Item (Join-Path $dist "douyin-publish-mcp\*") $OutDir -Recurse -Force
  $exePath = Join-Path $OutDir "douyin-publish-mcp.exe"
}

Write-Host ""
Write-Host "打包完成：$exePath" -ForegroundColor Green
Write-Host "自检（应打印 health JSON）：" -ForegroundColor Cyan
Write-Host "  启动： `"$exePath`" --http --port 18080"
Write-Host "  探活： Invoke-RestMethod http://127.0.0.1:18080/health"
Write-Host ""
Write-Host "商店配置（streamable_http）里 command 就填这个绝对路径：" -ForegroundColor Cyan
Write-Host "  command = $exePath"
Write-Host "  args    = --http --port 18080"
Write-Host "  url     = http://127.0.0.1:18080/mcp"
