"""Connection faults must not replay an accepted generation or hide stream failures."""

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import anyio
import pytest

import codebuddy_proxy.__main__ as proxy


URL = "https://upstream.example.test/v2/chat/completions"
BODY = {"model": "deepseek-v4.1-flash", "stream": True, "messages": []}
CHUNK = {
    "id": "chatcmpl-test", "object": "chat.completion.chunk",
    "created": 1, "model": BODY["model"],
    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}],
}
SSE = f"data: {json.dumps(CHUNK)}\n\ndata: [DONE]\n\n".encode()


@pytest.fixture
def harness(monkeypatch):
    records = []
    state = SimpleNamespace(
        write_log=lambda event, **fields: records.append({"event": event, **fields}),
        write_body_log=lambda *args, **kwargs: None,
        verbose_llm=False, logger=None,
    )
    monkeypatch.setattr(proxy, "proxy_state", state)
    monkeypatch.setattr(proxy, "diagnostic", lambda *args, **kwargs: None)
    sleep = AsyncMock()
    monkeypatch.setattr(proxy.asyncio, "sleep", sleep)
    client_class = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            proxy.httpx, "AsyncClient",
            lambda **kwargs: client_class(transport=transport, trust_env=False, **kwargs),
        )

    return SimpleNamespace(records=records, sleep=sleep, install=install)


async def read_stream(protocol="openai"):
    return b"".join([chunk async for chunk in proxy.stream_upstream(
        URL, {"Authorization": "Bearer private-token"}, BODY, protocol, BODY,
    )])


def events(harness, event):
    return [record for record in harness.records if record["event"] == event]


@pytest.mark.parametrize("protocol", ["openai", "responses", "anthropic"])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
@pytest.mark.parametrize("failure_count", [1, 2])
def test_connection_retry_recovers_once_without_duplicate_output(
    harness, protocol, error_type, failure_count,
):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) <= failure_count:
            raise error_type("connection unavailable", request=request)
        return httpx.Response(200, content=SSE)

    harness.install(handler)
    output = asyncio.run(read_stream(protocol))

    assert len(requests) == failure_count + 1
    assert all(request == BODY for request in requests)
    assert b'"error"' not in output
    retries = events(harness, "upstream_connect_retry")
    assert [record["attempt"] for record in retries] == list(range(1, failure_count + 1))
    assert [call.args[0] for call in harness.sleep.await_args_list] == [0.5, 1.0][:failure_count]
    completed, = events(harness, "stream_completed")
    assert completed["upstream_done"] is True
    assert completed["status"] == 200
    assert completed["chunk_count"] == 1
    assert all(record["request_id"] == completed["request_id"] for record in retries)
    assert not events(harness, "stream_failed")
    assert not events(harness, "upstream_connect_failed")


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_exhaustion_logs_native_error_and_never_claims_completion(harness, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        native_error = OSError(10060, "connection timed out")
        native_error.winerror = 10060
        raise error_type("All connection attempts failed", request=request) from native_error

    harness.install(handler)
    output = asyncio.run(read_stream())

    assert len(calls) == 3
    assert b'"error"' in output
    assert harness.sleep.await_count == 2
    exhausted, = events(harness, "upstream_connect_failed")
    assert exhausted["attempt"] == 3
    assert exhausted["retry_delay"] == 0
    assert exhausted["upstream_host"] == "upstream.example.test"
    assert exhausted["upstream_port"] == 443
    assert exhausted["exception_chain"][-1]["errno"] == 10060
    assert exhausted["exception_chain"][-1]["winerror"] == 10060
    failed, = events(harness, "stream_failed")
    assert failed["status"] is None
    assert failed["chunk_count"] == 0
    assert failed["upstream_done"] is False
    assert not events(harness, "stream_completed")
    assert "private-token" not in json.dumps(harness.records)


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_errors_are_not_retried(harness, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "rejected"})

    harness.install(handler)
    output = asyncio.run(read_stream())

    assert len(calls) == 1
    assert b'"error"' in output
    harness.sleep.assert_not_awaited()
    failed, = events(harness, "stream_failed")
    assert failed["status"] == status
    assert not events(harness, "stream_completed")


@pytest.mark.parametrize("error_type", [
    httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
])
@pytest.mark.parametrize("with_content", [False, True])
def test_error_after_response_headers_never_replays_post(harness, error_type, with_content):
    calls = []
    streams = []

    class InterruptedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            if with_content:
                yield f"data: {json.dumps(CHUNK)}\n\n".encode()
            raise error_type("stream interrupted")

        async def aclose(self):
            self.closed = True

    def handler(request):
        calls.append(request)
        stream = InterruptedStream()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    harness.install(handler)
    output = asyncio.run(read_stream())

    assert len(calls) == 1
    assert b'"error"' in output
    assert streams[0].closed
    harness.sleep.assert_not_awaited()
    failed, = events(harness, "stream_failed")
    assert failed["status"] == 200
    assert failed["chunk_count"] == int(with_content)
    assert not events(harness, "stream_completed")


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError])
def test_other_failures_before_response_headers_are_not_retried(harness, error_type):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("request may already have been sent", request=request)

    harness.install(handler)
    asyncio.run(read_stream())
    assert len(calls) == 1
    harness.sleep.assert_not_awaited()
    assert not events(harness, "stream_completed")


def test_eof_without_done_is_logged_as_incomplete(harness):
    harness.install(lambda request: httpx.Response(
        200, content=f"data: {json.dumps(CHUNK)}\n\n".encode(),
    ))
    asyncio.run(read_stream())

    incomplete, = events(harness, "stream_incomplete")
    assert incomplete["upstream_done"] is False
    assert incomplete["chunk_count"] == 1
    assert not events(harness, "stream_completed")


def test_cancellation_during_backoff_stops_retrying(harness):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("connection unavailable", request=request)

    harness.install(handler)
    harness.sleep.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(read_stream())

    assert len(calls) == 1
    assert len(events(harness, "stream_cancelled")) == 1
    assert not events(harness, "stream_completed")


def test_aggregation_uses_same_connection_retry_policy(harness):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection unavailable", request=request)
        return httpx.Response(200, content=SSE)

    harness.install(handler)
    result = asyncio.run(proxy.collect_upstream(URL, {}, BODY, "openai"))
    assert len(calls) == 2
    assert result["choices"][0]["message"]["content"] == "hello"
    assert len(events(harness, "upstream_connect_retry")) == 1


def test_aggregation_reports_exhaustion_as_502(harness):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("connection unavailable", request=request)

    harness.install(handler)
    with pytest.raises(proxy.HTTPException) as caught:
        asyncio.run(proxy.collect_upstream(URL, {}, BODY, "openai"))
    assert caught.value.status_code == 502
    assert len(calls) == 3
    failure, = events(harness, "upstream_error")
    assert failure["error_type"] == "ConnectError"


def test_exception_group_members_and_cycles_are_bounded():
    try:
        from exceptiongroup import ExceptionGroup
    except ImportError:
        from builtins import ExceptionGroup

    native = OSError(10061, "connection refused")
    grouped = ExceptionGroup("connection attempts", [native, OSError(10060, "timed out")])
    outer = httpx.ConnectError("All connection attempts failed")
    outer.__cause__ = grouped
    details = proxy.exception_details(outer)
    assert [item["type"] for item in details["exception_chain"]] == [
        "ConnectError", "ExceptionGroup", type(native).__name__,
        type(grouped.exceptions[1]).__name__,
    ]
    assert [item["errno"] for item in details["exception_chain"] if "errno" in item] == [10061, 10060]

    native.__cause__ = outer
    assert len(proxy.exception_details(outer)["exception_chain"]) == 4
    deep = RuntimeError("x" * 2000)
    for _ in range(50):
        parent = RuntimeError("parent")
        parent.__cause__ = deep
        deep = parent
    assert len(proxy.exception_details(deep)["exception_chain"]) == 16
    assert len(proxy.exception_details(RuntimeError("x" * 2000))["exception_chain"][0]["message"]) == 1000


def test_real_httpx_error_mapping_keeps_suppressed_native_cause(harness, monkeypatch):
    """Use HTTPX/HTTPCore's real mapping, which hides the OS error in __context__."""
    native = OSError(10060, "Connect call failed ('192.0.2.1', 443)")
    native.winerror = 10060
    connection_error = OSError("All connection attempts failed")
    connection_error.__cause__ = native
    connect = AsyncMock(side_effect=connection_error)
    monkeypatch.setattr(anyio, "connect_tcp", connect)

    output = asyncio.run(read_stream())

    assert b'"error"' in output
    assert connect.await_count == 3
    exhausted, = events(harness, "upstream_connect_failed")
    chain = exhausted["exception_chain"]
    assert chain[0]["type"] == "ConnectError"
    assert any(item["relation"] == "suppressed_context" for item in chain)
    assert any(item.get("errno") == 10060 and item.get("winerror") == 10060 for item in chain)
    assert not events(harness, "stream_completed")


def test_real_tcp_failure_recovers_when_local_upstream_starts(harness):
    """Exercise HTTPX/AnyIO's actual socket error chain, without external API calls."""
    accepted = []

    async def run():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = None

        async def serve(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                accepted.append(json.loads(await reader.readexactly(length)))
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    + f"Content-Length: {len(SSE)}\r\nConnection: close\r\n\r\n".encode()
                    + SSE
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async def start_on_retry(delay):
            nonlocal server
            if delay == 0.5:
                server = await asyncio.start_server(serve, sock=listener)

        # Not listening: the OS reports either refusal or timeout (platform dependent).
        harness.sleep.side_effect = start_on_retry
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                async with proxy.open_upstream_stream(
                    client, f"http://127.0.0.1:{port}/v2/chat/completions",
                    {}, BODY, "openai", "local-tcp-test",
                ) as response:
                    assert await response.aread() == SSE
        finally:
            if server is not None:
                server.close()
                await server.wait_closed()
            listener.close()

    asyncio.run(run())
    assert accepted == [BODY]
    retry, = events(harness, "upstream_connect_retry")
    assert retry["error_type"] in ("ConnectError", "ConnectTimeout")
    assert len(retry["exception_chain"]) >= 2
    if retry["error_type"] == "ConnectError":
        assert any("errno" in item for item in retry["exception_chain"])
    assert not events(harness, "upstream_connect_failed")
