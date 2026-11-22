import asyncio
import json

import httpx

from codebuddy_proxy import trae_gateway as gateway


def test_request_translation_keeps_messages_and_tools():
    source = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hello"},
              {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}],
              "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]}
    body = gateway.prepare_body(source, "user", "device", "machine")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]
    assert body["messages"][1]["tool_calls"][0]["function_call"]["name"] == "f"
    assert json.loads(body["tools"][0]["function"]["parameters"]) == {"type": "object"}
    assert (body["function"], body["config_name"], body["model_name"], body["stream"]) == ("chat_v3", "glm-5.3", "glm-5.3__dev", True)
    assert source["messages"][0]["content"] == "hello"
    assert gateway.prepare_body(source, "user", "device", "machine")["session_id"] == body["session_id"]


def test_fragmented_tools_and_usage():
    converter = gateway.Converter("glm")
    first = converter.event("output", {"tool_calls": [{"id": "call_1", "function_call": {"name": "lookup", "arguments": "{\"a\":"}}]})
    second = converter.event("output", {"tool_calls": [{"function_call": {"arguments": "1}"}}]})
    assert first[0]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_1"
    assert second[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    converter.event("token_usage", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
    assert converter.event("done", {})[0]["choices"][0]["finish_reason"] == "tool_calls"
    result = converter.result()
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"a":1}'
    assert result["usage"]["total_tokens"] == 5


def test_endpoint_stream_and_aggregate(monkeypatch):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(gateway.router)
    app.state.trae_parse_request_body = lambda request: request.json()
    app.state.trae_get_account_manager = lambda: type("Manager", (), {"trae_accounts": object()})()
    monkeypatch.setattr(gateway, "load_account", lambda accounts, requested: ("user", "jwt", "device", "machine"))
    payload = b'event: output\ndata: {"response":"hello"}\n\nevent: token_usage\ndata: {"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}\n\nevent: done\ndata: {}\n\n'
    real_client = httpx.AsyncClient
    requests = []
    def mock_upstream(request):
        requests.append(request)
        return httpx.Response(200, content=payload)
    monkeypatch.setattr(gateway.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(mock_upstream), **kwargs))
    async def run():
        async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            body = {"model": "glm-5.3", "messages": [{"role": "user", "content": "hello"}]}
            response = await client.post("/trae/v1/chat/completions", json=body)
            assert response.status_code == 200
            assert response.json()["choices"][0]["message"]["content"] == "hello"
            response = await client.post("/trae/v1/chat/completions", json={**body, "stream": True})
            assert response.status_code == 200
            assert '"content": "hello"' in response.text
            assert "data: [DONE]" in response.text
            assert requests[0].headers["x-ide-token"] == "jwt"
            assert json.loads(requests[0].content)["function"] == "chat_v3"
    asyncio.run(run())


def test_premature_stream_is_error():
    async def run():
        response = httpx.Response(200, content=b'event: output\ndata: {"response":"partial"}')
        try:
            async for _ in gateway.events(response):
                pass
        except ValueError:
            return
        assert False, "an incomplete SSE event must fail"
    asyncio.run(run())


def test_upstream_error_is_not_treated_as_completion():
    converter = gateway.Converter("glm")
    frames = converter.event("error", {"code": 4001, "message": "private upstream detail"})
    assert frames == [{"error": {"message": "Trae upstream returned an error", "type": "upstream_error", "code": 4001}}]
    assert converter.finished
    assert converter.event("done", {}) == []


def test_tool_choice_none_removes_tools():
    body = gateway.prepare_body({"model": "glm", "messages": [], "tool_choice": "none",
                                 "tools": [{"type": "function", "function": {"name": "f"}}]}, "u", "d", "m")
    assert "tools" not in body
    assert "tool_choice" not in body
