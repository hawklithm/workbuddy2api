# 清理计划：只保留同步版本

## 为什么只保留同步版本？

1. **使用场景匹配**：本地个人使用，并发 < 10
2. **依赖最小化**：只需 requests，无需 fastapi/uvicorn/httpx
3. **代码更简单**：921行 vs 794行，但同步版本更易理解
4. **已修复问题**：超时问题已解决，功能完全够用

## 清理步骤

### 1. 删除 FastAPI 相关文件
```bash
rm codebuddy_proxy_fastapi.py
rm UPGRADE_TO_FASTAPI.md
rm README_FASTAPI.md
```

### 2. 简化 pyproject.toml
```bash
# 删除 fastapi optional dependencies
# 保留基础配置
```

### 3. 更新 README
```bash
# 移除 FastAPI 相关内容
# 突出同步版本的优势
```

### 4. 更新 quickstart.sh
```bash
# 移除 FastAPI 选项
# 简化为单一启动脚本
```

## 最终项目结构

```
.
├── pyproject.toml           # 简化配置
├── codebuddy_proxy.py       # 唯一的 proxy 实现
├── README.md                # 简化文档
├── quickstart.sh            # 简化启动脚本
├── requirements.txt         # 保留（向后兼容）
├── codebuddy_client_demo.py
├── desensitize.py
├── responses_projection.py
├── responses_adapter.py
├── anthropic_adapter.py
└── logs/
```

## 启动命令（清理后）

```bash
# 唯一的启动方式
uv run codebuddy_proxy.py
uv run codebuddy_proxy.py --desensitize
```

## 如果以后需要异步版本怎么办？

可以随时从 git 历史恢复：
```bash
git log --oneline --all --grep="FastAPI"
git checkout <commit> -- codebuddy_proxy_fastapi.py
```

或者参考 ~/workspace/codebuddy2api 的实现。
