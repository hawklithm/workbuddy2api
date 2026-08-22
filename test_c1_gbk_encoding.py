"""回归测试：C1 —— 请求体/响应体仅按 UTF-8 解码，遇 GBK 抛 500。

根因：
- __main__.py 三个端点用 `await request.json()`（Starlette 内部 `json.loads(raw_bytes)`
  默认 UTF-8），GBK 字节 → `UnicodeDecodeError`（`ValueError` 子类，非 `JSONDecodeError`），
  无异常处理器 → FastAPI 默认 500。
- codebuddy_client_demo._request() 用 `raw.decode("utf-8")`，GBK 响应同样抛
  `UnicodeDecodeError`（未捕获）。

修复：
- 新增 parse_request_body()：按 utf-8 → gbk → cp936 → latin-1 依次解码再 json.loads，
  全部失败抛结构化 400（而非 500）。
- 新增 _load_json_bytes()：同样的 fallback 链，供 _request() 解析上游响应。
"""

import asyncio
import json

import pytest
from fastapi import HTTPException

from codebuddy_proxy.__main__ import parse_request_body
from codebuddy_proxy.codebuddy_client_demo import _load_json_bytes


class _FakeRequest:
    """最小 Request 替身，仅提供 parse_request_body 需要的 body()。"""

    def __init__(self, raw: bytes):
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


def _parse(raw: bytes):
    return asyncio.run(parse_request_body(_FakeRequest(raw)))


# ==============================
# 请求体侧：parse_request_body
# ==============================

def test_utf8_body_parses_normally():
    """正常 UTF-8 JSON 请求体不受影响（回归保护）。"""
    payload = {"model": "deepseek-v4-flash",
               "messages": [{"role": "user", "content": "梳理项目架构"}]}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert _parse(raw) == payload


def test_gbk_body_no_longer_500():
    """GBK 编码请求体应正常解析，而非抛 UnicodeDecodeError（500）。"""
    payload = {"model": "deepseek-v4-flash",
               "messages": [{"role": "user", "content": "你好世界，梳理项目架构"}]}
    raw = json.dumps(payload, ensure_ascii=False).encode("gbk")
    # 修复前：json.loads(gbk_bytes) → UnicodeDecodeError → 500；修复后应返回原 dict
    assert _parse(raw) == payload


def test_cp936_body_parses():
    """cp936（Windows GBK）编码请求体应正常解析。"""
    payload = {"content": "中文内容测试，编码为 cp936"}
    raw = json.dumps(payload, ensure_ascii=False).encode("cp936")
    assert _parse(raw) == payload


def test_invalid_body_returns_400_not_500():
    """UTF-8 可解码但 JSON 结构非法的请求体应返回 400，而非 500。"""
    raw = "这不是JSON".encode("utf-8")
    with pytest.raises(HTTPException) as exc_info:
        _parse(raw)
    assert exc_info.value.status_code == 400


def test_latin1_garbage_falls_through_to_400():
    """latin-1 可解码任意字节，但 JSON 结构非法时仍应抛 400（而非崩溃）。"""
    raw = b"\xff\xfe\x00{not json"
    with pytest.raises(HTTPException) as exc_info:
        _parse(raw)
    assert exc_info.value.status_code == 400


# ==============================
# 响应体侧：_load_json_bytes
# ==============================

def test_load_json_bytes_utf8():
    assert _load_json_bytes(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")) == {"ok": True}


def test_load_json_bytes_gbk():
    """GBK 编码的上游响应应能正确解码。"""
    raw = json.dumps({"msg": "成功"}, ensure_ascii=False).encode("gbk")
    assert _load_json_bytes(raw) == {"msg": "成功"}


def test_load_json_bytes_invalid_raises_jsondecode():
    """无法解析为 JSON 时抛 json.JSONDecodeError（调用方已有处理）。"""
    with pytest.raises(json.JSONDecodeError):
        _load_json_bytes(b"not json at all")
