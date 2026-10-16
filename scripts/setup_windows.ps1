<#
.SYNOPSIS
    在 Windows 上准备 latency_bench 的 Python 环境。

.DESCRIPTION
    1. 确保有 Python 3.13（优先用已安装的，否则尝试 winget，再否则从 python.org 下载安装）；
    2. 在仓库根创建 .venv；
    3. 安装 av>=19 与 pytest，并以可编辑方式安装本项目。

    脚本可重复执行：已存在的步骤会被跳过。

.EXAMPLE
    pwsh -File scripts\setup_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.13',
    [switch]$Recreate,
    [switch]$SkipPythonInstall
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'

function Write-Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Ok($message) { Write-Host "    $message" -ForegroundColor Green }
function Write-Warn2($message) { Write-Host "    $message" -ForegroundColor Yellow }

function Test-Python313([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try {
        $out = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        return ($out.Trim() -eq $PythonVersion)
    } catch {
        return $false
    }
}

function Find-Python313 {
    $candidates = @()

    # 1) py launcher
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $found = & py "-$PythonVersion" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found.Trim() }
        } catch { }
    }

    # 2) 常见安装位置
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
    if (Test-Path $local) { $candidates += $local }
    $candidates += "C:\Python313\python.exe"

    # 3) PATH 上的 python
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }

    foreach ($c in $candidates) {
        if (Test-Python313 $c) { return $c }
    }
    return $null
}

Write-Step "仓库根：$repoRoot"

# --- 1. Python ---------------------------------------------------------------
$python = Find-Python313
if (-not $python -and -not $SkipPythonInstall) {
    Write-Warn2 "没找到 Python $PythonVersion，尝试 winget 安装"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install -e --id "Python.Python.$($PythonVersion -replace '\.','')" --scope user --silent --accept-package-agreements --accept-source-agreements
        } catch {
            Write-Warn2 "winget 安装失败：$($_.Exception.Message)"
        }
    } else {
        Write-Warn2 "没有 winget"
    }
    $python = Find-Python313
}

if (-not $python) {
    Write-Host "错误：找不到 Python $PythonVersion。" -ForegroundColor Red
    Write-Host "请手动安装后重跑本脚本："
    Write-Host "  winget install -e --id Python.Python.3.13 --scope user"
    Write-Host "或从 https://www.python.org/downloads/windows/ 下载 3.13.x amd64 安装包，"
    Write-Host "安装时勾选 'Add python.exe to PATH'。"
    exit 1
}
Write-Ok "Python：$python"

# --- 2. venv -----------------------------------------------------------------
if ($Recreate -and (Test-Path $venvPath)) {
    Write-Step "删除旧 .venv"
    Remove-Item -Recurse -Force $venvPath
}

if (-not (Test-Path $venvPython)) {
    Write-Step "创建 .venv"
    & $python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败" }
} else {
    Write-Ok ".venv 已存在"
}

# --- 3. 依赖 -----------------------------------------------------------------
Write-Step "升级 pip"
& $venvPython -m pip install --upgrade pip --disable-pip-version-check

Write-Step "安装依赖（av>=19, pytest）"
& $venvPython -m pip install "av>=19,<20" "pytest>=8" --disable-pip-version-check

Write-Step "以可编辑方式安装本项目"
Push-Location $repoRoot
try {
    & $venvPython -m pip install -e . --disable-pip-version-check
} finally {
    Pop-Location
}

# --- 4. 能力自检 --------------------------------------------------------------
Write-Step "硬件加速能力自检"
& $venvPython -c @"
import av
from av.codec.hwaccel import hwdevices_available
print('PyAV', av.__version__)
print('FFmpeg libavcodec', '.'.join(map(str, av.library_versions['libavcodec'])))
print('hwdevices_available():', ', '.join(hwdevices_available()))
for name in ('hevc', 'av1'):
    try:
        codec = av.codec.Codec(name, 'r')
        fmts = [c.format.name for c in codec.hardware_configs if c.is_supported]
        print(f'  {name}: hardware pix fmts = {fmts}')
    except Exception as exc:
        print(f'  {name}: 不可用（{exc}）')
"@

Write-Host ''
Write-Ok "完成。运行："
Write-Host "    .\.venv\Scripts\python.exe -m latency_bench --clips ..\clips"
Write-Host "    .\.venv\Scripts\python.exe -m pytest"
