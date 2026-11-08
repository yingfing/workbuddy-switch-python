"""Qoder SSE 信封流 → OpenAI SSE 伪 Response。

Qoder 加密端点的响应是明文信封：每行 ``data: {"body": "<inner>"}``，
inner 即 OpenAI chunk（model_config.format=openai）。本模块逐行剥壳，
输出标准 OpenAI SSE 字节流——网关两条消费路径（流式透传 iter_content、
非流式聚合 iter_lines）零改动复用，形态对齐 zcode 的伪 Response。

流内 error 信封（带 code/message）转成内容 chunk（``[Qoder 上游错误] …``），
与 ZCode 泳道的流内错误策略一致；heartbeat 信封静默跳过。
"""

from __future__ import annotations

import json
import uuid


def _chunk_line(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    body = {"id": cid, "model": model, "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return "data: " + json.dumps(body, ensure_ascii=False)


class _EncryptedShapeResponse:
    """把 Qoder 信封流伪装成标准 OpenAI SSE 的 requests.Response。"""

    def __init__(self, upstream, model: str):
        self._up = upstream
        self.status_code = 200
        self.ok = True
        self.headers = {"content-type": "text/event-stream"}
        self._model = model
        self._cid = f"chatcmpl-qoder-{uuid.uuid4().hex[:12]}"

    def close(self) -> None:
        self._up.close()

    @property
    def text(self) -> str:
        return ""

    def json(self) -> dict:
        raise ValueError("Qoder 上游恒为流式，请走 SSE 消费路径。")

    def _lines(self):
        """逐行剥信封，产出 OpenAI SSE 行（含结尾 [DONE]）。"""
        from .qoder import _classify_inner, _envelope_body
        buffer = ""
        emitted = False
        for chunk in self._up.iter_content(chunk_size=None):
            if not chunk:
                continue
            buffer += chunk.decode("utf-8", "replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.rstrip("\r")
                if not line.startswith("data:"):
                    continue
                out = self._consume(line[5:].strip(),
                                    _classify_inner, _envelope_body)
                if out is not None:
                    emitted = True
                    yield out
        rest = buffer.strip()
        if rest.startswith("data:"):
            out = self._consume(rest[5:].strip(), _classify_inner, _envelope_body)
            if out is not None:
                emitted = True
                yield out
        if not emitted:
            # 上游一行有效内容都没给：给可读空回复而不是静默
            yield _chunk_line(self._cid, self._model,
                              {"role": "assistant", "content": ""})
        yield _chunk_line(self._cid, self._model, {}, finish="stop")
        yield "data: [DONE]"

    def _consume(self, payload: str, classify, envelope_body) -> str | None:
        if payload == "[DONE]":
            return "data: [DONE]"
        inner = envelope_body(payload)
        if inner is None:
            return f"data: {payload}"          # 非信封行原样透传
        kind = classify(inner)
        if kind == "heartbeat":
            return None
        if kind == "error":
            try:
                parsed = json.loads(inner)
            except ValueError:
                parsed = {}
            code = parsed.get("code")
            msg = str(parsed.get("message") or parsed.get("error") or inner)[:200]
            note = f"[Qoder 上游错误{' ' + str(code) if code else ''}] {msg}"
            return _chunk_line(self._cid, self._model, {"content": note})
        return f"data: {inner}"                # chunk 原样透传

    def iter_lines(self, decode_unicode=True, **_kw):
        for line in self._lines():
            yield line

    def iter_content(self, chunk_size=None, **_kw):
        for line in self._lines():
            yield (line + "\n\n").encode("utf-8")
