"""
结构化投影元数据单元测试

测试覆盖：
1. 数据结构序列化
2. MetadataCollector 功能
3. 各类截断记录
4. 统计信息计算
"""

import pytest

from codebuddy_proxy.projection_metadata import (
    MetadataCollector,
    TruncationLocation,
    TruncationMetadata,
)


class TestTruncationLocation:
    """测试 TruncationLocation 数据类"""

    def test_basic_location(self):
        """测试基本位置信息"""
        loc = TruncationLocation(
            message_index=2,
            role="tool",
            field="content",
            tool_call_id="call_abc123",
        )

        assert loc.message_index == 2
        assert loc.role == "tool"
        assert loc.field == "content"
        assert loc.tool_call_id == "call_abc123"
        assert loc.tool_call_index is None

    def test_to_dict_removes_none(self):
        """测试 to_dict 移除 None 值"""
        loc = TruncationLocation(
            message_index=5,
            role="assistant",
            field="content",
        )

        result = loc.to_dict()

        assert "message_index" in result
        assert "role" in result
        assert "field" in result
        assert "tool_call_id" not in result
        assert "tool_call_index" not in result


class TestMetadataCollector:
    """测试 MetadataCollector 收集器"""

    def test_init(self):
        """测试初始化"""
        collector = MetadataCollector()

        assert len(collector.truncations) == 0
        assert collector._original_chars_total == 0
        assert collector._projected_chars_total == 0

    def test_record_tool_output_truncation(self):
        """测试记录工具输出截断"""
        collector = MetadataCollector()
        loc = TruncationLocation(
            message_index=2,
            role="tool",
            field="content",
            tool_call_id="call_abc",
        )

        trunc_id = collector.record_tool_output_truncation(
            location=loc,
            original_lines=156,
            original_chars=8942,
            kept_head_lines=10,
            kept_head_chars=453,
            kept_tail_lines=6,
            kept_tail_chars=289,
            omitted_lines=140,
            omitted_chars=8200,
            start_line=11,
            end_line=150,
            marker="... [omitted 140 lines] ...",
        )

        assert trunc_id == "trunc-msg2-tool-output-1"
        assert len(collector.truncations) == 1
        assert collector._original_chars_total == 8942
        assert collector._projected_chars_total == 742

    def test_to_dict_with_stats(self):
        """测试序列化包含统计信息"""
        collector = MetadataCollector()

        collector.record_tool_output_truncation(
            location=TruncationLocation(0, "tool", "content"),
            original_lines=156,
            original_chars=8942,
            kept_head_lines=10,
            kept_head_chars=453,
            kept_tail_lines=6,
            kept_tail_chars=289,
            omitted_lines=140,
            omitted_chars=8200,
            start_line=11,
            end_line=150,
            marker="...",
        )

        result = collector.to_dict()

        assert result["enabled"] is True
        assert result["version"] == "1.0"
        assert len(result["truncations"]) == 1
        assert result["stats"]["total_truncations"] == 1
        assert result["stats"]["original_size_chars"] == 8942
        assert result["stats"]["projected_size_chars"] == 742


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
