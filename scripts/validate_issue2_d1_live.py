#!/usr/bin/env python3
"""Opt-in LIVE Issue #2 / D1 verification against an already running proxy.

Use the *PR #9 code* with a real upstream login. No commands are executed by
this script: function calls/outputs are only conversation history fixtures.
This script never prints response bodies, authentication data, or model text.

Example:
  python scripts/validate_issue2_d1_live.py --model glm-5.2
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def d1_payload(model: str) -> dict:
    return {
        "model": model,
        "stream": True,
        "max_output_tokens": 64,
        "input": [
            {"type": "message", "role": "user", "content": "run two commands"},
            {"type": "function_call", "call_id": "call_a1", "name": "shell",
             "arguments": '{"command":"ls"}'},
            {"type": "function_call", "call_id": "call_a2", "name": "shell",
             "arguments": '{"command":"pwd"}'},
            {"type": "function_call_output", "call_id": "call_a1", "output": "first result"},
            {"type": "message", "role": "developer", "content": "context interleaved"},
            {"type": "function_call_output", "call_id": "call_a2", "output": "second result"},
            {"type": "message", "role": "user", "content": "Acknowledge both outputs briefly."},
        ],
        "tools": [{
            "type": "function", "name": "shell", "description": "A dummy tool for protocol compatibility",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    events: dict[str, int] = {}
    completed = False
    failed = False
    url = args.base_url.rstrip("/") + "/responses"
    try:
        with httpx.Client(timeout=args.timeout) as client:
            with client.stream("POST", url, json=d1_payload(args.model)) as response:
                print(f"HTTP status: {response.status_code}")
                if response.status_code != 200:
                    print("FAIL: upstream/proxy rejected the D1 history; inspect private server logs.")
                    return 1
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:].strip()
                        events[event] = events.get(event, 0) + 1
                        if event == "response.completed":
                            completed = True
                        if event == "response.failed":
                            failed = True
                    elif line.startswith("data: ") and line[6:].strip() != "[DONE]":
                        try:
                            obj = json.loads(line[6:])
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and obj.get("error"):
                            failed = True
    except httpx.HTTPError as exc:
        print(f"FAIL: transport error: {type(exc).__name__}")
        return 1

    print("SSE events:", json.dumps(events, sort_keys=True))
    if failed or not completed:
        print("FAIL: missing response.completed or received a failure event.")
        return 1
    print("PASS: D1 interleaved history received response.completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
