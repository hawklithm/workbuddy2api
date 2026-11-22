"""Request-scoped, reversible Responses namespace/custom tool translation.

No global maps, caller identity substitution, network access or refusal retries.
"""
from copy import deepcopy
import hashlib
import json


class ToolTranslationError(ValueError):
    pass


class ResponsesTools:
    def __init__(self, body):
        self.by_alias = {}
        self.by_identity = {}
        self.tools = []
        self.call_aliases = {}
        self.ordinary_names = set()
        self.declared = set()
        for tool in body.get("tools") or []:
            if not isinstance(tool, dict):
                raise ToolTranslationError("Tool definitions must be objects")
            if tool.get("type") == "namespace":
                namespace = tool.get("namespace") or tool.get("name")
                if not isinstance(namespace, str) or not namespace:
                    raise ToolTranslationError("Namespace name is required")
                for nested in tool.get("tools") or []:
                    self._add(nested, namespace)
            else:
                self._add(tool, None)
        # Historical declarations may no longer be in the current tool list.
        for item in body.get("input") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("function_call", "custom_tool_call"):
                namespace = item.get("namespace")
                custom = item["type"] == "custom_tool_call"
                if namespace or custom:
                    self._register(namespace, item.get("name"), custom)
        if self.ordinary_names.intersection(self.by_alias):
            raise ToolTranslationError("Ordinary function collides with translated tool")
        self.active = bool(self.by_alias)

    def _register(self, namespace, name, custom):
        if not isinstance(name, str) or not name:
            raise ToolTranslationError("Tool name is required")
        if namespace is not None and not isinstance(namespace, str):
            raise ToolTranslationError("Namespace must be a string")
        identity = (namespace or None, name)
        if identity in self.by_identity:
            alias = self.by_identity[identity]
            if self.by_alias[alias]["custom"] != custom:
                raise ToolTranslationError("Conflicting tool types for the same identity")
            return alias
        encoded = json.dumps([namespace, name, custom], ensure_ascii=False).encode()
        alias = "rtool_" + hashlib.sha256(encoded).hexdigest()[:48]
        entry = {"namespace": namespace, "name": name, "custom": custom}
        if alias in self.by_alias and self.by_alias[alias] != entry:
            raise ToolTranslationError("Tool alias collision")
        self.by_alias[alias] = entry
        self.by_identity[identity] = alias
        return alias

    def _add(self, tool, namespace):
        if not isinstance(tool, dict):
            raise ToolTranslationError("Tool definitions must be objects")
        custom = tool.get("type") == "custom"
        if custom and tool.get("format") is not None:
            fmt = tool["format"]
            if not isinstance(fmt, dict) or fmt.get("type") != "text":
                raise ToolTranslationError("Custom grammar/constrained formats are not supported; use text format")
        if not namespace and not custom:
            fn = tool.get("function") or tool
            if isinstance(fn.get("name"), str):
                self.ordinary_names.add(fn["name"])
            self.tools.append(deepcopy(tool))
            return
        if tool.get("type") not in ("function", "custom"):
            raise ToolTranslationError("Only function/custom tools are supported inside a namespace")
        fn = tool.get("function") or tool
        alias = self._register(namespace, fn.get("name"), custom)
        if alias in self.declared:
            raise ToolTranslationError("Duplicate tool declaration")
        self.declared.add(alias)
        parameters = ({"type": "object", "properties": {"input": {"type": "string"}},
                       "required": ["input"]} if custom else
                      deepcopy(fn.get("parameters", {"type": "object", "properties": {}})))
        description = fn.get("description", "")
        if custom:
            description += "\nSupply the complete freeform tool input verbatim in the input string."
        self.tools.append({"type": "function", "name": alias,
                           "description": description, "parameters": parameters})

    def prepare(self, body):
        result = deepcopy(body)
        if "tools" in body:
            result["tools"] = self.tools
        original_items = body.get("input")
        if isinstance(original_items, list):
            for item in result["input"]:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind in ("function_call", "custom_tool_call"):
                    alias = self.by_identity.get((item.get("namespace") or None, item.get("name")))
                    if alias:
                        item["name"] = alias
                        item.pop("namespace", None)
                    if kind == "custom_tool_call":
                        if not isinstance(item.get("input"), str):
                            raise ToolTranslationError("Custom tool input must be a string")
                        item["type"] = "function_call"
                        item["arguments"] = json.dumps({"input": item.pop("input")}, ensure_ascii=False)
                elif kind == "custom_tool_call_output":
                    item["type"] = "function_call_output"
        choice = result.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") in ("function", "custom"):
            fn = choice.get("function") or choice
            alias = self.by_identity.get((choice.get("namespace") or None, fn.get("name")))
            if alias:
                result["tool_choice"] = {"type": "function", "name": alias}
        return result

    def restore_item(self, item, *, partial=False):
        result = deepcopy(item)
        entry = self.by_alias.get(result.get("name"))
        if not entry:
            return result
        result["name"] = entry["name"]
        if entry["namespace"]:
            result["namespace"] = entry["namespace"]
        if entry["custom"]:
            raw = result.pop("arguments", "")
            result["type"] = "custom_tool_call"
            if partial:
                result["input"] = ""
            else:
                try:
                    decoded = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError) as exc:
                    raise ToolTranslationError("Invalid custom tool JSON envelope") from exc
                if not isinstance(decoded, dict) or not isinstance(decoded.get("input"), str):
                    raise ToolTranslationError("Custom tool envelope requires a string input")
                result["input"] = decoded["input"]
        return result

    def events(self, events):
        restored = []
        for name, data in events:
            data = deepcopy(data)
            if "item" in data:
                item = data["item"]
                if item.get("type") == "function_call":
                    if item.get("name") in self.by_alias:
                        self.call_aliases[item.get("id")] = item["name"]
                    data["item"] = self.restore_item(item, partial=name.endswith(".added"))
            alias = self.call_aliases.get(data.get("item_id"))
            custom = self.by_alias.get(alias, {}).get("custom")
            if custom and name == "response.function_call_arguments.delta":
                # The JSON envelope can be fragmented; never expose partial escaped JSON as raw input.
                continue
            if custom and name == "response.function_call_arguments.done":
                item = self.restore_item({"name": alias, "arguments": data["arguments"]})
                base = {k: v for k, v in data.items() if k not in ("type", "arguments")}
                restored.append(("response.custom_tool_call_input.delta", {
                    **base, "type": "response.custom_tool_call_input.delta", "delta": item["input"]}))
                name = "response.custom_tool_call_input.done"
                data = {**base, "type": name, "input": item["input"]}
            if "response" in data and "output" in data["response"]:
                data["response"]["output"] = [self.restore_item(i) for i in data["response"]["output"]]
            restored.append((name, data))
        return restored
