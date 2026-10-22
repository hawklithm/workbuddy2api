# CodeBuddy Proxy: request/response boundary refactor

## Scope and constraints

This is a structural refactor, **not** a protocol or behavior change.
It follows the already-added domestic `developer -> system` compatibility
implementation. Existing routes, billing, authentication, retry behavior,
stream parsing, OpenAI/Anthropic/Responses payload contracts, response status
handling, and diagnostics remain in place.

The goal is to reduce the responsibility of the large FastAPI entry point
(`src/codebuddy_proxy/__main__.py`) without moving the high-risk streaming
state machine.

## Responsibilities

| Module | Responsibility | Pattern |
| --- | --- | --- |
| `__main__.py` | HTTP routes, authentication check, stream/non-stream dispatch, lifecycle, error handling | Controller / orchestration |
| `upstream_request.py` | Prepare the upstream URL, normalized request body, stream flags, authentication and gateway headers | Builder + composable policies |
| `DomesticDeveloperRolePolicy` | Transform unsupported `developer` messages only for the verified domestic gateway | Message strategy |
| `InternationalConversationHeaderPolicy` | Inject `X-Conversation-*` headers for the same URLs as before, with nonfatal fallback | Header strategy |
| `protocol_response.py` | Convert aggregated OpenAI-shaped output into client-compatible objects | Strategy registry |
| `billing_proxy.py` | Independent authenticated billing endpoints | Existing service module |

## Request preparation order (unchanged)

1. `state.ensure_auth()` and log the original client request summary.
2. Shallow-copy the body and normalize `tool_choice`.
3. Resolve the configured upstream chat URL.
4. Apply domestic developer-role compatibility when applicable.
5. Run optional `--desensitize` and harness compaction.
6. Force upstream `stream=True` while retaining the incoming stream preference separately, and default `stream_options`.
7. Build original authentication / IDE / SSE request headers.
8. Apply international conversation headers where the existing URL guard matches.
9. Emit original IDE diagnostic when enabled.
10. Leave the existing streaming preflight or aggregate code to send the prepared body unchanged.

The `codebuddy.ai` substring check for international headers is deliberately
preserved as an observable compatibility detail. Tightening hostname matching
would be a separate behavior-changing fix and is out of scope.

## Nonstream protocol formatting (unchanged)

The aggregation layer continues producing the same OpenAI-style completion.
`convert_nonstream` is retained as a public/local wrapper, delegating to:

- `OpenAIFormatter`: returns the *same* response object.
- `AnthropicFormatter`: produces `msg_*`, text/tool-use blocks, stop reason,
  and mapped usage counters.
- `ResponsesFormatter`: produces `resp_*`, output-text/function-call blocks,
  and completion status.
- Unknown protocol: same identity fallback as before.

The formatter registry accepts custom implementations without editing route
or upstream transport code. ID generation and the existing clock callback
remain injectable and produce the same output structures.

## Regression safeguards

- `test_upstream_request_pipeline.py` checks preparation order, original
  input immutability, stream settings, token headers, tool choice, gateway
  policy scope, nonblocking international-header failures, and policy injection.
- `test_protocol_response_strategies.py` checks exact legacy wire payload
  structures for all supported protocol formatters, malformed tool arguments,
  empty choices, identity fallback, and custom formatters.
- `test_intl_conversation_headers.py` replaces a brittle source-inspection
  assertion with functional verification of emitted headers.
- Existing protocol, streaming, billing, developer-role, and retry tests are
  run unchanged.

The known Windows-only `stat.S_IMODE(...) == 0o600` test assertion fails
on NTFS as it did before this refactor. The rest of the suite is expected
to pass with that one environment-specific test deselected.

## Next phases intentionally excluded

Streaming SSE parsing / DSML, the model catalog cache, and login/session
persistence are more stateful and require additional characterization tests.
They are deliberately left for separate narrowly scoped changes instead of
being rewritten alongside the request/response boundary.
