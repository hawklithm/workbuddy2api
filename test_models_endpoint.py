#!/usr/bin/env python3
"""测试修改后的 /v1/models 端点"""

import json
import sys
from pathlib import Path

def test_local_config():
    """测试本地配置文件加载"""
    print("=" * 60)
    print("1. 测试本地配置文件加载")
    print("=" * 60)
    
    config_file = Path("models_config.json")
    if not config_file.exists():
        print(f"❌ 配置文件不存在: {config_file}")
        return False
    
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        models = data.get("models", [])
        print(f"✓ 成功加载配置文件")
        print(f"✓ 模型数量: {len(models)}")
        print(f"\n前5个模型:")
        for i, model in enumerate(models[:5], 1):
            print(f"  {i}. {model['id']:<20} - {model.get('name', 'N/A')}")
        
        return True
    
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析错误: {e}")
        return False
    except Exception as e:
        print(f"❌ 加载失败: {e}")
        return False


def test_model_fields():
    """测试模型字段完整性"""
    print("\n" + "=" * 60)
    print("2. 测试模型字段完整性")
    print("=" * 60)
    
    config_file = Path("models_config.json")
    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    models = data.get("models", [])
    required_fields = ["id", "name"]
    optional_fields = [
        "descriptionZh", "descriptionEn", 
        "maxInputTokens", "maxOutputTokens",
        "supportsImages", "supportsToolCall", "supportsReasoning",
        "vendor", "tags", "credits"
    ]
    
    print(f"\n检查必需字段: {required_fields}")
    missing_count = 0
    for model in models:
        missing = [f for f in required_fields if f not in model]
        if missing:
            print(f"  ❌ {model.get('id', 'unknown')}: 缺少 {missing}")
            missing_count += 1
    
    if missing_count == 0:
        print(f"  ✓ 所有模型都包含必需字段")
    
    print(f"\n检查常见可选字段: {optional_fields[:5]}...")
    field_stats = {f: 0 for f in optional_fields}
    for model in models:
        for field in optional_fields:
            if field in model:
                field_stats[field] += 1
    
    print("\n字段覆盖率:")
    for field, count in sorted(field_stats.items(), key=lambda x: -x[1]):
        percentage = (count / len(models)) * 100
        print(f"  {field:<25} {count:>2}/{len(models)} ({percentage:.0f}%)")
    
    return missing_count == 0


def test_codex_format_simulation():
    """模拟Codex格式转换"""
    print("\n" + "=" * 60)
    print("3. 模拟Codex格式转换")
    print("=" * 60)
    
    config_file = Path("models_config.json")
    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    models = data.get("models", [])
    
    print(f"\n转换前3个模型为Codex格式:")
    for i, model in enumerate(models[:3], 1):
        print(f"\n模型 {i}: {model['id']}")
        print(f"  - 名称: {model.get('name', 'N/A')}")
        print(f"  - 描述: {model.get('descriptionZh') or model.get('descriptionEn', 'N/A')}")
        print(f"  - 上下文窗口: {model.get('maxInputTokens', 'N/A')}")
        print(f"  - 最大输出: {model.get('maxOutputTokens', 'N/A')}")
        print(f"  - 支持图片: {model.get('supportsImages', False)}")
        print(f"  - 支持工具: {model.get('supportsToolCall', False)}")
        print(f"  - 支持推理: {model.get('supportsReasoning', False)}")
        print(f"  - 供应商: {model.get('vendor', 'N/A')}")
        print(f"  - 积分: {model.get('credits', 'N/A')}")
    
    return True


def main():
    print("测试修改后的 /v1/models 端点\n")
    
    results = []
    
    # 测试1: 加载配置文件
    results.append(("配置文件加载", test_local_config()))
    
    # 测试2: 字段完整性
    results.append(("字段完整性", test_model_fields()))
    
    # 测试3: 格式转换模拟
    results.append(("格式转换模拟", test_codex_format_simulation()))
    
    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    for name, passed in results:
        status = "✓ 通过" if passed else "❌ 失败"
        print(f"{status:<10} {name}")
    
    all_passed = all(r[1] for r in results)
    
    if all_passed:
        print("\n✓ 所有测试通过!")
        print("\n下一步:")
        print("1. 启动proxy服务器: ./codebuddy_proxy.py")
        print("2. 测试端点: curl http://localhost:8000/v1/models")
        return 0
    else:
        print("\n❌ 部分测试失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
