"""Offline wire and protocol regressions for Buddy Switch alignment."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy.__main__ as proxy
from codebuddy_proxy.backend_profile import GLOBAL_PROFILE, resolve_backend
from codebuddy_proxy.prompt_rewrite import PromptRewriter, DEGRADED_PROMPT, sanitize_fingerprints, should_degrade
from codebuddy_proxy.reasoning import ReasoningNormalizer
from codebuddy_proxy.session_context import SessionContext
from codebuddy_proxy.upstream_request import ChatRequestPreparer
from codebuddy_proxy.workbuddy_compat import normalize_deepseek


def client(backend="global", uid="account-a", endpoint=None):
    endpoint = endpoint or ("https://www.workbuddy.ai" if backend == "global" else "https://copilot.tencent.com")
    return SimpleNamespace(
        profile=SimpleNamespace(key=backend, chat_path="/v2/chat/completions"), endpoint=endpoint,
        build_url=lambda path: endpoint + path,
        auth_headers=lambda: {"Authorization": "Bearer fake", "X-User-Id": uid,
                              "X-Enterprise-Id": "enterprise", "X-Domain": "domain"},
        ensure_authenticated=lambda **kwargs: None,
    )


def prepare(body, account=None, **kwargs):
    return ChatRequestPreparer(
        client=account or client(), enable_desensitize=kwargs.pop("sanitize", False),
        desensitize_body=proxy.desensitize_body, normalize_tool_choice=proxy._normalize_tool_choice,
        diagnostic=lambda *args, **kwargs: None, **kwargs,
    ).prepare(body, "openai")


@pytest.mark.parametrize("backend,host,language", [
    ("global", "https://www.workbuddy.ai", "en-US"), ("domestic", "https://copilot.tencent.com", "zh-CN")])
def test_default_wire_identity_and_no_auth_secret_leak(backend, host, language):
    request = prepare({"messages": [{"role": "user", "content": "hi"}]}, client(backend))
    assert request.url == host + "/v2/chat/completions"
    assert request.headers["User-Agent"].startswith("WorkBuddy/")
    assert request.headers["Accept-Language"] == language
    assert request.headers["X-CodeBuddy-Request"] == "1"
    assert request.headers["X-Product"] == "SaaS"
    assert request.headers["X-B3-SpanId"] == request.headers["X-Request-ID"][:16]
    assert not any(key in request.headers for key in ("X-Refresh-Token", "X-IDE-Type", "X-Machine-Id"))
    assert resolve_backend(global_mode=True).endpoint == GLOBAL_PROFILE.default_endpoint == "https://www.workbuddy.ai"


def test_sessions_are_stable_across_turns_and_do_not_collapse_on_shared_system():
    initial = {"messages": [{"role": "system", "content": "common"}, {"role": "user", "content": "first"}]}
    later = deepcopy(initial)
    later["messages"] += [{"role": "assistant", "content": "answer"}, {"role": "user", "content": "next"}]
    other = deepcopy(initial)
    other["messages"][1]["content"] = "different initial question"
    a, b, c = [SessionContext.from_request(body) for body in (initial, later, other)]
    assert a.conversation_id == b.conversation_id != c.conversation_id
    assert a.request_id == b.request_id
    assert a.headers()["X-Conversation-Message-ID"] != a.headers()["X-Conversation-Message-ID"]


def test_explicit_session_and_trace_have_priority_and_remain_stable_on_retry():
    context = SessionContext.from_request({"session_id": "session-a"}, {
        "X-Session-ID": "session-b", "X-Conversation-Request-ID": "client-request", "X-Trace-ID": "trace"})
    assert context.conversation_id == "session-a"
    a, b = context.headers(), context.headers()
    assert a["X-Root-Request-ID"] == b["X-Root-Request-ID"] == "client-request"
    assert a["X-Trace-ID"] == "trace"
    assert len(a["X-B3-TraceId"]) == 32 and a["X-B3-TraceId"] != "client-request"


def test_cache_is_account_bound_explicit_keys_preserved_and_inputs_unchanged():
    body = {"session_id": "session", "model": "deepseek-v4.1-flash", "messages": [
        {"role": "developer", "content": "rules"}, {"role": "assistant", "content": "old", "reasoning": "thought"}]}
    original = deepcopy(body)
    a, b = prepare(body, client(uid="a")), prepare(body, client(uid="b"))
    assert a.body["prompt_cache_key"] != b.body["prompt_cache_key"]
    assert a.headers["X-Conversation-ID"] == b.headers["X-Conversation-ID"]
    assert body == original
    assert prepare({**body, "prompt_cache_key": "client-key"}).body["prompt_cache_key"] == "client-key"


@pytest.mark.parametrize("backend,wanted", [("domestic", "low"), ("global", "high")])
def test_deepseek_effort_is_region_specific_and_history_backfilled(backend, wanted):
    body = {"model": "deepseek-v4.1-flash", "reasoning_effort": "medium", "messages": [
        {"role": "assistant", "reasoning": "旧思考"}, {"role": "assistant", "content": "answer"}]}
    normalize_deepseek(body, backend)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == wanted
    assert [m["reasoning_content"] for m in body["messages"]] == ["旧思考", ""]


def test_disabled_and_non_deepseek_do_not_receive_incompatible_parameters():
    disabled = {"model": "deepseek-v4-pro", "thinking": {"type": "disabled"},
                "reasoning_effort": "max", "reasoningEffort": "max"}
    normalize_deepseek(disabled, "domestic")
    assert "reasoning_effort" not in disabled and "reasoningEffort" not in disabled
    plain = {"model": "glm-5.3"}
    normalize_deepseek(plain, "domestic")
    assert plain == {"model": "glm-5.3"}
    normalize_deepseek(plain, "domestic", {"glm-5.3": "medium"})
    assert plain["reasoning_effort"] == "low"
    assert "thinking" not in plain


def test_custom_endpoint_keeps_its_identity_and_does_not_receive_workbuddy_parameters():
    body = {"model": "deepseek-v4.1-flash", "messages": [{"role": "developer", "content": "original"}]}
    request = prepare(body, client(endpoint="https://custom.example.test"))
    assert request.body["messages"][0]["role"] == "developer"
    assert "thinking" not in request.body and "prompt_cache_key" not in request.body
    assert "X-Conversation-ID" not in request.headers and not request.prompt_scope


def test_prompt_rewriting_modes_and_multimodal_fingerprints(tmp_path):
    body = {"messages": [{"role": "developer", "content": "rules"}, {"role": "user", "content": [
        {"type": "text", "text": "X-Anthropic-Billing-Header: value; YOU ARE AN AI AGENT POWERED BY DEEPSEEK HARNESS"},
        {"type": "image_url", "image_url": {"url": "https://image.test/11128.png"}}]}]}
    original = deepcopy(body)
    assert PromptRewriter().apply(body, "a") == original
    path = tmp_path / "prompt.txt"
    path.write_text("Custom 中文", encoding="utf-8")
    custom = PromptRewriter("custom", path).apply(body, "a")
    assert custom["messages"][0] == {"role": "system", "content": "Custom 中文"}
    cleaned, changed = sanitize_fingerprints(body)
    assert changed and "the DeepSeek Harness" in cleaned["messages"][1]["content"][0]["text"]
    assert cleaned["messages"][1]["content"][1] == original["messages"][1]["content"][1]
    assert body == original
    with pytest.raises(OSError):
        PromptRewriter("custom", tmp_path / "missing.txt")
    with pytest.raises(ValueError):
        PromptRewriter("passthrough", path)


def test_degrade_expiry_and_error_classification_are_bounded_and_scope_isolated():
    rewriter = PromptRewriter()
    now = datetime(2026, 11, 21, 15, 59, tzinfo=timezone.utc).timestamp()
    assert rewriter.trigger("account-a", now)
    assert not rewriter.trigger("account-a", now + 1)
    assert rewriter.active("account-a", now + 10)
    assert not rewriter.active("account-b", now + 10)
    assert not rewriter.active("account-a", now + 61)
    assert should_degrade(400, "11128 content_blocked", False)
    assert not should_degrade(400, "11128 unapproved channel", False)
    assert should_degrade(400, "11128 unapproved channel", True)


def chunk(delta, finish=None, **extra):
    return {"id": "chat-1", "model": "deepseek-v4.1-flash", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish}], **extra}


def test_reasoning_snapshots_true_deltas_and_tools_keep_their_meaning():
    normalizer = ReasoningNormalizer()
    fragments = []
    for text in ("想", "想一想", "想", "继续"):
        result = normalizer.normalize(chunk({"reasoning_content": text, "content": "", "tool_calls": []}))
        if result:
            fragments.append(result["choices"][0]["delta"]["reasoning_content"])
    assert fragments == ["想", "一想", "继续"]
    tool = chunk({"reasoning_content": "想一想继续", "tool_calls": [{"index": 0, "function": {"name": "read"}}]})
    result = normalizer.normalize(tool)
    assert result["choices"][0]["delta"]["tool_calls"]
    assert "reasoning_content" not in result["choices"][0]["delta"]
    assert normalizer.normalize(chunk({}, "stop")) is not None
    assert normalizer.normalize({"choices": [], "usage": {"total_tokens": 2}}) is not None
    assert ReasoningNormalizer().normalize(chunk({"reasoning_content": "想"}))["choices"][0]["delta"]["reasoning_content"] == "想"


MODES = [("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
         ("/v1/responses", {"input": "hello"}),
         ("/v1/messages", {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16})]


def install(monkeypatch, handler, *, rewriter=None, sanitize=False):
    state = proxy.ProxyState(client(), None, None, enable_desensitize=sanitize,
                             prompt_rewriter=rewriter)
    monkeypatch.setattr(proxy, "proxy_state", state)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    return state


def events(response):
    return [json.loads(line[5:]) for line in response.text.splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


@pytest.mark.parametrize("path,payload", MODES)
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("fragmented", [True, False])
def test_http_wire_and_reasoning_roundtrip_for_all_protocols(monkeypatch, path, payload, stream, fragmented):
    seen = []
    usage = {"prompt_tokens": 2, "completion_tokens": 9, "total_tokens": 11,
             "completion_tokens_details": {"reasoning_tokens": 7}}
    frames = [chunk({"reasoning_content": text, "tool_calls": []}) for text in ("想", "想一想", "想")]
    frames += [chunk({"content": "答案"}, "stop", usage=usage)]
    wire = "".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames) + "data: [DONE]\n\n"

    class ByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for value in wire.replace("\n", "\r\n").encode("utf-8"):
                yield bytes([value])

    def upstream(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, stream=ByteStream()) if fragmented else httpx.Response(200, content=wire.encode())

    install(monkeypatch, upstream)
    with TestClient(proxy.app) as app:
        response = app.post(path, headers={"x-claude-code-session-id": "test-session"},
                            json={**payload, "model": "deepseek-v4.1-flash", "stream": stream})
    assert response.status_code == 200
    headers, body = seen[0]
    assert headers["x-conversation-id"] == "test-session"
    assert headers["user-agent"].startswith("WorkBuddy/")
    assert body["thinking"] == {"type": "enabled"}
    assert body["messages"][0]["role"] == "system" and body["stream"] is True
    if not stream:
        result = response.json()
        if path.endswith("completions"):
            assert result["choices"][0]["message"]["reasoning_content"] == "想一想"
        elif path.endswith("responses"):
            assert result["output"][0]["type"] == "reasoning"
            assert result["output"][0]["summary"][0]["text"] == "想一想"
            assert result["usage"]["output_tokens_details"]["reasoning_tokens"] == 7
        else:
            assert result["content"][0] == {"type": "thinking", "thinking": "想一想"}
    else:
        received = events(response)
        if path.endswith("completions"):
            thoughts = [c.get("delta", {}).get("reasoning_content", "") for event in received for c in event.get("choices", [])]
        elif path.endswith("responses"):
            thoughts = [event["delta"] for event in received if event.get("type") == "response.reasoning_summary_text.delta"]
            completed = next(event["response"] for event in received if event.get("type") == "response.completed")
            assert completed["output"][0]["summary"][0]["text"] == "想一想"
        else:
            thoughts = [event["delta"]["thinking"] for event in received if event.get("delta", {}).get("type") == "thinking_delta"]
            assert any(event.get("type") == "content_block_stop" for event in received)
        assert "".join(thoughts) == "想一想"


@pytest.mark.parametrize("stream", [True, False])
def test_prompt_retry_is_once_and_reuses_correlation(monkeypatch, stream):
    seen = []

    def upstream(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        if len(seen) == 1:
            return httpx.Response(400, json={"code": 11128, "msg": "content_blocked"})
        return httpx.Response(200, content=("data: " + json.dumps(chunk({"content": "ok"}, "stop")) + "\n\ndata: [DONE]\n\n").encode())

    install(monkeypatch, upstream)
    with TestClient(proxy.app) as app:
        response = app.post("/v1/chat/completions", headers={"x-session-id": "stable"}, json={
            "model": "test", "stream": stream, "messages": [{"role": "system", "content": "original"}, {"role": "user", "content": "hi"}]})
    assert response.status_code == 200 and len(seen) == 2
    assert seen[1][1]["messages"][0]["content"] == DEGRADED_PROMPT
    for field in ("x-conversation-id", "x-conversation-request-id"):
        assert seen[0][0][field] == seen[1][0][field]
    assert seen[0][0]["x-conversation-message-id"] != seen[1][0]["x-conversation-message-id"]
    assert seen[0][1]["prompt_cache_key"] == seen[1][1]["prompt_cache_key"]


@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize("mode,message,attempts", [
    ("passthrough", "content_blocked", 2), ("custom", "content_blocked", 1),
    ("passthrough", "unapproved channel", 1)])
def test_repeated_refusal_and_custom_prompt_do_not_loop(monkeypatch, stream, mode, message, attempts):
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(400, json={"code": 11128, "msg": message})
    install(monkeypatch, upstream, rewriter=PromptRewriter(mode))
    with TestClient(proxy.app) as app:
        result = app.post("/v1/chat/completions", json={"model": "test", "stream": stream,
            "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 400 and len(seen) == attempts


def test_context_identity_is_captured_before_optional_projection(monkeypatch):
    seen = []
    def upstream(request):
        seen.append((request.headers["x-conversation-id"], json.loads(request.content)["prompt_cache_key"]))
        return httpx.Response(200, content=b'data: [DONE]\n\n')
    state = install(monkeypatch, upstream)
    state.enable_optimize_context = True
    monkeypatch.setattr(proxy, "project_responses_chat_body", lambda body: (
        {**body, "messages": [{"role": "user", "content": "projected"}]}, {}))
    with TestClient(proxy.app) as app:
        for question in ("first", "other"):
            response = app.post("/v1/responses", json={"model": "test", "input": question})
            assert response.status_code == 200
    assert seen[0][0] != seen[1][0] and seen[0][1] != seen[1][1]


def test_json_tool_arguments_and_names_remain_valid_during_fingerprint_rewrite():
    arguments = {"count": 11128, "text": "11128", "nested": ["X-ANTHROPIC-BILLING-HEADER"]}
    body = {"messages": [{"role": "assistant", "content": None, "tool_calls": [{
        "function": {"name": "lookup_11128", "arguments": json.dumps(arguments)}}]}]}
    cleaned, changed = sanitize_fingerprints(body)
    function = cleaned["messages"][0]["tool_calls"][0]["function"]
    assert changed and function["name"] == "lookup_11128"
    assert json.loads(function["arguments"]) == {"count": 11128, "text": "11-128", "nested": ["x-anthropic-billing-hdr"]}
    assert json.loads(body["messages"][0]["tool_calls"][0]["function"]["arguments"]) == arguments


def test_deepseek_reasoning_controls_survive_protocol_translation():
    from codebuddy_proxy.responses_adapter import responses_request_to_chat
    from codebuddy_proxy.anthropic_adapter import anthropic_to_chat
    response = responses_request_to_chat({"model": "deepseek-v4-pro", "input": "hello",
        "reasoning": {"effort": "max"}, "prompt_cache_key": "explicit"})
    assert response["reasoning_effort"] == "max" and prepare(response).body["prompt_cache_key"] == "explicit"
    anthropic = anthropic_to_chat({"model": "deepseek-v4.1-flash", "thinking": {"type": "disabled"},
        "messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "history"}]},
                     {"role": "user", "content": "continue"}]})
    assert anthropic["messages"][0]["reasoning_content"] == "history"
    assert "reasoning_effort" not in prepare(anthropic).body


def test_choices_have_independent_reasoning_and_terminal_or_error_frames_survive():
    normalizer = ReasoningNormalizer()
    source = {"choices": [{"index": i, "delta": {"reasoning_content": value}} for i, value in ((0, "甲"), (1, "乙"))]}
    first = normalizer.normalize(source)
    assert [c["delta"]["role"] for c in first["choices"]] == ["assistant", "assistant"]
    source["choices"][0]["delta"]["reasoning_content"] = "甲一"
    source["choices"][1]["delta"]["reasoning_content"] = "乙二"
    assert [c["delta"]["reasoning_content"] for c in normalizer.normalize(source)["choices"]] == ["一", "二"]
    error = {"error": {"message": "failed"}}
    assert normalizer.normalize(error) == error
    assert normalizer.normalize({"choices": [], "usage": {}}) is not None


@pytest.mark.parametrize("stream", [True, False])
def test_responses_reasoning_and_custom_tools_share_stable_output_positions(monkeypatch, stream):
    def upstream(request):
        alias = json.loads(request.content)["tools"][0]["function"]["name"]
        frames = [chunk({"reasoning_content": "plan"}), chunk({"reasoning_content": "plan", "tool_calls": [{
            "index": 0, "id": "call-a", "type": "function", "function": {"name": alias, "arguments": '{"input":"中文"}'}}]}, "tool_calls")]
        return httpx.Response(200, content=("".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames) + "data: [DONE]\n\n").encode())
    install(monkeypatch, upstream)
    with TestClient(proxy.app) as app:
        result = app.post("/v1/responses", json={"model": "test", "input": "hi", "stream": stream,
            "tools": [{"type": "custom", "name": "edit"}]})
    assert result.status_code == 200
    output = (next(event["response"] for event in events(result) if event.get("type") == "response.completed")["output"]
              if stream else result.json()["output"])
    assert [item["type"] for item in output] == ["reasoning", "custom_tool_call"]
    assert output[1]["name"] == "edit" and output[1]["input"] == "中文"
    if stream:
        added = [event for event in events(result) if event.get("type") == "response.output_item.added"]
        assert [item["output_index"] for item in added] == [0, 1]
