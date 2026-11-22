"""Experimental Trae CN chat gateway, based on BuddySwitch's llm_utils_chat protocol."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

URL = "https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
VERSION = "0.1.50"
VERSION_CODE = "20260811"
router = APIRouter()
logger = logging.getLogger(__name__)


class TraeStreamProtocolError(ValueError):
    """A fixed diagnostic code, without response content or credentials."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def stream_error(exc):
    if isinstance(exc, TraeStreamProtocolError):
        code = exc.code
    elif isinstance(exc, httpx.TimeoutException):
        code = 'upstream_timeout'
    elif isinstance(exc, httpx.HTTPError):
        code = 'upstream_transport_error'
    else:
        code = 'upstream_protocol_error'
    logger.warning('Trae stream failed: code=%s exception=%s', code, type(exc).__name__)
    messages = {
        'invalid_sse_json': 'Trae upstream returned an invalid stream event',
        'invalid_sse_shape': 'Trae upstream returned an unsupported stream event',
        'upstream_timeout': 'Trae upstream stream timed out',
        'upstream_transport_error': 'Trae upstream stream connection failed',
        'missing_finish': 'Trae upstream stream ended without a completion event',
        'upstream_protocol_error': 'Trae upstream stream protocol failed',
    }
    return {'message': messages.get(code, messages['upstream_protocol_error']), 'type': 'upstream_error', 'code': code}


def prepare_body(body, uid, device_id, machine_id):
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise ValueError("messages must be an array")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise ValueError("stream must be a boolean")
    if "tools" in body and not isinstance(body["tools"], list):
        raise ValueError("tools must be an array")
    out = copy.deepcopy(body)
    model = out.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required")
    model = model.strip()
    aliases = {"deepseek-v4-flash": ("DeepSeek-V4-Flash", "deepseek_v4_flash__dev"),
               "deepseek-v4-pro": ("DeepSeek-V4-Pro", "deepseek_v4_pro__dev"),
               "seed-code-pro-0430": ("Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Pro__dev")}
    config, upstream_model = aliases.get(model.lower(), (model, model + "__dev"))
    for message in out["messages"]:
        if not isinstance(message, dict):
            raise ValueError("each message must be an object")
        if isinstance(message.get("content"), str):
            message["content"] = [{"type": "text", "text": message["content"]}]
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if isinstance(call, dict) and "function" in call:
                    call["function_call"] = call.pop("function")
    for tool in out.get("tools", []):
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if isinstance(function.get("parameters"), dict):
            function["parameters"] = json.dumps(function["parameters"], ensure_ascii=False, separators=(",", ":"))
    choice = out.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        out["tool_choice"] = (choice.get("function") or {}).get("name", "auto") if kind == "function" else kind
    if out.get("tool_choice") == "none":
        out.pop("tool_choice", None)
        out.pop("tools", None)
        out.pop("functions", None)
    # Explicit conversation ID is preferred. Otherwise use the first system/user
    # turns so adding subsequent turns does not change upstream session affinity.
    prefix = [next((m for m in out["messages"] if m.get("role") == role), None) for role in ("system", "user")]
    identity = str(out.get("conversation_id") or out.get("session_id") or json.dumps(prefix, ensure_ascii=False, sort_keys=True))
    def sid(salt):
        digest = hashlib.sha256((salt + uid + "|" + identity).encode()).hexdigest()
        return str(uuid.UUID(digest[:32]))
    out.update(config_name=config, model_name=upstream_model, function="chat_v3", stream=True,
               conversation_id=sid("conv"), session_id=sid("session"), project_id=sid("project"),
               user_id=uid, device_id=device_id, machine_id=machine_id, workspace_id="e04cdd",
               prompt_max_tokens=168000, mode="FunctionCall", ide_version=VERSION,
               ide_version_code=VERSION_CODE, app_id=APP_ID, package_type="stable_cn")
    return out


def headers(jwt, device_id, machine_id):
    return {"x-ide-token": jwt.removeprefix("Cloud-IDE-JWT "), "x-app-id": APP_ID,
            "x-app-version": "default", "x-app-version-code": VERSION_CODE,
            "x-ide-version": VERSION, "x-ide-version-code": VERSION_CODE,
            "x-ide-version-type": "stable", "x-device-type": "windows", "x-device-brand": "CREFG-XX",
            "x-device-cpu": "Intel", "x-device-id": device_id, "x-machine-id": machine_id,
            "x-os-version": "Windows 11 Home China", "request-traffic-type": "prod",
            "package-type": "stable_cn", "x-lgw-req-sdk-type": "3", "x-lscbd-aid": "787976",
            "x-lscbd-platform": "windows", "x-ss-dp": "787976", "app-version": VERSION,
            "user-agent": "TraeClient/TTNet", "accept": "*/*", "accept-encoding": "identity", "referer": URL}


def load_account(accounts, requested):
    from buddy_agent_auth.providers.trae.auth import TraeAuth
    from codebuddy_proxy.trae_accounts import selected_record
    with accounts.lock:
        record = selected_record(accounts, requested)
        path = accounts._path(record["id"]) / "credentials.json"
        auth = TraeAuth(creds_file=path)
        try:
            auth.load()
            if auth.creds.user_id != record["uid"]:
                raise ValueError("Trae account identity mismatch")
            if not auth.token_valid() and auth.cloudide_token_expired():
                raise ValueError("Trae account expired; log in again")
            token = auth.get_jwt()
            if not token or not auth.device.device_id or not auth.device.machine_id:
                raise ValueError("Trae account credentials or device identity missing")
            return record["uid"], token, auth.device.device_id, auth.device.machine_id
        finally:
            auth.http.close()


class Converter:
    def __init__(self, model):
        self.id, self.model, self.created = "chatcmpl-" + uuid.uuid4().hex, model, int(time.time())
        self.content, self.reasoning, self.usage = "", "", None
        self.tools, self.by_id, self.by_index, self.last = [], {}, {}, None
        self.finished = False
        self.finish_reason = "stop"

    def chunk(self, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created,
                "model": self.model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def event(self, kind, data):
        if self.finished:
            return []
        if kind == "error":
            self.finished = True
            return [{"error": {"message": "Trae upstream returned an error", "type": "upstream_error", "code": data.get("code")}}]
        if kind == "token_usage":
            self.usage = {k: data[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in data}
            return []
        if kind in ("done", "turn_completion"):
            self.finished = True
            finish = "tool_calls" if self.tools else data.get("finish_reason", "stop")
            self.finish_reason = finish
            chunk = self.chunk({}, finish)
            if self.usage is not None:
                chunk["usage"] = self.usage
            return [chunk]
        if kind not in ("output", "thought"):
            return []
        delta = {}
        text = data.get("response", data.get("thought", ""))
        if isinstance(text, str) and text:
            self.content += text
            delta["content"] = text
        reasoning = data.get("reasoning_content", "")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning += reasoning
            delta["reasoning_content"] = reasoning
        calls = []
        for call in data.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index, cid = call.get("index"), call.get("id") or ""
            slot = self.by_index.get(index) if index is not None else self.by_id.get(cid) if cid else self.last
            if slot is None:
                slot = len(self.tools)
                self.tools.append({"id": "", "name": "", "arguments": ""})
            self.last = slot
            if index is not None:
                self.by_index[index] = slot
            if cid:
                self.by_id[cid] = slot
            target = self.tools[slot]
            part = {"index": slot}
            if cid and not target["id"]:
                target["id"] = cid
                part.update(id=cid, type="function")
            function = call.get("function") or call.get("function_call") or {}
            name, args = function.get("name") or "", function.get("arguments") or ""
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
            name_delta = name[len(target["name"]):] if name.startswith(target["name"]) else ("" if target["name"].startswith(name) else name)
            target["name"] += name_delta
            args_delta = "" if args == target["arguments"] else args
            target["arguments"] += args_delta
            if name_delta or args_delta:
                part["function"] = {k: v for k, v in (("name", name_delta), ("arguments", args_delta)) if v}
            if len(part) > 1:
                calls.append(part)
        if calls:
            delta["tool_calls"] = calls
        return [self.chunk(delta)] if delta else []

    def result(self):
        message = {"role": "assistant", "content": self.content or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.tools:
            message["tool_calls"] = [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t["arguments"]}} for t in self.tools]
        result = {"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
                  "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}]}
        if self.usage is not None:
            result["usage"] = self.usage
        return result


async def events(response):
    relevant = {'output', 'thought', 'token_usage', 'done', 'turn_completion', 'error'}
    event, data = "", []
    def decode(kind, lines):
        raw = "\n".join(lines)
        if raw.strip() == '[DONE]' and kind in ('', 'done', 'turn_completion'):
            return 'done', {}
        if kind not in relevant:
            return None
        if not raw.strip() and kind in ('done', 'turn_completion'):
            return kind, {}
        try:
            value = json.loads(raw)
        except ValueError:
            raise TraeStreamProtocolError('invalid_sse_json') from None
        if value is None and kind in ('done', 'turn_completion'):
            value = {}
        if not isinstance(value, dict):
            raise TraeStreamProtocolError('invalid_sse_shape')
        return kind, value
    async for line in response.aiter_lines():
        if not line:
            if data or event in ('done', 'turn_completion'):
                parsed = decode(event, data)
                if parsed is not None:
                    yield parsed
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data or event in ('done', 'turn_completion'):
        parsed = decode(event, data)
        if parsed is not None:
            yield parsed


@router.post("/trae/v1/chat/completions")
async def trae_chat(request: Request):
    body = await request.app.state.trae_parse_request_body(request)
    manager = request.app.state.trae_get_account_manager()
    return await forward_trae(body, getattr(manager, "trae_accounts", None), request.headers.get("x-trae-account-id"))


async def forward_trae(body, accounts, account_id=None):
    try:
        # Validate before any account/network operation.
        prepare_body(body, "validation", "validation", "validation")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid Trae chat request") from None
    try:
        if accounts is None:
            raise HTTPException(503, "Trae account management unavailable")
        uid, token, device, machine = await asyncio.to_thread(load_account, accounts, account_id)
        upstream = prepare_body(body, uid, device, machine)
    except ValueError:
        raise HTTPException(400, "Set a current Trae account or select one with X-Trae-Account-Id; ensure it is logged in") from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "Trae account authentication failed; check account login") from None
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15))
    try:
        response = await client.send(client.build_request("POST", URL, headers=headers(token, device, machine), json=upstream), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "Trae upstream connection failed") from None
    if response.status_code >= 400:
        status = response.status_code
        await response.aclose()
        await client.aclose()
        raise HTTPException(status if status in (401, 403, 429) else 502, "Trae upstream request failed")
    converter = Converter(body["model"])
    async def stream():
        try:
            yield "data: " + json.dumps(converter.chunk({"role": "assistant"}), ensure_ascii=False) + "\n\n"
            async for kind, data in events(response):
                for chunk in converter.event(kind, data):
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                if converter.finished:
                    break
            if not converter.finished:
                error = stream_error(TraeStreamProtocolError('missing_finish'))
                yield 'data: ' + json.dumps({'error': error}, ensure_ascii=False) + '\n\n'
        except (ValueError, httpx.HTTPError) as exc:
            yield 'data: ' + json.dumps({'error': stream_error(exc)}, ensure_ascii=False) + '\n\n'
        finally:
            await response.aclose()
            await client.aclose()
        yield "data: [DONE]\n\n"
    if body.get("stream", False):
        return StreamingResponse(stream(), media_type="text/event-stream")
    try:
        async for kind, data in events(response):
            chunks = converter.event(kind, data)
            if chunks and "error" in chunks[0]:
                return JSONResponse(chunks[0], status_code=502)
            if converter.finished:
                break
        if not converter.finished:
            raise TraeStreamProtocolError('missing_finish')
        return JSONResponse(converter.result())
    except (ValueError, httpx.HTTPError) as exc:
        stream_error(exc)
        raise HTTPException(502, "Trae upstream stream failed") from None
    finally:
        await response.aclose()
        await client.aclose()
