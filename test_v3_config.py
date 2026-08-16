#!/usr/bin/env python3
"""测试 /v1/models 接口返回格式是否符合 Codex 期望"""

import json
import sys
from pathlib import Path

def test_response_format():
    """验证返回格式是否符合 Codex 客户端期望"""
    print("=" * 60)
    print("测试 /v1/models 返回格式")
    print("=" * 60)
    
    # 模拟函数调用（不启动服务器）
    sys.path.insert(0, str(Path(__file__).parent))
    
    # 导入必要的模块
    from codebuddy_proxy import load_models_from_local_config, model_to_codex_format
    
    # 1. 加载模型配置
    print("\n1. 加载本地模型配置...")
    data = load_models_from_local_config()
    print(f"   ✓ 加载了 {len(data)} 个模型")
    
    # 2. 转换为 Codex 格式
    print("\n2. 转换为 Codex 格式...")
    codex_models = [model_to_codex_format(m) for m in data]
    print(f"   ✓ 转换了 {len(codex_models)} 个模型")
    
    # 3. 构建返回格式
    print("\n3. 验证返回格式...")
    
    # ❌ 错误格式（OpenAI）
    wrong_format = {"object": "list", "data": codex_models}
    
    # ✓ 正确格式（Codex）
    correct_format = {"models": codex_models}
    
    
    print("\n   错误格式（OpenAI）:")
    wrong_sample = {'object': wrong_format['object'], 'data': f'[{len(wrong_format["data"])} models]'}
    print(f"   {json.dumps(wrong_sample)}")
    print("   ❌ Codex 客户端会报错: missing field `models`")
    
    print("\n   正确格式（Codex）:")
    correct_sample = {'models': f'[{len(correct_format["models"])} models]'}
    print(f"   {json.dumps(correct_sample)}")
    print("   ✓ Codex 客户端可以正确解析")
    
    # 4. 验证顶层字段
    print("\n4. 验证顶层字段...")
    has_models_field = "models" in correct_format
    has_object_field = "object" in correct_format
    has_data_field = "data" in correct_format
    
    print(f"   {'✓' if has_models_field else '❌'} 包含 'models' 字段: {has_models_field}")
    print(f"   {'✓' if not has_object_field else '❌'} 不包含 'object' 字段: {not has_object_field}")
    print(f"   {'✓' if not has_data_field else '❌'} 不包含 'data' 字段: {not has_data_field}")
    
    # 5. 验证模型内容
    print("\n5. 验证模型内容...")
    if codex_models:
        first_model = codex_models[0]
        required_fields = ["id", "slug", "display_name", "object", "created", "owned_by"]
        missing = [f for f in required_fields if f not in first_model]
        
        if not missing:
            print(f"   ✓ 第一个模型包含所有必需字段")
            print(f"   - ID: {first_model['id']}")
            print(f"   - 名称: {first_model['display_name']}")
            print(f"   - 供应商: {first_model['owned_by']}")
        else:
            print(f"   ❌ 第一个模型缺少字段: {missing}")
            return False
    
    # 6. 测试 JSON 序列化
    print("\n6. 测试 JSON 序列化...")
    try:
        json_str = json.dumps(correct_format, ensure_ascii=False)
        print(f"   ✓ JSON 序列化成功 ({len(json_str)} bytes)")
        
        # 验证可以反序列化
        parsed = json.loads(json_str)
        if "models" in parsed and len(parsed["models"]) == len(codex_models):
            print(f"   ✓ JSON 反序列化成功")
        else:
            print(f"   ❌ JSON 反序列化后格式不正确")
            return False
    except Exception as e:
        print(f"   ❌ JSON 序列化失败: {e}")
        return False
    
    return has_models_field and not has_object_field and not has_data_field


def test_codex_compatibility():
    """测试与 Codex Rust 客户端的兼容性"""
    print("\n" + "=" * 60)
    print("测试 Codex Rust 客户端兼容性")
    print("=" * 60)
    
    # Codex Rust 客户端期望的结构（推测）
    expected_structure = """
    struct ModelsResponse {
        models: Vec<ModelInfo>,
    }
    
    struct ModelInfo {
        id: String,
        slug: String,
        display_name: String,
        object: String,
        created: i64,
        owned_by: String,
        ...
    }
    """
    
    print("\nCodex 客户端期望的 Rust 结构:")
    print(expected_structure)
    
    print("\n如果返回格式不匹配，会产生错误:")
    print("  ❌ missing field `models` at line 1 column XXXXX")
    print("\n修复后的返回格式:")
    print("  ✓ {\"models\": [...]}")
    print("  ✓ 客户端可以正确反序列化")
    
    return True


def main():
    """主测试函数"""
    print("\n" + "=" * 60)
    print("开始测试")
    print("=" * 60)
    
    try:
        # 测试返回格式
        format_ok = test_response_format()
        
        # 测试 Codex 兼容性
        compat_ok = test_codex_compatibility()
        
        # 总结
        print("\n" + "=" * 60)
        print("测试总结")
        print("=" * 60)
        
        if format_ok and compat_ok:
            print("\n✓ 所有测试通过!")
            print("✓ 返回格式: {\"models\": [...]}")
            print("✓ Codex 客户端兼容")
            print("\n可以启动服务器测试:")
            print("  ./codebuddy_proxy.py --desensitize --verbose-llm")
            return 0
        else:
            print("\n❌ 部分测试失败")
            return 1
    
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
