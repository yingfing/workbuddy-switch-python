"""本机 OpenAI 兼容网关（FastAPI）。

统一端点，三类模型同池路由：

* ``cn:*`` / ``global:*``        — WorkBuddy 账号池（多账号轮换容错）
* ``<provider>/<model>``         — 76 家聚合渠道（按 key 路由，OpenAI 兼容透传）
* 纯模型名（无前缀）              — 兼容写法，等价 ``cn:<model>``

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
from .usage import LEDGER, estimate_tokens
from .upstream import EMBEDDING_MODELS, UpstreamError

MODEL_TTL_SEC = 300


def create_app(pool: AccountPool, api_key: str = "") -> FastAPI:
    app = FastAPI(title="wbspy gateway", docs_url=None, redoc_url=None)
    app.state.model_cache: list[dict] = []
    app.state.model_cached_at = 0.0

    # 统一鉴权中间件：/v1/*（OpenAI）与 /api/*（管理）都受保护，
    # / 与 /healthz 豁免。接受 Authorization: Bearer <key> 或 X-Wbspy-Key。
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if api_key and (path.startswith("/v1") or path.startswith("/api")):
            presented = request.headers.get("authorization", "") or \
                        request.headers.get("x-wbspy-key", "")
            if presented not in (f"Bearer {api_key}", api_key):
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
            rows = [{"id": m["id"], "object": "model", "owned_by": "wbspy", **m.get("wbspy", {})}
                    for m in pool.models()]
            rows += [{"id": m["id"], "object": "model", "owned_by": m["owned_by"], **m["wbspy"]}
                     for m in aggregate.models()]
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
        if "/" in model and not model.startswith(("cn:", "global:")):
            return _aggregate_chat(raw)

        # ── 路由二：WorkBuddy 账号池 ────────────────────────────────────────
        # 纯模型名等价 cn:<model>；区域前缀剥掉后由池按各账号可用性调度
        if model.split(":", 1)[0] in ("cn", "global") and len(model) > 2:
            raw = {**raw, "model": model.split(":", 1)[1]}
        raw, _trimmed = trim_context(raw)

        try:
            doc, resp = pool.chat_failover(raw)
        except UpstreamError as exc:
            return _error(exc)

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

    @app.post("/v1/embeddings")
    async def embeddings(request: Request, authorization: str = Header("")) -> Any:
        try:
            raw = await request.json()
        except ValueError:
            raise HTTPException(400, "请求须为 JSON 对象。")
        model = raw.get("model", "")
        if isinstance(model, str) and model.startswith("global:"):
            raise HTTPException(400, "unsupported_region",
                                "向量模型目前仅国内版可用，不接受 global: 前缀。")
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
