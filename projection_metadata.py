"""
结构化投影元数据支持

为响应截断提供机器可读的元数据，使客户端能够：
- 准确定位被截断的内容位置
- 了解原始内容的完整长度
- 获取压缩统计信息

## 启用方式

### 请求头（推荐）
```
x-projection-metadata: structured
```

### 请求体参数（备选）
```json
{"x_projection_metadata": "structured"}
```

## 元数据结构

响应中新增 `_meta.projection` 字段：

```json
{
  "choices": [...],
  "_meta": {
    "projection": {
      "enabled": true,
      "version": "1.0",
      "truncations": [
        {
          "id": "trunc-msg2-tool-output-1",
          "type": "tool_output_lines",
          "location": {
            "message_index": 2,
            "role": "tool",
            "tool_call_id": "call_abc123",
            "field": "content"
          },
          "original_size": {"lines": 156, "chars": 8942},
          "kept": {
            "head": {"lines": 10, "chars": 453},
            "tail": {"lines": 6, "chars": 289}
          },
          "omitted": {
            "lines": 140,
            "chars": 8200,
            "position": {"start_line": 11, "end_line": 150}
          },
          "marker": "... [omitted 140 lines] ..."
        }
      ],
      "stats": {
        "total_truncations": 1,
        "original_size_chars": 8942,
        "projected_size_chars": 742,
        "compression_ratio": 0.083
      }
    }
  }
}
```

## 截断类型

- `tool_output_lines`: 工具输出行截断（超过 24 行）
- `free_text_chars`: 自由文本字符截断
- `hard_truncate`: 硬截断（尾部）
- `json_depth`: JSON 深度限制（超过 4 层）
- `json_keys`: JSON 键数量限制（超过 12 个）

## 向后兼容性

- 不携带启用参数时，响应中**不包含** `_meta` 字段（默认行为）
- 现有客户端无影响
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TruncationLocation:
    """截断位置信息"""

    message_index: int  # 消息在 messages 数组中的索引
    role: str  # assistant | user | tool | system
    field: str  # content | arguments | ...
    tool_call_id: str | None = None  # 工具调用 ID（role=tool 时）
    tool_call_index: int | None = None  # 工具调用索引（role=assistant 时）

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（移除 None 值）"""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class TruncationMetadata:
    """单个截断的元数据"""

    id: str  # 唯一标识（格式: trunc-msg{idx}-{type}-{seq}）
    type: str  # tool_output_lines | free_text_chars | hard_truncate | json_depth | json_keys
    location: TruncationLocation  # 截断位置
    original_size: dict[str, int]  # 原始大小（lines/chars/keys/depth）
    kept: dict[str, Any]  # 保留部分信息
    omitted: dict[str, Any]  # 省略部分信息
    marker: str | dict  # 人类可读标记（文本或结构化字段）

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "id": self.id,
            "type": self.type,
            "location": self.location.to_dict(),
            "original_size": self.original_size,
            "kept": self.kept,
            "omitted": self.omitted,
            "marker": self.marker,
        }


class MetadataCollector:
    """元数据收集器（在投影过程中累积截断信息）"""

    VERSION = "1.0"

    def __init__(self):
        self.truncations: list[TruncationMetadata] = []
        self._id_counters: dict[str, int] = {}  # 每种类型的序列号计数器
        self._original_chars_total: int = 0  # 原始总字符数
        self._projected_chars_total: int = 0  # 投影后总字符数

    def record_tool_output_truncation(
        self,
        location: TruncationLocation,
        original_lines: int,
        original_chars: int,
        kept_head_lines: int,
        kept_head_chars: int,
        kept_tail_lines: int,
        kept_tail_chars: int,
        omitted_lines: int,
        omitted_chars: int,
        start_line: int,
        end_line: int,
        marker: str,
    ) -> str:
        """
        记录工具输出行截断

        Args:
            location: 截断位置
            original_lines: 原始总行数
            original_chars: 原始总        kept_head_lines: 保留的头部行数
            kept_head_chars: 保留的头部字符数
            kept_tail_lines: 保留的尾部行数
            kept_tail_chars: 保留的尾部字符数
            omitted_lines: 省略的行数
            omitted_chars: 省略的字符数
            start_line: 省略部分起始行号（1-indexed）
            end_line: 省略部分结束行号（1-indexed）
            marker: 人类可读标记

        Returns:
            截断 ID
        """
        trunc_id = self._generate_id(location.message_index, "tool-output")

        metadata = TruncationMetadata(
            id=trunc_id,
            type="tool_output_lines",
            location=location,
            original_size={"lines": original_lines, "chars": original_chars},
            kept={
                "head": {"lines": kept_head_lines, "chars": kept_head_chars},
                "tail": {"lines": kept_tail_lines, "chars": kept_tail_chars},
            },
            omitted={
                "lines": omitted_lines,
                "chars": omitted_chars,
                "position": {"start_line": start_line, "end_line": end_line},
            },
            marker=marker,
        )

        self.truncations.append(metadata)
        self._update_stats(original_chars, kept_head_chars + kept_tail_chars)
        return trunc_id

    def record_free_text_truncation(
        self,
        location: TruncationLocation,
        original_chars: int,
        kept_head_chars: int,
        kept_tail_chars: int,
        omitted_chars: int,
        start_char: int,
        end_char: int,
        marker: str,
    ) -> str:
        """
        记录自由文本字符截断

        Args:
            location: 截断位置
            original_chars: 原始总字符数
            kept_head_chars: 保留的头部字符数
            kept_tail_chars: 保留的尾部字符数
            start_char: 省略部分起始字符位置（0-indexed）
      start_char: 省略部分起始字符位置（0-indexed）
            end_char: 省略部分结束字符位置（0-indexed）
            marker: 人类可读标记

        Returns:
            截断 ID
        """
        trunc_id = self._generate_id(location.message_index, "free-text")

        metadata = TruncationMetadata(
            id=trunc_id,
            type="free_text_chars",
            location=location,
            original_size={"chars": original_chars},
            kept={
                "head": {"chars": kept_head_chars},
                "tail": {"chars": kept_tail_chars},
            },
            omitted={
                "chars": omitted_chars,
                "position": {"start_char": start_char, "end_char": end_char},
            },
            marker=marker,
        )

        self.truncations.append(metadata)
        self._update_stats(original_chars, kept_head_chars + kept_tail_chars)
        return trunc_id

    def record_hard_truncation(
        self,
        location: TruncationLocation,
        original_chars: int,
        kept_chars: int,
        truncated_chars: int,
        marker: str,
    ) -> str:
        """
        记录硬截断（尾部）

        Args:
            location: 截断位置
            original_chars: 原始总字符数
            kept_chars: 保留的字符数
            truncated_chars: 截断的字符数
            marker: 人类可读标记

        Returns:
            截断 ID
        """
        trunc_id = self._generate_id(location.message_index, "hard-truncate")

        metadata = TruncationMetadata(
            id=trunc_id,
            type="hard_truncate",
            location=location,
            original_size={"chars": original_chars},
            kept={"chars": kept_chars},
            omitted={"chars": truncated_chars},
            marker=marker,
        )

        self.truncations.append(metadata)
        self._update_stats(original_chars, kept_chars)
        return trunc_id

    def record_json_keys_truncation(
        self,
        location: TruncationLocation,
        original_keys: int,
        kept_keys: int,
        kept_key_names: list[str],
        omitted_keys: int,
        omitted_key_names: list[str],
        marker: dict,
    ) -> str:
        """
        记录 JSON 键数量截断

        Args:
            location: 截断位置
            original_keys: 原始键数量
            kept_keys: 保留的键数量
            kept_key_names: 保留的键名列表
            omitted_keys: 省略的键数量
            omitted_key_names: 省略的键名列表
            marker: 结构化标记（如 {"_omitted_keys": 5}）

        Returns:
            截断 ID
        """
        trunc_id = self._generate_id(location.message_index, "json-keys")

        metadata = TruncationMetadata(
            id=trunc_id,
            type="json_keys",
            location=location,
            original_size={"keys": original_keys},
            kept={"keys": kept_keys, "names": kept_key_names},
            omitted={"keys": omitted_keys, "names": omitted_key_names},
            marker=marker,
        )

        self.truncations.append(metadata)
        # JSON 键截断不计入字符统计（因为难以准确测量）
        return trunc_id

    def record_json_depth_truncation(
        self,
        location: TruncationLocation,
        original_depth: int,
        max_depth: int,
        json_path: str,
        marker: str,
    ) -> str:
        """
        记录 JSON 深度截断

        Args:
            location: 截断位置
            original_depth: 原始嵌套深度
            max_depth: 最大允许深度
            json_path: 被截断的 JSON 路径（如 "data.items[0].metadata"）
            marker: 人类可读标记（如 "<omitted>"）

        Returns:
            截断 ID
        """
        trunc_id = self._generate_id(location.message_index, "json-depth")

        metadata = TruncationMetadata(
            id=trunc_id,
            type="json_depth",
            location=location,
            original_size={"depth": original_depth},
            kept={"depth": max_depth},
            omitted={"depth": original_depth - max_depth, "path": json_path},
            marker=marker,
        )

        self.truncations.append(metadata)
        return trunc_id

    def to_dict(self) -> dict[str, Any]:
        """
        序列化为响应中的 _meta.projection 字段

        Returns:
            字典格式的元数据（包含所有截断记录 + 统计信息）
        """
        compression_ratio = 0.0
        if self._original_chars_total > 0:
            compression_ratio = round(
                self._projected_chars_total / self._original_chars_total, 3
            )

        return {
            "enabled": True,
            "version": self.VERSION,
            "truncations": [t.to_dict() for t in self.truncations],
            "stats": {
                "total_truncations": len(self.truncations),
                "original_size_chars": self._original_chars_total,
                "projected_size_chars": self._projected_chars_total,
                "compression_ratio": compression_ratio,
            },
        }

    def _generate_id(self, message_index: int, type_slug: str) -> str:
        """
        生成截断唯一 ID

        格式: trunc-msg{idx}-{type}-{seq}
        示例: trunc-msg2-tool-output-1

        Args:
            message_index: 消息索引
            type_slug: 类型标识（tool-output | free-text | hard-truncate | json-keys | json-depth）

        Returns:
            唯一 ID
        """
        key = f"msg{message_index}-{type_slug}"
        seq = self._id_counters.get(key, 0) + 1
        self._id_counters[key] = seq
        return f"trunc-{key}-{seq}"

    def _update_stats(self, original_chars: int, projected_chars: int) -> None:
        """
        更新统计信息

        Args:
            original_chars: 原始字符数
            projected_chars: 投影后字符数
        """
        self._original_chars_total += original_chars
        self._projected_chars_total += projected_chars
