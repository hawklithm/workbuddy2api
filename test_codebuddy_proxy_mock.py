#!/usr/bin/env python3
"""Exercise every public proxy endpoint using recorded CodeBuddy responses."""

import json
import pathlib
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import ProxyHandler, ProxyState


ROOT = pathlib.Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "codebuddy-real"


class MockProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        cls.server.proxy_state = ProxyState(  # type: ignore[attr-defined]
            CodeBuddyClient("https://invalid.test"), FIXTURES
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()

    def test_health(self):
        status, _, raw = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "ok")
        self.assertFalse(json.loads(raw)["authenticated"])

    def test_models_from_recorded_config(self):
        status, _, raw = self.request("GET", "/v1/models")
        payload = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertTrue(any(item["id"] == "deepseek-v4-flash" for item in payload["data"]))

    def test_chat_completions_stream_and_nonstream(self):
        body = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}
        status, headers, raw = self.request("POST", "/v1/chat/completions", body)
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get_content_type())
        self.assertEqual(json.loads(raw)["object"], "chat.completion")

        status, headers, raw = self.request("POST", "/v1/chat/completions", {**body, "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        self.assertIn(b"data: [DONE]", raw)

    def test_responses_stream_and_nonstream(self):
        body = {"model": "default", "input": "hi"}
        status, _, raw = self.request("POST", "/v1/responses", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["object"], "response")

        status, headers, raw = self.request("POST", "/v1/responses", {**body, "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        self.assertIn(b"response.completed", raw)

    def test_messages_stream_and_nonstream(self):
        body = {"model": "default", "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}]}
        status, _, raw = self.request("POST", "/v1/messages", body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["type"], "message")

        status, headers, raw = self.request("POST", "/v1/messages", {**body, "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/event-stream")
        self.assertIn(b"message_stop", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
