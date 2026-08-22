from codebuddy_proxy.desensitize import desensitize_body, _CLAUDE_HARNESS_SUMMARY
from codebuddy_proxy.anthropic_adapter import anthropic_request_to_chat


def test_compacts_current_claude_harness_system_block():
    body = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an interactive agent that helps users with software engineering tasks.\n"
                    "# Harness\n"
                    + ("runtime details; " * 500)
                    + "\n# Environment\n# Context management"
                ),
            }
        ]
    }

    result = desensitize_body(body, compact_harness=True)

    assert result is not body
    assert result["messages"][0]["content"] == _CLAUDE_HARNESS_SUMMARY


def test_does_not_compact_short_or_user_content():
    body = {
        "messages": [
            {"role": "system", "content": "# Harness\nshort instructions"},
            {
                "role": "user",
                "content": "You are an interactive agent that helps users with software engineering tasks."
                * 100,
            },
        ]
    }

    result = desensitize_body(body, compact_harness=True)

    assert result["messages"][0]["content"] != _CLAUDE_HARNESS_SUMMARY
    assert result["messages"][1]["content"] == body["messages"][1]["content"]


def test_anthropic_preserves_non_streaming_request():
    assert anthropic_request_to_chat({"messages": [], "stream": False})["stream"] is False
    assert anthropic_request_to_chat({"messages": [], "stream": True})["stream"] is True
    assert anthropic_request_to_chat({"messages": []})["stream"] is False
