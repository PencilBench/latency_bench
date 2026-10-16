#!/usr/bin/env bash
# 在 macOS 上准备 latency_bench 的 Python 环境。
#
# 1. 确保有 Python 3.13（优先 pyenv/已安装的，否则用 Homebrew 装 python@3.13）；
# 2. 在仓库根创建 .venv；
# 3. 安装 av>=19 与 pytest，并以可编辑方式安装本项目。
#
# 用法：
#   bash scripts/setup_macos.sh
#   RECREATE=1 bash scripts/setup_macos.sh    # 强制重建 .venv

set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
RECREATE="${RECREATE:-0}"
SKIP_BREW="${SKIP_BREW:-0}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="$repo_root/.venv"
venv_python="$venv_path/bin/python"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m    %s\033[0m\n' "$1"; }
warn() { printf '\033[33m    %s\033[0m\n' "$1"; }
die()  { printf '\033[31m错误：%s\033[0m\n' "$1" >&2; exit 1; }

is_313() {
  local exe="$1"
  [[ -x "$exe" ]] || return 1
  [[ "$("$exe" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" == "$PYTHON_VERSION" ]]
}

step "仓库根：$repo_root"

# --- 1. Python ---------------------------------------------------------------
python=""
for candidate in \
  "$(command -v "python${PYTHON_VERSION}" || true)" \
  "$(command -v python3 || true)" \
  "/opt/homebrew/bin/python${PYTHON_VERSION}" \
  "/usr/local/bin/python${PYTHON_VERSION}"; do
  if [[ -n "$candidate" ]] && is_313 "$candidate"; then
    python="$candidate"
    break
  fi
done

if [[ -z "$python" && "$SKIP_BREW" != "1" ]]; then
  if command -v brew >/dev/null 2>&1; then
    warn "没有找到 Python $PYTHON_VERSION，尝试 brew install python@$PYTHON_VERSION"
    brew install "python@$PYTHON_VERSION" || warn "brew 安装失败"
    candidate="$(brew --prefix)/bin/python${PYTHON_VERSION}"
    if is_313 "$candidate"; then python="$candidate"; fi
  else
    warn "没有 Homebrew"
  fi
fi

if [[ -z "$python" ]]; then
  die "找不到 Python $PYTHON_VERSION。请先安装：
  brew install python@$PYTHON_VERSION
或从 https://www.python.org/downloads/macos/ 下载安装。"
fi
ok "Python：$python"

# --- 2. venv -----------------------------------------------------------------
if [[ "$RECREATE" == "1" && -d "$venv_path" ]]; then
  step "删除旧 .venv"
  rm -rf "$venv_path"
fi

if [[ ! -x "$venv_python" ]]; then
  step "创建 .venv"
  "$python" -m venv "$venv_path"
else
  ok ".venv 已存在"
fi

# --- 3. 依赖 -----------------------------------------------------------------
step "升级 pip"
"$venv_python" -m pip install --upgrade pip --disable-pip-version-check

step "安装依赖（av>=19, pytest）"
"$venv_python" -m pip install "av>=19,<20" "pytest>=8" --disable-pip-version-check

step "以可编辑方式安装本项目"
(cd "$repo_root" && "$venv_python" -m pip install -e . --no-build-isolation --disable-pip-version-check)

# --- 4. 能力自检 --------------------------------------------------------------
step "硬件加速能力自检（macOS 应看到 videotoolbox）"
"$venv_python" - <<'PY'
import av
from av.codec.hwaccel import hwdevices_available

print("PyAV", av.__version__)
print("FFmpeg libavcodec", ".".join(map(str, av.library_versions["libavcodec"])))
devices = hwdevices_available()
print("hwdevices_available():", ", ".join(devices))
if "videotoolbox" not in devices:
    print("  警告：当前 FFmpeg 构建没有 videotoolbox，本项目在 macOS 上无法硬解。")
for name in ("hevc", "av1"):
    try:
        codec = av.codec.Codec(name, "r")
        fmts = [c.format.name for c in codec.hardware_configs if c.is_supported]
        print(f"  {name}: hardware pix fmts = {fmts}")
    except Exception as exc:
        print(f"  {name}: 不可用（{exc}）")
print()
print("注意：AV1 硬解需要 Apple M3 及以上；更早的芯片只能软解。")
PY

echo
ok "完成。运行："
echo "    ./.venv/bin/python -m latency_bench --clips ../clips"
echo "    ./.venv/bin/python -m pytest"
