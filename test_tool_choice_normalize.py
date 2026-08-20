"""回归测试：tool_choice object → string 归一化。

根因：上游 CodeBuddy 后端（Go）的 Request.tool_choice 字段是 string 类型，
而 grok 等客户端按 OpenAI 标准发送 object 形式
`{"type":"function","function":{"name":"X"}}`，导致上游反序列化失败返回 400
（cannot unmarshal object into ... of type string）。

修复：forward_chat 转发前调用 _normalize_tool_choice()，把 object 形式转成
等价的函数名字符串。
"""

from codebuddy_proxy.__main__ import _normalize_tool_choice


def test_object_to_function_name():
    """object 形式 → 函数名字符串（最精确等价）。"""
    tc = {"type": "function", "function": {"name": "session_title"}}
    assert _normalize_tool_choice(tc) == "session_title"


def test_object_missing_name_falls_back_to_required():
    """缺 name 的 object → 'required'（退化为强制调用工具）。"""
    assert _normalize_tool_choice({"type": "function"}) == "required"


def test_string_passthrough():
    """字符串形式原样保留。"""
    for s in ("auto", "none", "required"):
        assert _normalize_tool_choice(s) == s


def test_none_passthrough():
    """None 原样返回。"""
    assert _normalize_tool_choice(None) is None


def test_other_dict_passthrough():
    """非 function 类型的 object 原样返回（不做破坏性转换）。"""
    tc = {"type": "other", "name": "x"}
    assert _normalize_tool_choice(tc) == tc
