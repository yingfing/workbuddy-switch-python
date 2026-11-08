"""Qoder 渠道：静态模型目录 + wasm 加密推理（COSY 签名）。

**模型目录**：Qoder 无公开目录端点，官方客户端同样使用内置静态表。
本表逐条搬运 dsh-xdpool vendor/channel-pack 的 QODER_FALLBACK_MODELS
（国际版 17 条 / 国内版 14 条，2026-09 校准），``cm`` 即官方
priceFactor（计费倍率，0 = 免费模型），``cw`` 为上下文窗口（官方
档位表最大档）。

**对话推理**：官方 wasm（qoder-auth-wasm，随包附带）生成
``Bearer COSY.<载荷>.<签名>`` 并加密请求体；响应是**明文信封**——
每行 ``data: {"body": "<inner>"}``，剥壳即 OpenAI chunk（model_config
format=openai）。端点由 wasm 下发：
国际 ``api2.qoder.sh`` / 国内 ``gateway.qoder.com.cn``，
路径 ``/algo/api/v2/service/pro/sse/agent_chat_generation``。

协议依据 dsh-xdpool 2.6.1 vendor/channel-pack（实测注释同步吸收）：
* payload 必须带 ``business: {"type": "agent"}``，缺了服务端把请求
  路由到故障节点（实测 2026-09-20：qfmodel 恒 ``[FAIL]node``）；
* ``model_config`` 按官方 ``Uyc()`` 的 10 字段逐项对齐；
* ``tools`` 是**顶层**字段且无工具时空数组（缺了模型只能正文臆造调用）；
* inner 分类（qoder-envelope.ts）：有 choices/usage → chunk；带
  code/message/error/statusCodeValue/type → error；其余 → heartbeat 跳过。
"""

from __future__ import annotations

import json
import uuid

from .document import Document

_M = 1_000_000

COSY_VERSION = "1.1.49"                       # 官方 wasm 客户端版本（pack.js 常量）
CLIENT_METADATA = {"client_type": "5", "business_product": "cli",
                   "business_type": "agent", "scene": "assistant"}
HOSTS = {"qoder": "https://api2.qoder.sh",
         "qodercn": "https://gateway.qoder.com.cn"}
# 模型元数据（fallback 表）：name → (supports_thinking, supports_image, context_window)
FALLBACK_META: dict[str, dict] = {
    # 国际版
    "auto": {"cw": 200_000}, "ultimate": {"cw": _M}, "performance": {"cw": _M},
    "efficient": {"cw": _M}, "smodel": {"cw": _M}, "cmodel": {"cw": _M},
    "qmodel_38max": {"cw": _M}, "qfmodel": {"cw": _M, "thinking": True, "image": True},
    "qmodel_latest": {"cw": _M}, "qmodel": {"cw": _M},
    "kmodel_latest": {"cw": _M, "thinking": True}, "kmodel": {"cw": _M},
    "gmodel": {"cw": _M, "thinking": True}, "gfmodel": {"cw": _M},
    "dmodel": {"cw": _M, "thinking": True, "image": True},
    "dfmodel": {"cw": _M, "thinking": True, "image": True},
    "mmodel": {"cw": _M},
    # 国内版独有
    "q37fmodel": {"cw": _M}, "gm51model": {"cw": _M},
}

CATALOGS: dict[str, list[dict]] = {
    "qoder": [                                   # 国际版（17 条）
        {"id": "auto", "name": "Auto", "cw": 200_000, "cm": 0.5},
        {"id": "ultimate", "name": "Ultimate", "cw": _M, "cm": 2.0},
        {"id": "performance", "name": "Performance", "cw": _M, "cm": 1.1},
        {"id": "efficient", "name": "Efficient", "cw": _M, "cm": 0.3},
        {"id": "smodel", "name": "Sonus", "cw": _M, "cm": 8.0},
        {"id": "cmodel", "name": "Cantus", "cw": _M, "cm": 4.0},
        {"id": "qmodel_38max", "name": "Qwen3.8-Max", "cw": _M, "cm": 0.2},
        {"id": "qfmodel", "name": "Qwen3.8-Flash", "cw": _M, "cm": 0.0},
        {"id": "qmodel_latest", "name": "Qwen3.7-Max", "cw": _M, "cm": 0.1},
        {"id": "qmodel", "name": "Qwen3.7-Plus", "cw": _M, "cm": 0.04},
        {"id": "kmodel_latest", "name": "Kimi-K3", "cw": _M, "cm": 1.4},
        {"id": "kmodel", "name": "Kimi-K2.8-Preview", "cw": _M, "cm": 0.8},
        {"id": "gmodel", "name": "GLM-5.3", "cw": _M, "cm": 0.8},
        {"id": "gfmodel", "name": "GLM-5.3-Flash", "cw": _M, "cm": 0.1},
        {"id": "dmodel", "name": "DeepSeek-V4-Pro", "cw": _M, "cm": 0.5},
        {"id": "dfmodel", "name": "DeepSeek-Flash", "cw": _M, "cm": 0.1},
        {"id": "mmodel", "name": "MiniMax-M3", "cw": _M, "cm": 0.2},
    ],
    "qodercn": [                                 # 国内版（14 条）
        {"id": "auto", "name": "Auto", "cw": 200_000, "cm": 0.5},
        {"id": "qmodel_38max", "name": "Qwen3.8-Max", "cw": _M, "cm": 0.2},
        {"id": "qfmodel", "name": "Qwen3.8-Flash", "cw": _M, "cm": 0.0},
        {"id": "qmodel_latest", "name": "Qwen3.7-Max", "cw": _M, "cm": 0.1},
        {"id": "qmodel", "name": "Qwen3.7-Plus", "cw": _M, "cm": 0.04},
        {"id": "q37fmodel", "name": "Qwen3.7-Flash", "cw": _M, "cm": 0.1},
        {"id": "dmodel", "name": "DeepSeek-V4-Pro", "cw": _M, "cm": 0.5},
        {"id": "dfmodel", "name": "DeepSeek-Flash", "cw": _M, "cm": 0.1},
        {"id": "gmodel", "name": "GLM-5.3", "cw": _M, "cm": 0.8},
        {"id": "gfmodel", "name": "GLM-5.3-Flash", "cw": _M, "cm": 0.1},
        {"id": "gm51model", "name": "GLM-5.2", "cw": _M, "cm": 0.6},
        {"id": "kmodel_latest", "name": "Kimi-K3", "cw": _M, "cm": 1.4},
        {"id": "kmodel", "name": "Kimi-K2.8-Preview", "cw": _M, "cm": 0.8},
        {"id": "mmodel", "name": "MiniMax-M2.7", "cw": 200_000, "cm": 0.2},
    ],
}


def _catalog_row(channel_id: str, model_key: str) -> dict | None:
    for row in CATALOGS.get(channel_id) or CATALOGS["qoder"]:
        if row["id"] == model_key:
            return row
    return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
            and isinstance(b.get("text"), str))
    return ""


def build_infer_payload(payload: dict, channel_id: str) -> dict:
    """OpenAI 请求 → Qoder 明文推理 payload（对齐 buildQoderInferPayload）。

    system 消息拎到顶层 ``system`` 字段（与官方客户端一致——history 里
    不含 system）；图片块不支持（附件服务未接）转文本占位说明。
    """
    messages = payload.get("messages") or []
    system_text = "\n\n".join(
        t for t in (_text_of(m.get("content")) for m in messages
                    if isinstance(m, dict) and m.get("role") == "system") if t)
    history = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") in ("system", "developer"):
            continue
        role = m.get("role")
        content = _text_of(m.get("content"))
        tool_calls = m.get("tool_calls")
        tool_calls = tool_calls if isinstance(tool_calls, list) and tool_calls else None
        tool_call_id = m.get("tool_call_id")
        tool_call_id = tool_call_id if isinstance(tool_call_id, str) else None
        if not content and tool_calls is None and tool_call_id is None:
            continue
        entry: dict = {"role": role, "content": content}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        history.append(entry)
    if not history:
        history.append({"role": "user", "content": ""})

    model_key = str(payload.get("model") or "")
    row = _catalog_row(channel_id, model_key)
    meta = FALLBACK_META.get(model_key, {})
    is_reasoning = bool(meta.get("thinking"))
    context_window = (row or {}).get("cw") or meta.get("cw") or 200_000
    rid = str(uuid.uuid4())

    parameters: dict = {}
    if payload.get("max_completion_tokens") or payload.get("max_tokens"):
        parameters["max_tokens"] = int(
            payload.get("max_completion_tokens") or payload.get("max_tokens"))
    effort = payload.get("reasoning_effort")
    if effort:
        parameters["reasoning_effort"] = str(effort)
        parameters["enable_thinking"] = effort != "none"
    parameters["context_length"] = context_window

    tools = payload.get("tools")
    return {
        "request_id": rid,
        "request_set_id": rid,
        "chat_record_id": rid,
        "session_id": str(uuid.uuid4()),
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "text": history[-1]["content"],
            "features": [],
            "extra": {
                "context": [],
                "modelConfig": {"key": model_key, "is_reasoning": is_reasoning},
                "originalContent": history[-1]["content"],
            },
            "chatPrompt": "",
            "imageUrls": None,
        },
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "agent_id": "agent_common",
        "task_id": "common",
        "session_type": "qodercli",
        "aliyun_user_type": "",
        "model_config": {
            "key": model_key,
            "display_name": (row or {}).get("name", ""),
            "model": "",
            "format": "openai",
            "is_vl": bool(meta.get("image", False)),
            "is_reasoning": is_reasoning,
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": context_window,
        },
        "custom_model": None,
        "system": [{"type": "text", "text": system_text}] if system_text else [],
        "messages": history,
        # ⚠️ 顶层 tools，无工具时空数组（与官方客户端一致）
        "tools": tools if isinstance(tools, list) else [],
        "parameters": parameters,
        # ⚠️ business 必填：缺了服务端路由到故障节点（xdpool 实测）
        "business": {"type": "agent"},
    }


def _classify_inner(inner: str) -> str:
    """qoder-envelope classifyInner：chunk / error / heartbeat。"""
    trimmed = inner.strip()
    if not trimmed:
        return "heartbeat"
    if "[DONE]" in trimmed:
        return "chunk"
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return "error"
    if parsed is None or not isinstance(parsed, dict):
        return "heartbeat"
    if isinstance(parsed.get("choices"), list) or "usage" in parsed:
        return "chunk"
    if any(k in parsed for k in ("code", "message", "error", "statusCodeValue", "type")):
        return "error"
    return "heartbeat"


def _envelope_body(payload: str) -> str | None:
    """data: 行 → inner 文本（信封剥壳）；非信封返回 None 表示原样透传。"""
    try:
        envelope = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or "body" not in envelope:
        return None
    body = envelope["body"]
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)


def _unwrap_envelope(line: str) -> str | None:
    """data: 行 → inner 文本；非信封原样返回（None=跳过 heartbeat）。"""
    payload = line.strip()
    if payload == "[DONE]":
        return "data: [DONE]"
    inner = _envelope_body(payload)
    if inner is None:
        return f"data: {payload}"
    kind = _classify_inner(inner)
    if kind == "heartbeat":
        return None
    return f"data: {inner}"


class QoderClient:
    """Qoder 泳道客户端：目录查静态表；推理走 wasm COSY 加密。"""

    def __init__(self, doc: Document):
        self.doc = doc

    def models(self) -> list[dict]:
        rows = CATALOGS.get(self.doc.channel_id) or CATALOGS["qoder"]
        return [{"id": r["id"], "name": r["name"],
                 "credit_multiplier": r["cm"], "context_window": r["cw"]}
                for r in rows]

    # -- 加密推理 ----------------------------------------------------------

    def _machine_id(self) -> str:
        mid = (self.doc.raw or {}).get("qoderMachineId")
        return mid if isinstance(mid, str) and mid else str(uuid.uuid4())

    def prepare(self, payload: dict) -> dict:
        """明文 payload → wasm 加密的 {url, headers, body}。"""
        from ._qoder_wasm import QoderWasmError, get_wasm
        try:
            wasm = get_wasm()
        except QoderWasmError as exc:
            raise self._unsupported(str(exc)) from exc
        channel = self.doc.channel_id or "qoder"
        host = HOSTS.get(channel, HOSTS["qoder"])
        fields = wasm.generate_runtime_auth_fields(json.dumps({
            "uid": self.doc.uid,
            "security_oauth_token": self.doc.token,
            "organization_id": "",
            "organization_tags": [],
            "data_policy_agreed": False,
        }))
        user_info = json.dumps({
            "uid": self.doc.uid,
            "encrypt_user_info": fields.get("encrypt_user_info", ""),
            "key": fields.get("key", ""),
            "organization_id": "",
            "organization_tags": [],
            "data_policy_agreed": False,
        })
        ctx = wasm.context_new(self._machine_id(), COSY_VERSION, user_info,
                               json.dumps(CLIENT_METADATA))
        body = json.dumps(build_infer_payload(payload, channel))
        return wasm.prepare_infer_request(ctx, host, body,
                                          str(payload.get("model") or ""),
                                          "system")

    def chat(self, payload: dict, **_kw):
        import requests

        from .upstream import STREAM_TIMEOUT, classify_business_error

        req = self.prepare(payload)
        headers = {str(k): str(v) for k, v in (req.get("headers") or {}).items()}
        headers.setdefault("Accept", "text/event-stream")
        try:
            resp = requests.post(req["url"], headers=headers,
                                 data=req["body"].encode("utf-8"),
                                 timeout=STREAM_TIMEOUT, stream=True)
        except requests.RequestException as exc:
            from .upstream import UpstreamError
            raise UpstreamError(502, "upstream_transport_error",
                                "Qoder 对话连接中断（结果与消耗未知，网关会换号重试）。"
                                ) from exc
        if not resp.ok:
            body = resp.text[:65536]
            resp.close()
            raise classify_business_error(resp.status_code, body)
        from .qoder_stream import _EncryptedShapeResponse
        return _EncryptedShapeResponse(resp, str(payload.get("model") or ""))

    def refresh(self):
        # 渠道未声明 refresh 能力，续期 worker 不会调；防御性兜底
        raise self._unsupported("Qoder 凭据续期未接入。")

    @staticmethod
    def _unsupported(reason: str):
        from .upstream import UpstreamError
        return UpstreamError(
            501, "qoder_infer_unsupported",
            f"Qoder 加密推理不可用：{reason} "
            "（需 pip install wasmtime，armv7 设备暂不支持）。")
