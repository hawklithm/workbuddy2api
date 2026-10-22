"""Contract tests for the refactored upstream request preparation pipeline.

No real credentials or network calls are used.
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from codebuddy_proxy.upstream_request import ChatRequestPreparer


class FakeClient:
    def __init__(self, *, endpoint="https://copilot.tencent.com", backend="domestic"):
        self.endpoint = endpoint
        self.profile = SimpleNamespace(key=backend, chat_path="/v2/chat/completions")
        self.calls = []
        self.fail_conversation_headers = False

    def build_url(self, path):
        self.calls.append(("build_url", path))
        return self.endpoint + path

    def auth_headers(self):
        self.calls.append(("auth_headers", None))
        return {
            "Authorization": "Bearer test",
            "X-IDE-Type": "vscode",
            "X-Machine-Id": "machine-1",
        }

    def conversation_headers(self, body):
        self.calls.append(("conversation_headers", deepcopy(body)))
        if self.fail_conversation_headers:
            raise ValueError("invalid conversation metadata")
        return {
            "X-Conversation-ID": "0123456789abcdef",
            "X-Conversation-Message-ID": "new-message",
            "X-Conversation-Request-ID": "new-request",
        }


def _preparer(client, *, desensitize=False, effects=None, desensitize_fn=None,
              message_policies=None, header_policies=None):
    effects = effects if effects is not None else []
    kwargs = {}
    if message_policies is not None:
        kwargs["message_policies"] = message_policies
    if header_policies is not None:
        kwargs["header_policies"] = header_policies
    return ChatRequestPreparer(
        client=client,
        enable_desensitize=desensitize,
        desensitize_body=desensitize_fn or (lambda body, **kw: body),
        normalize_tool_choice=lambda value: (
            value["function"]["name"] if isinstance(value, dict) else value
        ),
        diagnostic=lambda event, **fields: effects.append((event, fields)),
        **kwargs,
    )


@pytest.mark.parametrize("stream", [True, False, None])
def test_preparer_normalizes_choice_and_forces_stream_without_mutating_client(stream):
    client = FakeClient()
    body = {
        "model": "test",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": {"type": "function", "function": {"name": "read"}},
        "stream": stream,
    }
    original = deepcopy(body)
    req = _preparer(client).prepare(body, "openai")

    assert req.url == "https://copilot.tencent.com/v2/chat/completions"
    assert req.stream is bool(stream)
    assert req.body["stream"] is True
    assert req.body["stream_options"] == {"include_usage": True}
    assert req.body["tool_choice"] == "read"
    assert req.headers == {
        "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
        "Authorization": "Bearer test",
        "X-IDE-Type": "vscode",
        "X-Machine-Id": "machine-1",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    assert body == original
    assert req.body is not body
    assert client.calls == [
        ("build_url", "/v2/chat/completions"),
        ("auth_headers", None),
    ]


def test_explicit_stream_options_are_preserved_by_setdefault():
    original_options = {"include_usage": False, "custom": "yes"}
    body = {"stream_options": original_options, "stream": True}
    prepared = _preparer(FakeClient()).prepare(body, "openai")
    assert prepared.body["stream_options"] is original_options
    assert original_options == {"include_usage": False, "custom": "yes"}


@pytest.mark.parametrize("endpoint,backend,roles", [
    ("https://copilot.tencent.com", "domestic", ["system", "user"]),
    ("https://www.codebuddy.ai", "global", ["developer", "user"]),
    ("https://somewhere.example", "domestic", ["developer", "user"]),
    ("https://copilot.tencent.com", "global", ["developer", "user"]),
])
def test_gateway_message_strategy_scope_and_diagnostics(endpoint, backend, roles):
    client = FakeClient(endpoint=endpoint, backend=backend)
    effects = []
    messages = [{"role": "developer", "content": "guidance"}, {"role": "user", "content": "question"}]
    body = {"messages": messages}
    original = deepcopy(body)
    req = _preparer(client, effects=effects).prepare(body, "openai")

    assert [m["role"] for m in req.body["messages"]] == roles
    assert body == original
    compat = [fields for name, fields in effects if name == "upstream_role_compat"]
    assert len(compat) == (1 if roles[0] == "system" else 0)
    if compat:
        assert compat[0] == {
            "protocol": "openai", "backend": "domestic",
            "mapping": "developer_to_system", "count": 1,
        }


def test_transform_steps_run_in_existing_order():
    effects = []
    seen = []
    client = FakeClient()

    def desensitize(body, **kwargs):
        seen.append((deepcopy(body), kwargs))
        return {**body, "messages": [{"role": "system", "content": "masked"}]}

    body = {
        "stream": False,
        "messages": [{"role": "developer", "content": "sensitive"}],
    }
    req = _preparer(
        client, desensitize=True, desensitize_fn=desensitize, effects=effects,
    ).prepare(body, "openai")

    assert seen[0][0]["messages"][0]["role"] == "system"
    assert seen[0][0]["stream"] is False
    assert seen[0][1] == {"compact_harness": True}
    assert req.body["messages"][0]["content"] == "masked"
    assert req.body["stream"] is True
    assert body["messages"][0]["role"] == "developer"
    assert [name for name, _ in effects] == ["upstream_role_compat"]


def test_intl_conversation_headers_get_fully_transformed_body():
    client = FakeClient(endpoint="https://www.codebuddy.ai", backend="global")
    effects = []
    req = _preparer(client, effects=effects).prepare(
        {"stream": False, "messages": [{"role": "developer", "content": "guide"}]},
        "openai",
    )
    conv_call = [details for name, details in client.calls if name == "conversation_headers"]
    assert len(conv_call) == 1
    assert conv_call[0] == req.body
    assert conv_call[0]["stream"] is True
    assert conv_call[0]["messages"][0]["role"] == "developer"
    assert req.headers["X-Conversation-ID"] == "0123456789abcdef"
    assert effects == [("intl_conversation_headers", {"conversation_id": "0123456789ab..."})]


def test_intl_headers_failure_is_best_effort_and_diagnostic_is_truncated():
    client = FakeClient(endpoint="https://www.codebuddy.ai", backend="global")
    client.fail_conversation_headers = True
    effects = []
    req = _preparer(client, effects=effects).prepare(
        {"messages": [{"role": "user", "content": "hello"}]}, "openai",
    )
    assert "X-Conversation-ID" not in req.headers
    assert req.headers["Authorization"] == "Bearer test"
    assert effects == [
        ("intl_conversation_headers_failed", {"error": "invalid conversation metadata"}),
    ]


def test_custom_gateway_strategies_can_be_composed_without_modifying_builder():
    steps = []

    class MessageRule:
        def apply(self, body, *, client, url, protocol, diagnostic):
            steps.append("message")
            body["extra"] = protocol

    class HeaderRule:
        def apply(self, headers, *, body, client, url, diagnostic):
            steps.append("headers")
            assert body["extra"] == "anthropic"
            assert body["stream"] is True
            headers["X-Extra"] = "enabled"

    req = _preparer(
        FakeClient(), message_policies=(MessageRule(),), header_policies=(HeaderRule(),),
    ).prepare({"model": "test"}, "anthropic")

    assert steps == ["message", "headers"]
    assert req.headers["X-Extra"] == "enabled"
    assert req.body["extra"] == "anthropic"
