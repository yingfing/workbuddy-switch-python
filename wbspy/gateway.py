"""本机 OpenAI 兼容网关（FastAPI）。

统一端点，三类模型同池路由：

* ``workbuddy:*``               — WorkBuddy 账号池（多账号轮换容错）
* ``<provider>/<model>``         — 76 家聚合渠道（按 key 路由，OpenAI 兼容透传）
* 纯模型名（无前缀）              — 兼容写法，等价 ``workbuddy:<model>``
* 兼容旧 id：``cn:*`` / ``global:*`` 仍按账号池路由（旧客户端配置不断）

端点：

* ``GET  /healthz``          健康检查 + 账号池状态
* ``GET  /v1/models``        聚合模型目录（账号池 + 聚合渠道，含配置状态）
* ``POST /v1/chat/completions``  对话（流式 SSE，自动换号/路由）
* ``POST /v1/embeddings``    向量（bge-m3 / hunyuan-embedding）

安全：默认只绑 127.0.0.1；可在配置里设 ``api_key`` 启用 Bearer 校验。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import aggregate, convert
from .context_budget import trim as trim_context
from .pool import AccountPool
from .store import load_config
from .usage import LEDGER, estimate_tokens
from .upstream import EMBEDDING_MODELS, UpstreamError

MODEL_TTL_SEC = 300


def _sse_aggregate(resp, route_model: str) -> dict:
    """上游强制流式 → 聚合 SSE 块为完整 chat.completion（对齐参考实现）。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict | None = None
    finish = ""
    model = route_model
    cid = ""
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line or not str(raw_line).startswith("data:"):
            continue
        data = str(raw_line)[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        cid = chunk.get("id") or cid
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or ch.get("message") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    body = {"id": cid or "wbspy-agg", "model": model, "object": "chat.completion",
            "created": int(time.time()),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or "stop"}],
            "usage": usage or {}}
    return body


def create_app(pool: AccountPool, api_key: str = "") -> FastAPI:
    app = FastAPI(title="wbspy gateway", docs_url=None, redoc_url=None)
    app.state.model_cache: list[dict] = []
    app.state.model_cached_at = 0.0
    app.state.gw_key = api_key                    # 网页设置端点热更新直接改这里
    # 对外网关开关（config gateway.enabled，网页端点热更新直接改这里）：
    # 关闭后 /v1/* 一律 404，/api/* 管理界面与 /healthz 不受影响
    app.state.gw_enabled = bool(
        (load_config().get("gateway") or {}).get("enabled", True))

    # 统一鉴权中间件：只保护 /v1/*（对外 OpenAI 服务）；/api/* 是本机管理
    # 界面，不被网关密钥锁死（公网防护由 _public_guard / 反向代理负责）。
    # 接受 Authorization: Bearer <key> 或 X-Wbspy-Key。
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if not app.state.gw_enabled and path.startswith("/v1"):
            return JSONResponse(status_code=404,
                                content={"error": {"message": "gateway is disabled",
                                                   "type": "gateway_disabled"}})
        key = app.state.gw_key
        if key and path.startswith("/v1"):
            presented = request.headers.get("authorization", "") or \
                        request.headers.get("x-wbspy-key", "")
            if presented not in (f"Bearer {key}", key):
                return JSONResponse(status_code=401,
                                    content={"error": {"message": "invalid api key",
                                                       "type": "invalid_api_key"}})
        return await call_next(request)

    # 鉴权统一由上方 auth_middleware 处理（Bearer 或 X-Wbspy-Key）。

    def _error(exc: UpstreamError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"message": exc.message, "type": exc.code,
                               "code": exc.code,
                               **({"retry_after": exc.retry_after} if exc.retry_after else {})}},
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "accounts": pool.status()}

    @app.get("/v1/models")
    def models(authorization: str = Header("")) -> dict:
        now = time.time()
        if now - app.state.model_cached_at > MODEL_TTL_SEC:
            # 账号池模型：id 是路由键保持不动；官方计费倍率直接拼进显示名
            # （如「快速 0.15x」「Hy4（限时免费）」），任何客户端都可见；
            # 排序按倍率升序，限时免费最前
            def _with_rate(m: dict) -> str:
                base = m.get("name") or m["id"].split(":", 1)[-1]
                cm = m.get("credit_multiplier")
                if cm == 0:
                    return f"{base}（限时免费）"
                if isinstance(cm, (int, float)):
                    return f"{base} {cm:.2f}x"
                return base

            pool_rows = [{"id": m["id"], "object": "model", "owned_by": "wbspy",
                          **m.get("wbspy", {}),
                          "name": _with_rate(m),
                          "region": m.get("region"),
                          **({"credit_multiplier": m["credit_multiplier"]}
                             if "credit_multiplier" in m else {})}
                         for m in pool.models()]
            pool_rows.sort(key=lambda r: (r.get("credit_multiplier") is None,
                                          r.get("credit_multiplier") or 0))
            rows = pool_rows
            # 聚合渠道只暴露「当前真的可调用」的：未配置 key 的厂商不进目录
            rows += [{"id": m["id"], "object": "model", "owned_by": m["owned_by"], **m["wbspy"]}
                     for m in aggregate.models() if m["wbspy"].get("configured")]
            app.state.model_cache = rows
            app.state.model_cached_at = now
        return {"object": "list", "data": app.state.model_cache}

    def _aggregate_chat(raw: dict) -> Any:
        """``<provider>/<model>`` 路由：透传到厂商上游（含协议转换与 failover）。"""
        route_model = raw["model"]
        row = aggregate.resolve(route_model)
        raw, trimmed = trim_context(raw)
        if trimmed:
            raw = {**raw, "messages": [
                {"role": "system", "content": f"[系统提示：较早的 {trimmed} 条上下文已按窗口预算截断]"},
                *raw["messages"]]}

        # anthropic-messages 厂商：协议转换后直连（无跨厂商 failover）
        if row["format"] == "anthropic-messages":
            return _anthropic_chat(row, raw, route_model)

        try:
            used_row, resp = aggregate.forward_with_failover(route_model, raw)
        except aggregate.AggregateError as exc:
            LEDGER.record(model=route_model, provider=row["provider_id"], error=exc.code)
            return _agg_error(exc)
        failover = used_row["provider_id"] != row["provider_id"]
        if raw.get("stream"):
            def sse() -> Iterator[bytes]:
                pt = estimate_tokens(raw.get("messages", []))
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    resp.close()
                    LEDGER.record(model=route_model, provider=used_row["provider_id"],
                                  prompt_tokens=pt, failover=failover)
            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"X-Wbspy-Provider": used_row["provider_id"]})
        try:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                # 上游强制流式（对齐 WorkBuddy-Switch：payload 恒 stream:true），
                # 非流式请求在此聚合为完整 chat.completion 对象。
                body = _sse_aggregate(resp, route_model)
            else:
                body = resp.json()
        finally:
            resp.close()
        LEDGER.record(model=route_model, provider=used_row["provider_id"],
                      prompt_tokens=estimate_tokens(raw.get("messages", [])),
                      completion_tokens=len(str(body.get("choices", [{}])[0]
                                               .get("message", {}).get("content") or "")) // 3,
                      failover=failover)
        return JSONResponse(body)

    def _anthropic_chat(row: dict, raw: dict, route_model: str) -> Any:
        import requests as _rq
        body = convert.to_anthropic_request(raw)
        entry = aggregate.load_keys().get(row["provider_id"], {})
        api_key = entry.get("api_key", "")
        if row["auth_kind"] == "apiKey" and not api_key:
            return _agg_error(aggregate.AggregateError(
                401, "key_required", f"厂商 {row['provider']} 尚未配置 API Key。"))
        base = row["base_url"].rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        try:
            resp = _rq.post(url, headers=headers, json=body, timeout=(15, 600), stream=True)
        except _rq.RequestException as exc:
            LEDGER.record(model=route_model, provider=row["provider_id"], error="transport")
            return _agg_error(aggregate.AggregateError(502, "upstream_transport_error", str(exc)))
        if not resp.ok:
            detail = resp.text[:400]; status = resp.status_code; resp.close()
            LEDGER.record(model=route_model, provider=row["provider_id"], error="upstream")
            return _agg_error(aggregate.AggregateError(
                502 if status >= 500 else status, "upstream_error",
                f"厂商 {row['provider']} 返回 HTTP {status}：{detail}"))
        if body.get("stream"):
            def sse() -> Iterator[bytes]:
                try:
                    yield from convert.anthropic_sse_to_openai(
                        resp.iter_content(chunk_size=None), route_model)
                finally:
                    resp.close()
                    LEDGER.record(model=route_model, provider=row["provider_id"],
                                  prompt_tokens=estimate_tokens(raw.get("messages", [])))
            return StreamingResponse(sse(), media_type="text/event-stream")
        try:
            out = convert.from_anthropic_response(resp.json(), route_model)
        finally:
            resp.close()
        LEDGER.record(model=route_model, provider=row["provider_id"],
                      prompt_tokens=estimate_tokens(raw.get("messages", [])))
        return JSONResponse(out)

    def _agg_error(exc: aggregate.AggregateError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"message": exc.message, "type": exc.code, "code": exc.code}})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: str = Header("")) -> Any:
        try:
            raw: dict[str, Any] = await request.json()
        except ValueError:
            raise HTTPException(400, "请求须为 JSON 对象。")
        if not isinstance(raw.get("model"), str) or not raw.get("model"):
            raise HTTPException(400, "model 不能为空。")
        model = raw["model"]

        # ── 路由一：聚合渠道 <provider>/<model> ─────────────────────────────
        if "/" in model and not model.startswith(("workbuddy:", "cn:", "global:")):
            return _aggregate_chat(raw)

        # ── 路由二：WorkBuddy 账号池 ────────────────────────────────────────
        # 纯模型名等价 workbuddy:<model>；区域/命名前缀剥掉后由池按各账号可用性调度
        if model.split(":", 1)[0] in ("workbuddy", "cn", "global") and ":" in model:
            raw = {**raw, "model": model.split(":", 1)[1]}
        raw, _trimmed = trim_context(raw)

        # 会话键：客户端可显式传 x-wbspy-session；缺省用「system+首条消息」
        # 指纹——同一段对话续发时前缀稳定，粘住同一账号保护上游 prompt 缓存
        session = request.headers.get("x-wbspy-session") or None
        if not session:
            msgs = raw.get("messages")
            if isinstance(msgs, list) and msgs:
                import hashlib
                seed = json.dumps(msgs[:2], ensure_ascii=False, default=str)[:2000]
                session = "auto:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

        try:
            doc, resp = pool.chat_failover(raw, session=session)
        except UpstreamError as exc:
            return _error(exc)

        if raw.get("stream"):
            def sse() -> Iterator[bytes]:
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    resp.close()
                    LEDGER.record(model=model, provider="workbuddy", account=doc.uid[:12],
                                  prompt_tokens=estimate_tokens(raw.get("messages", [])))
            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"X-Wbspy-Account": doc.uid[:12]})

        # 上游恒为流式（payload 强制 stream:true），非流式请求聚合为完整对象
        body = _sse_aggregate(resp, model)
        resp.close()
        LEDGER.record(model=model, provider="workbuddy", account=doc.uid[:12],
                      prompt_tokens=estimate_tokens(raw.get("messages", [])),
                      completion_tokens=len(str(body.get("choices", [{}])[0]
                                               .get("message", {}).get("content") or "")) // 3)
        return JSONResponse(body, headers={"X-Wbspy-Account": doc.uid[:12]})

    @app.post("/v1/embeddings")
    async def embeddings(request: Request, authorization: str = Header("")) -> Any:
        try:
            raw = await request.json()
        except ValueError:
            raise HTTPException(400, "请求须为 JSON 对象。")
        model = raw.get("model", "")
        if isinstance(model, str) and model.split(":", 1)[0] == "global" and ":" in model:
            raise HTTPException(400, "unsupported_region",
                                "向量模型目前仅国内版可用，不接受 global: 前缀。")
        if isinstance(model, str) and model.split(":", 1)[0] in ("workbuddy", "cn") \
                and ":" in model:
            raw = {**raw, "model": model.split(":", 1)[1]}   # 剥命名前缀再路由
        inputs = raw.get("input", [])
        inputs = [inputs] if isinstance(inputs, str) else list(inputs)
        doc = pool.pick(model)
        if doc is None:
            raise HTTPException(503, "账号池中没有可用账号。")
        try:
            return pool.clients[doc.uid].embeddings(model, [str(i) for i in inputs])
        except UpstreamError as exc:
            return _error(exc)

    @app.get("/v1/embedding-models")
    def embedding_models(authorization: str = Header("")) -> dict:
        return {"data": list(EMBEDDING_MODELS)}

    return app


def run_gateway(pool: AccountPool, host: str = "127.0.0.1", port: int = 8178,
                api_key: str = "") -> None:
    import uvicorn
    uvicorn.run(create_app(pool, api_key), host=host, port=port, log_level="warning")
