"""OpenAI ↔ Anthropic Messages 协议转换。

让 ``anthropic-messages`` 格式的 3 家厂商（anthropic / bedrock / azure_openai
等注册表条目）也能从统一 OpenAI 端点调用。覆盖转换核心字段：

* 请求：system 抽离、messages content 归一、max_tokens 必填补默认、
  tools → anthropic tools、tool_choice 映射
* 响应：content[].text 拼回 choices、usage 对齐、stop_reason 映射
* 流式：anthropic SSE 事件（message_start / content_block_delta /
  message_delta / message_stop）→ OpenAI chat.completion.chunk 形状
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

DEFAULT_MAX_TOKENS = 4096


class ConvertError(Exception):
    pass


# -- 请求：OpenAI → Anthropic ---------------------------------------------------


def _content_to_anthropic(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for part in content or []:
        ptype = part.get("type")
        if ptype == "text":
            out.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                meta, _, data = url.partition(",")
                media = meta.partition(":")[2].partition(";")[0]
                out.append({"type": "image", "source":
                            {"type": "base64", "media_type": media, "data": data}})
        elif ptype == "tool_call" or ptype == "tool_use":
            out.append({"type": "tool_use", "id": part.get("id") or uuid.uuid4().hex,
                        "name": (part.get("function") or part).get("name", ""),
                        "input": (part.get("function") or part).get("arguments") or {}})
        elif ptype == "tool_result":
            out.append({"type": "tool_result", "tool_use_id": part.get("tool_call_id", ""),
                        "content": part.get("content", "")})
    return out or [{"type": "text", "text": ""}]


def to_anthropic_request(openai_payload: dict) -> dict:
    system_parts: list[str] = []
    messages = []
    for msg in openai_payload.get("messages", []):
        role, content = msg.get("role"), msg.get("content")
        if role == "system" or role == "developer":
            text = content if isinstance(content, str) else \
                "\n".join(p.get("text", "") for p in content or [] if p.get("type") == "text")
            system_parts.append(text)
            continue
        if role == "tool":                           # OpenAI tool 结果 → user/tool_result
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": msg.get("tool_call_id", ""),
                "content": content if isinstance(content, str) else json.dumps(content)}]})
            continue
        out_content = _content_to_anthropic(content)
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                out_content.append({"type": "tool_use", "id": tc.get("id") or uuid.uuid4().hex,
                                    "name": fn.get("name", ""),
                                    "input": _safe_json(fn.get("arguments"))})
        messages.append({"role": "user" if role == "user" else "assistant",
                         "content": out_content})

    body: dict[str, Any] = {
        "model": openai_payload.get("model", ""),
        "max_tokens": openai_payload.get("max_tokens")
                      or openai_payload.get("max_completion_tokens") or DEFAULT_MAX_TOKENS,
        "messages": messages,
    }
    if system_parts:
        body["system"] = "\n\n".join(filter(None, system_parts))
    if openai_payload.get("temperature") is not None:
        body["temperature"] = openai_payload["temperature"]
    if openai_payload.get("top_p") is not None:
        body["top_p"] = openai_payload["top_p"]
    if openai_payload.get("stop"):
        body["stop_sequences"] = openai_payload["stop"] if \
            isinstance(openai_payload["stop"], list) else [openai_payload["stop"]]
    tools = openai_payload.get("tools") or []
    a_tools = []
    for t in tools:
        fn = (t or {}).get("function") or {}
        if fn.get("name"):
            a_tools.append({"name": fn["name"], "description": fn.get("description", ""),
                            "input_schema": fn.get("parameters") or {"type": "object"}})
    if a_tools:
        body["tools"] = a_tools
        tc = openai_payload.get("tool_choice")
        if tc == "required":
            body["tool_choice"] = {"type": "any"}
        elif isinstance(tc, dict) and tc.get("function"):
            body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    body["stream"] = bool(openai_payload.get("stream"))
    return body


def _safe_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# -- 响应：Anthropic → OpenAI ----------------------------------------------------

_STOP_MAP = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop",
             "tool_use": "tool_calls"}


def from_anthropic_response(anthropic: dict, route_model: str) -> dict:
    text_parts, tool_calls = [], []
    for block in anthropic.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block.get("id", uuid.uuid4().hex), "type": "function",
                               "function": {"name": block.get("name", ""),
                                            "arguments": json.dumps(block.get("input") or {})}})
    message = {"role": "assistant",
               "content": "\n".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage_in = anthropic.get("usage", {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": route_model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": _STOP_MAP.get(anthropic.get("stop_reason"), "stop")}],
        "usage": {"prompt_tokens": usage_in.get("input_tokens", 0),
                  "completion_tokens": (usage_in.get("output_tokens") or 0)},
    }


# -- 流式：Anthropic SSE → OpenAI chunk --------------------------------------------


def anthropic_sse_to_openai(sse_bytes_iter: Iterator[bytes], route_model: str) -> Iterator[bytes]:
    """逐块读 anthropic SSE，转换成 OpenAI chat.completion.chunk 序列。"""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    usage_in = 0

    def chunk(delta: dict, finish=None) -> bytes:
        payload = {"id": chunk_id, "object": "chat.completion.chunk", "created": created,
                   "model": route_model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload)}\n\n".encode()

    yield chunk({"role": "assistant"})
    event_name = ""
    for raw in sse_bytes_iter:
        line = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
            continue
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        etype = data.get("type") or event_name
        if etype == "message_start":
            usage_in = (data.get("message", {}).get("usage") or {}).get("input_tokens", 0)
        elif etype == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield chunk({"content": delta.get("text", "")})
            elif delta.get("type") == "input_json_delta":
                yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": delta.get("partial_json", "")}}]})
        elif etype == "message_delta":
            finish = _STOP_MAP.get((data.get("delta") or {}).get("stop_reason"))
            out_tokens = (data.get("usage") or {}).get("output_tokens")
            payload = {"id": chunk_id, "object": "chat.completion.chunk", "created": created,
                       "model": route_model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish or "stop"}],
                       "usage": {"prompt_tokens": usage_in,
                                 "completion_tokens": out_tokens or 0}}
            yield f"data: {json.dumps(payload)}\n\n".encode()
        elif etype in ("message_stop", "error"):
            break
    yield b"data: [DONE]\n\n"
