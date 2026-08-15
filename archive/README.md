# Archive - 归档文件

**归档时间**: 2026-08-15

本目录包含 DSML/XML 解析器开发过程中的旧版本和临时文件。

---

## 📁 目录结构

### `parsers/` - 旧版本解析器
- **dsml_parser_fix.py** (388 行, 70% 功能)
  - 快速修复版本
  - 支持基础格式和参数解析
  - 缺少高级特性（Markdown fence、CDATA、自动修复等）
  
- **xml_parser_complete.py** (有 bug)
  - 尝试完整移植但有语法错误
  - 参数解析失败
  
- **xml_parser_fixed.py** (部分修复)
  - 尝试修复 xml_parser_complete.py
  - 仍有语法错误
  
- **xml_parser_complete_v2.py** (中间版本)
  - 开发过程中的中间版本
  
- **dsml_parser.backup.*.py**
  - 自动备份文件

### `tests/` - 测试文件
- **test_xml_parser.py**
  - xml_parser_complete.py 的测试套件
  
- **test_xml_parser_fixed.py**
  - xml_parser_complete_fixed.py 的测试套件

### `docs/` - 临时文档
- **DSML_ADAPTATION_SUMMARY.md** - 早期 DSML 适配总结
- **BUGFIX_SUMMARY.md** - Bug 修复记录
- **DEPLOYMENT_GUIDE.md** - 部署指南
- **FINAL_DEPLOYMENT_SUMMARY.md** - 中间部署总结
- **FINAL_SUMMARY.md** - 早期总结
- **cleanup_plan.txt** - 清理计划

---

## 🎯 当前生产版本

**文件**: `../dsml_parser.py` (824 行)

**来源**: `xml_parser_complete_fixed.py`

**功能完整度**: 100%

**特性**:
- ✅ 所有格式支持（DSML、标准、简化、混合）
- ✅ Markdown fence 忽略
- ✅ CDATA/注释跳过
- ✅ 自动修复缺失包装器
- ✅ 精确标签配对（深度跟踪）
- ✅ 状态机扫描 (O(n) 复杂度)
- ✅ 完整递归参数解析

---

## 📚 保留文档

在父目录中保留了以下文档：

1. **COMPLETE_PARSER_DEPLOYED.md** - 完整部署文档和功能说明
2. **DSML_COMPARISON.md** - 详细技术对比（analyse_codebuddy vs ds2api）
3. **XML_PARSER_MIGRATION.md** - 迁移文档（如果存在）

---

## ⚠️ 注意事项

- 归档文件仅用于历史参考，不应用于生产环境
- 如需回滚，使用 `xml_parser_complete_fixed.py` 而非归档中的旧版本
- 定期清理归档（建议保留 6 个月后删除）

---

## 🔄 恢复方法

如果需要查看开发过程或回滚：

```bash
# 查看旧版本
cd archive/parsers
ls -lh

# 比较版本
diff dsml_parser_fix.py ../../dsml_parser.py

# 如需恢复旧版本（不推荐）
cp archive/parsers/dsml_parser_fix.py ../dsml_parser.py
```

---

**升级路径**: dsml_parser.py (原始) → dsml_parser_fix.py (70%) → xml_parser_complete_fixed.py (100%)
