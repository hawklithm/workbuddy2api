#!/usr/bin/env bash
#
# publish.sh — 构建并发布 workbuddy2api 到 PyPI / TestPyPI
#
# 用法:
#   ./scripts/publish.sh              # 发布到正式 PyPI（默认）
#   ./scripts/publish.sh --test       # 先发到 TestPyPI 验证流程
#   ./scripts/publish.sh --dry        # 只构建 + twine check，不上传（允许离线）
#   ./scripts/publish.sh --repo my    # 使用 ~/.pypirc 里 [my] 配置的仓库
#
# 前置要求:
#   - 项目根 .venv（python -m venv .venv 创建），内含 build/twine/setuptools/wheel
#   - PyPI 账号 + API Token（见 TOKEN 说明）
#
# 认证（从 ~/.pypirc 读取，必须用 -r 而非 --repository-url）：
#   twine 上传一律用 `twine upload -r <section>`，由 twine 读取
#   ~/.pypirc 里对应 [section] 的 username/password。
#   切勿用 --repository-url，否则 twine 不会读取 .pypirc 密码，
#   会走 trusted publishing 并返回 403。
#   也可用环境变量 PYPI_TOKEN="pypi-xxx" 覆盖（此时回退 --repository-url + __token__）。
#
# 在 ~/.pypirc 中配置（注意 pypi 与 testpypi 是两套独立账号，token 不通用）：
#   [pypi]
#   username = __token__
#   password = pypi-AgEIcHlwaS5vcmcC...
#   [testpypi]
#   username = __token__
#   password = pypi-...（TestPyPI 网站上单独生成的 token）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

REPO="pypi"
DRY=0
CUSTOM_REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) REPO="testpypi"; shift ;;
    --dry)  DRY=1; shift ;;
    --repo) CUSTOM_REPO="$2"; REPO="$2"; shift 2 ;;
    -h|--help) sed -n '3,22p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

REPO_URL="https://upload.pypi.org/legacy/"
[[ "$REPO" == "testpypi" ]] && REPO_URL="https://test.pypi.org/legacy/"
echo "==> 目标仓库: $REPO ($REPO_URL)"

# 1. venv 准备
PY="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  VENV_PY="$ROOT/.venv/bin/python"; VENV_BIN="$ROOT/.venv/bin"
  echo "==> 使用现有 .venv"
else
  echo "==> 未找到 .venv，创建并安装构建依赖 ..."
  "$PY" -m venv "$ROOT/.venv"
  VENV_PY="$ROOT/.venv/bin/python"; VENV_BIN="$ROOT/.venv/bin"
  "$VENV_PY" -m pip install -U pip build twine setuptools wheel
fi
"$VENV_BIN/twine" --version >/dev/null
"$VENV_PY" -c "import build,setuptools,wheel" 2>/dev/null || \
  "$VENV_PY" -m pip install -U build setuptools wheel

# 2. 网络检查（dry 允许离线）
if [[ "$DRY" -eq 0 ]]; then
  if ! "$VENV_PY" -c "import urllib.request,sys; urllib.request.urlopen('https://pypi.org/simple/',timeout=8); print('network OK')" 2>/dev/null; then
    echo "!! 无法访问 pypi.org，请检查网络/VPN 后重试。" >&2
    exit 1
  fi
fi

# 3. 清理
echo "==> 清理 dist/build/egg-info ..."
rm -rf dist build src/*.egg-info src/workbuddy2api.egg-info 2>/dev/null || true

# 4. 构建
echo "==> 构建 sdist + wheel ..."
"$VENV_BIN/python" -m build --no-isolation

# 5. 校验
echo "==> twine check ..."
"$VENV_BIN/twine" check dist/*

if [[ "$DRY" -eq 1 ]]; then
  echo "==> [dry-run] 已完成构建与校验，跳过上传。"
  exit 0
fi

# 6. 上传目标与认证
# 重要：必须使用 -r <section> 让 twine 从 ~/.pypirc 读取对应 section 的账号密码。
# 不要使用 --repository-url，否则 twine 不会读 .pypirc 的密码，会走 trusted
# publishing 导致 403 Invalid or non-existent authentication information。
#   - 正式 PyPI:   -r pypi      (读取 ~/.pypirc 的 [pypi])
#   - TestPyPI:    -r testpypi  (读取 ~/.pypirc 的 [testpypi]，需 TestPyPI 单独 token)
#   - 自定义仓库:  -r <name>    (读取 ~/.pypirc 的 [<name>])
TWINE_ARGS=(-r "$REPO")

# 若显式提供了 PYPI_TOKEN 环境变量，则覆盖为 __token__ 账号 + 该 token
if [[ -n "${PYPI_TOKEN:-}" ]]; then
  TWINE_ARGS=(--repository-url "$REPO_URL" --username __token__ --password "$PYPI_TOKEN")
  echo "==> 使用环境变量 PYPI_TOKEN 上传到 $REPO"
elif [[ -f "$HOME/.pypirc" ]] && grep -q "\[$REPO\]" "$HOME/.pypirc"; then
  echo "==> 使用 ~/.pypirc 中的 [$REPO] 配置上传 (twine -r $REPO)"
else
  echo "!! 未在 ~/.pypirc 找到 [$REPO] 配置，也未设置 PYPI_TOKEN 环境变量。" >&2
  echo "   请先在 ~/.pypirc 添加:" >&2
  echo "     [$REPO]" >&2
  echo "     username = __token__" >&2
  echo "     password = pypi-xxxxxxxx" >&2
  exit 1
fi

# 7. 上传
echo "==> twine upload -r $REPO ..."
"$VENV_BIN/twine" upload "${TWINE_ARGS[@]}" dist/*

echo "==> 上传完成。"
if [[ "$REPO" == "testpypi" ]]; then
  echo "    安装验证: python3 -m pip install --index-url https://test.pypi.org/simple/ workbuddy2api"
else
  echo "    安装验证: python3 -m pip install -U workbuddy2api && workbuddy2api --help"
fi
