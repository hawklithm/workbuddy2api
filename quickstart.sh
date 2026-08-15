#!/bin/bash
# uv 快速启动指南

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║         CodeBuddy Proxy - uv 快速启动                      ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# 检查 uv
if ! command -v uv &> /dev/null; then
    echo "❌ uv 未安装"
    echo ""
    echo "安装 uv:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  或"
    echo "  brew install uv"
    echo ""
    exit 1
fi

echo "✓ uv 版本: $(uv --version)"
echo ""

# 显示使用方法
cat <<'EOF'
╔════════════════════════════════════════════════════════════╗
║  使用方法（uv 会自动安装依赖，无需手动操作）               ║
╚════════════════════════════════════════════════════════════╝

【方式1】直接运行（推荐 - 最简单）
──────────────────────────────────────────────────────────
# 同步版本（本地开发）
uv run codebuddy_proxy.py
uv run codebuddy_proxy.py --desensitize

# FastAPI版本（生产环境）
uv run codebuddy_proxy_fastapi.py
uv run codebuddy_proxy_fastapi.py --desensitize


【方式2】项目级管理
──────────────────────────────────────────────────────────
# 初始化项目（创建 .venv 并安装基础依赖）
uv sync

# 安装 FastAPI 可选依赖
uv sync --extra fastapi

# 运行
uv run python codebuddy_proxy.py
uv run python codebuddy_proxy_fastapi.py


【常用参数】
──────────────────────────────────────────────────────────
--host HOST            监听地址（默认: 127.0.0.1）
--port PORT            监听端口（默认: 8787）
--desensitize          启用脱敏处理（推荐）
--optimize-context     启用上下文优化
--log-file PATH        日志文件路径


【示例命令】
──────────────────────────────────────────────────────────
# 完整配置启动
uv run codebuddy_proxy.py \
    --host 0.0.0.0 \
    --port 8787 \
    --desensitize \
    --log-file logs/proxy.jsonl

# FastAPI 高并发版本
uv run codebuddy_proxy_fastapi.py \
    --desensitize \
    --optimize-context


【测试验证】
──────────────────────────────────────────────────────────
# 另开终端，启动服务
uv run codebuddy_proxy.py &

# 健康检查
curl http://127.0.0.1:8787/health

# 查看模型
curl http://127.0.0.1:8787/v1/models


【查看日志】
──────────────────────────────────────────────────────────
tail -f logs/codebuddy-proxy.jsonl
tail -f logs/proxy.log


╔════════════════════════════════════════════════════════════╗
║  现在就开始吧！                                             ║
╚════════════════════════════════════════════════════════════╝

选择一个命令运行：

1️⃣  基础版：  uv run codebuddy_proxy.py

2️⃣  推荐版：  uv run codebuddy_proxy.py --desensitize

3️⃣  完整版：  uv run codebuddy_proxy.py --desensitize --optimize-context

4️⃣  FastAPI： uv run codebuddy_proxy_fastapi.py --desensitize

EOF

# 提示用户选择
echo ""
read -p "按 Enter 键启动默认配置（推荐版），或按 Ctrl+C 退出手动运行... " -r
echo ""
echo "🚀 启动 CodeBuddy Proxy（推荐配置）..."
echo ""

uv run codebuddy_proxy.py --desensitize
