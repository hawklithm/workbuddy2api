#!/bin/bash

echo "🚀 部署完整 XML 解析器"
echo "======================"
echo ""

cd ~/workspace/analyse_codebuddy

# 1. 验证新文件
echo "🔍 1. 验证语法..."
if ! python3 -m py_compile xml_parser_complete_fixed.py; then
    echo "❌ 语法错误，终止部署"
    exit 1
fi
echo "   ✅ 语法检查通过"

# 2. 快速测试
echo ""
echo "🧪 2. 快速功能测试..."
python3 xml_parser_complete_fixed.py > /tmp/parser_test.log 2>&1
if [ $? -eq 0 ]; then
    echo "   ✅ 快速测试通过"
    cat /tmp/parser_test.log | grep "总计:"
else
    echo "   ❌ 测试失败"
    cat /tmp/parser_test.log
    exit 1
fi

# 3. 备份现有文件
echo ""
echo "📦 3. 备份现有文件..."
BACKUP_NAME="dsml_parser.backup.$(date +%Y%m%d_%H%M%S).py"
cp dsml_parser.py "$BACKUP_NAME"
echo "   ✅ 备份完成: $BACKUP_NAME"

# 4. 部署新版本
echo ""
echo "✨ 4. 部署新版本..."
cp xml_parser_complete_fixed.py dsml_parser.py
echo "   ✅ 文件已替换"

# 5. 验证部署后的文件
echo ""
echo "🔍 5. 验证部署..."
python3 -c "
from dsml_parser import parse_all_tool_calls
import json

test = '<tool_call><invoke name=\"exec_command\"><cmd>pwd && ls -la</cmd></invoke></tool_call>'
result = parse_all_tool_calls(test)
args = json.loads(result[0]['function']['arguments'])

if 'cmd' in args and args['cmd'] == 'pwd && ls -la':
    print('   ✅ 部署验证通过: 参数解析正确')
else:
    print('   ❌ 部署验证失败')
    exit(1)
"

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ 部署验证失败，恢复备份..."
    cp "$BACKUP_NAME" dsml_parser.py
    exit 1
fi

# 6. 重启服务
echo ""
echo "🔄 6. 重启服务..."
pkill -f codebuddy_proxy.py
sleep 2
nohup uv run codebuddy_proxy.py --desensitize > /tmp/proxy_startup.log 2>&1 &
NEW_PID=$!

echo "   ⏳ 等待服务启动..."
sleep 3

# 7. 健康检查
echo ""
echo "💚 7. 健康检查..."
if curl -s http://127.0.0.1:8787/health 2>/dev/null | grep -q '"status": "ok"'; then
    echo "   ✅ 服务运行正常 (PID: $NEW_PID)"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🎉 部署完成！"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "📊 部署的特性："
    echo "   ✅ 状态机标签扫描"
    echo "   ✅ Markdown fence 忽略"
    echo "   ✅ CDATA/注释跳过"
    echo "   ✅ 自动修复缺失包装器"
    echo "   ✅ 递归参数解析"
    echo "   ✅ 任意标签名支持"
    echo "   ✅ 流式缓冲器"
    echo ""
    echo "🧪 测试 Claude CLI:"
    echo "   claude \"分析一下当前项目\""
    echo ""
else
    echo "   ⚠️ 无法连接健康检查端点"
    echo "   查看日志: tail -20 /tmp/proxy_startup.log"
fi

