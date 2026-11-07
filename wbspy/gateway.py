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


def _payload_has_image(payload: dict) -> bool:
    """消息里是否携带图片 part（判定逻辑与 strip_images 同源）。"""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return False
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and (p.get("type") in ("image_url", "image")
                                            or isinstance(p.get("image_url"), (str, dict))):
                    return True
    return False


def capability_guard(rows: list | None, model_id: str, raw: dict) -> None:
    """池泳道请求前的能力闸（语义对齐 dsh-xdpool 的 ChannelRequestError）。

    目录**明说**做不了的事就明 400——带图打纯文本模型、发不支持的推理
    档位，都直接给客户端人话错误，而不是把请求原样扔给上游收 11128 /
    11133 谜语（xdpool：「502 读起来像上游宕机，真正被拒的是用户发的图」）。
    目录没数据（行不存在 / 字段缺省）一律放行——未知不编造。
    """
    row = None
    for r in rows or []:
        if isinstance(r, dict) and r.get("id") == model_id:
            row = r
            break
    if row is None:
        return
    effort = raw.get("reasoning_effort")
    efforts = row.get("supported_efforts")
    if (isinstance(effort, str) and effort
            and isinstance(efforts, list) and efforts
            and effort not in efforts):
        raise UpstreamError(
            400, "effort_unsupported",
            f"模型 {model_id.split(':', 1)[-1]} 声明支持的推理强度为 "
            f"{'、'.join(efforts)}，收到 reasoning_effort: {effort}。请换档或不传该参数。")
    if row.get("supports_images") is False and _payload_has_image(raw):
        raise UpstreamError(
            400, "model_vision_unsupported",
            f"模型 {model_id.split(':', 1)[-1]} 不支持图片输入，但请求携带了图片。"
            "请改用视觉模型或不传图。")


def strip_images(payload: dict) -> tuple[dict, int]:
    """视觉透传关闭时剥掉消息里的图片 part，换文字占位（对齐原版
    `[image unavailable]` 思路——宁可明说图没了，不发假占位骗模型）。

    返回 (新 payload, 剥掉的图数)；content 为字符串或无图的消息零改动。
    """
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload, 0
    out: list = []
    dropped = 0
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            out.append(m)
            continue
        parts: list = []
        changed = False
        for p in c:
            if isinstance(p, dict) and (p.get("type") in ("image_url", "image")
                                        or isinstance(p.get("image_url"), (str, dict))):
                dropped += 1
                changed = True
                parts.append({"type": "text", "text": "（图片已省略：网关视觉透传已关闭）"})
            else:
                parts.append(p)
        out.append({**m, "content": parts} if changed else m)
    return ({**payload, "messages": out}, dropped) if dropped else (payload, 0)


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
    # 聚合渠道模型注入开关（config gateway.aggregate，网页端点热更新直接改这里）：
    # 关闭后 /v1/models 只暴露账号池模型，目录清爽
    app.state.gw_aggregate = bool(
        (load_config().get("gateway") or {}).get("aggregate", True))
    # 视觉透传开关（config gateway.vision，默认开）：开启时 /v1/models 目录行
    # 带视觉能力声明（input: ["text","image"]，对齐原版 channel-pack 网关出站
    # 约定，客户端据此放行传图），消息里的图片 part 原样透传上游；关闭时
    # 目录不声明、图片 part 剥成文字占位（上游零图片流量）。
    app.state.gw_vision = bool(
        (load_config().get("gateway") or {}).get("vision", True))
    # 网关运行日志（与自动化共用网页日志抽屉 /api/logs）
    app.state.auto_log: list[str] = []
    app.state.auto_total = 0

    def _gw_log(msg: str) -> None:
        app.state.auto_log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        del app.state.auto_log[:-200]
        app.state.auto_total += 1
    app.state.gw_log = _gw_log

    # -- 客户端调试日志：每个 /v1/* 请求的来龙去脉（环形缓冲 300 条）----------
    app.state.req_log: list[dict] = []
    app.state.gw_debug = bool((load_config().get("gateway") or {}).get("debug", True))

    def _dbg(entry: dict) -> None:
        """调试模式关闭时只记 4xx/5xx（排障底线），打开时全量记录。"""
        if not app.state.gw_debug and entry.get("status", 0) < 400:
            return
        entry = {"t": time.strftime("%H:%M:%S"), **entry}
        app.state.req_log.append(entry)
        del app.state.req_log[:-300]
    app.state.gw_dbg = _dbg

    def _client_ip(request) -> str:
        return (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "?"))

    def _mask(presented: str) -> str:
        """密钥脱敏：只留前 8 后 4，中间打码（能认出是哪个 key，又不泄密）。"""
        s = (presented or "").strip()
        if not s:
            return "（未带密钥）"
        if len(s) <= 14:
            return s[:4] + "****"
        return s[:8] + "****" + s[-4:]

    async def _body_digest(request) -> str:
        """请求体摘要（不存全文）：model、消息条数、最后一条消息预览、字节数。"""
        try:
            raw = await request.body()
        except Exception:
            return "（body 读取失败）"
        if not raw:
            return "（无 body）"
        try:
            j = json.loads(raw)
        except ValueError:
            return f"非 JSON：{raw[:120]!r}（{len(raw)}B）"
        if not isinstance(j, dict):
            return f"JSON {type(j).__name__}（{len(raw)}B）"
        msgs = j.get("messages")
        n = len(msgs) if isinstance(msgs, list) else 0
        last = ""
        if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
            last = str(msgs[-1].get("content"))[:140]
        return (f"model={j.get('model')} messages={n} stream={j.get('stream')} "
                f"last={last!r}（{len(raw)}B）")

    # 调试日志管理端点（本机管理界面，同 /api/* 不设网关密钥）
    @app.get("/api/debug-logs")
    def debug_logs() -> dict:
        return {"ok": True, "data": {"on": app.state.gw_debug,
                                     "logs": list(reversed(app.state.req_log[-300:]))}}

    @app.post("/api/debug-logs")
    async def debug_logs_toggle(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "请求须为 JSON 对象。")
        on = bool(body.get("on"))
        app.state.gw_debug = on
        from .store import save_config
        cfg = load_config()
        cfg.setdefault("gateway", {})["debug"] = on
        save_config(cfg)
        _gw_log(f"调试日志已{'开启' if on else '关闭'}（配置已落盘）")
        return {"ok": True, "data": {"on": on}}

    @app.delete("/api/debug-logs")
    def debug_logs_clear() -> dict:
        app.state.req_log.clear()
        return {"ok": True}

    # 统一鉴权中间件：只保护 /v1/*（对外 OpenAI 服务）；/api/* 是本机管理
    # 界面，不被网关密钥锁死（公网防护由 _public_guard / 反向代理负责）。
    # 接受 Authorization: Bearer <key> 或 X-Wbspy-Key。
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        if not app.state.gw_enabled and path.startswith("/v1"):
            _dbg({"ip": _client_ip(request), "method": request.method, "path": path,
                  "status": 404, "auth": "（未带密钥）", "note": "网关已关闭（gateway_disabled）"})
            return JSONResponse(status_code=404,
                                content={"error": {"message": "gateway is disabled",
                                                   "type": "gateway_disabled"}})
        key = app.state.gw_key
        if key and path.startswith("/v1"):
            presented = request.headers.get("authorization", "") or \
                        request.headers.get("x-wbspy-key", "")
            if presented not in (f"Bearer {key}", key):
                _dbg({"ip": _client_ip(request), "method": request.method, "path": path,
                      "status": 401, "auth": _mask(presented),
                      "ua": request.headers.get("user-agent", "")[:120],
                      "body": await _body_digest(request),
                      "note": "✗ 密钥无效：客户端发的密钥与网关设置的不一致"
                              f"（本网关密钥长度 {len(key)}，对方发来长度 {len(presented.strip())}）"})
                return JSONResponse(status_code=401,
                                    content={"error": {"message": "invalid api key",
                                                       "type": "invalid_api_key"}})
            # 密钥正确 → 记录请求与响应全貌（调试模式关闭则只记账不落盘）。
            # body 摘要必须在 call_next 之前取：流会被下游消费掉。
            digest = await _body_digest(request)
            t0 = time.time()
            try:
                response = await call_next(request)
            except Exception as exc:
                # 路由内未捕获异常：调试日志必须留痕（否则 500 无从排查）
                note = f"✗ 网关内部异常：{type(exc).__name__}: {exc}"[:200]
                _dbg({"ip": _client_ip(request), "method": request.method, "path": path,
                      "status": 500, "ms": int((time.time() - t0) * 1000),
                      "auth": _mask(presented),
                      "ua": request.headers.get("user-agent", "")[:120],
                      "body": digest, "note": note})
                _gw_log(f"网关内部异常 {path}：{note}")
                return JSONResponse(status_code=500,
                                    content={"error": {"message": "网关内部错误，详情见网页日志。",
                                                       "type": "internal_error"}})
            _dbg({"ip": _client_ip(request), "method": request.method, "path": path,
                  "status": response.status_code,
                  "ms": int((time.time() - t0) * 1000),
                  "auth": _mask(presented),
                  "ua": request.headers.get("user-agent", "")[:120],
                  "body": digest})
            return response
        try:
            return await call_next(request)
        except Exception as exc:                          # 未设密钥的 /v1 与 /api/* 同样兜底
            note = f"✗ 网关内部异常：{type(exc).__name__}: {exc}"[:200]
            _dbg({"ip": _client_ip(request), "method": request.method, "path": path,
                  "status": 500, "auth": "（未带密钥）", "note": note})
            _gw_log(f"网关内部异常 {path}：{note}")
            return JSONResponse(status_code=500,
                                content={"error": {"message": "网关内部错误，详情见网页日志。",
                                                   "type": "internal_error"}})

    # 鉴权统一由上方 auth_middleware 处理（Bearer 或 X-Wbspy-Key）。

    def _error(exc: UpstreamError) -> JSONResponse:
        # 上游账号凭据失效类错误绝不以 401/「密钥/凭据」字样透传——那是账号池
        # 内部的问题（自动换号已失败），客户端只会误以为自己的中转密钥坏了。
        if exc.kind == "auth":
            exc = UpstreamError(503, "upstream_account_unavailable",
                                "上游账号暂不可用（网关已自动换号仍未成功），请稍后重试。",
                                retry_after=60)
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
                if isinstance(cm, (int, float)) and not isinstance(cm, bool) and cm == 0:
                    return f"{base}（限时免费）"
                if isinstance(cm, (int, float)) and not isinstance(cm, bool):
                    return f"{base} {cm:.2f}x"
                return base

            def _cm_field(m: dict) -> dict:
                """倍率字段只认数值：上游给的脏字符串不进目录（防客户端再被坑）。"""
                v = m.get("credit_multiplier")
                return {"credit_multiplier": v} \
                    if isinstance(v, (int, float)) and not isinstance(v, bool) else {}

            def _vision_field(m: dict) -> dict:
                """视觉声明只给 WorkBuddy 系行；Cline/Qoder 行模型混杂不越权
                代言。字段名对齐原版网关出站（input: ["text","image"]）。
                逐模型判断（对齐 dsh-xdpool）：目录明说 supports_images=False
                （含 disabledMultimodal 否定拼法）就不声明——上游上了纯文本
                模型时客户端不会误传图；目录没带该字段维持原版实测结论
                「全表支持」，不凭空把现有模型变纯文本。"""
                if not getattr(app.state, "gw_vision", True):
                    return {}
                mid = str(m.get("id") or "")
                if mid.startswith(("cline:", "qoder:", "qodercn:")):
                    return {}
                if m.get("supports_images") is False:
                    return {}
                return {"input": ["text", "image"]}

            def _cap_fields(m: dict) -> dict:
                """能力字段白名单透传：目录明说了才带，未知不编造。"""
                out: dict = {}
                eff = m.get("supported_efforts")
                if (isinstance(eff, list) and eff
                        and all(isinstance(x, str) for x in eff)):
                    out["supported_efforts"] = eff
                si = m.get("supports_images")
                if isinstance(si, bool):
                    out["supports_images"] = si
                return out

            try:
                pool_rows = [{"id": m["id"], "object": "model", "owned_by": "wbspy",
                              **m.get("wbspy", {}),
                              "name": _with_rate(m),
                              "region": m.get("region"),
                              **_cm_field(m), **_vision_field(m), **_cap_fields(m)}
                             for m in pool.models()]
            except Exception as exc:
                # 目录拉取失败不该 500 卡死客户端启动：用旧缓存顶上，没有则空目录
                _gw_log(f"模型目录刷新失败（沿用缓存）：{type(exc).__name__}: {exc}")
                pool_rows = None
            if pool_rows is not None:
                # 排序 key 只认数值：万一上游把倍率给成字符串，也不能 500 全池
                def _cm(r: dict):
                    v = r.get("credit_multiplier")
                    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

                pool_rows.sort(key=lambda r: (_cm(r) is None, _cm(r) or 0))
                rows = pool_rows
                # 聚合渠道只暴露「当前真的可调用」的：未配置 key 的厂商不进目录；
                # 可在网页设置里整体关掉（目录只留账号池模型）
                if app.state.gw_aggregate:
                    try:
                        rows += [{"id": m["id"], "object": "model", "owned_by": m["owned_by"],
                                  **m["wbspy"]}
                                 for m in aggregate.models() if m["wbspy"].get("configured")]
                    except Exception as exc:
                        _gw_log(f"聚合渠道目录刷新失败（忽略）：{type(exc).__name__}: {exc}")
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

        # ── 路由二：登录账号池（WorkBuddy 系 / Cline / Qoder，按前缀选泳道）──
        # 泳道内多账号轮换容错；纯模型名等价 workbuddy:<model>
        lane = "workbuddy"
        head, sep, rest = model.partition(":")
        if sep and head in ("cline", "qoder", "qodercn"):
            lane = head
            raw = {**raw, "model": rest}
        elif sep and head in ("workbuddy", "cn", "global"):
            raw = {**raw, "model": rest}
        # 视觉透传关闭 → 剥图（占位文字替代，上游零图片流量）
        if not getattr(app.state, "gw_vision", True):
            raw, _dropped = strip_images(raw)

        # 能力闸（池泳道）：目录明说做不了就明 400，不扔给上游收谜语错误
        if lane == "workbuddy":
            try:
                capability_guard(app.state.model_cache,
                                 "workbuddy:" + str(raw["model"]), raw)
            except UpstreamError as exc:
                _gw_log(f"能力闸拒绝：{model} → {exc.code}：{exc.message[:120]}")
                return _error(exc)

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
            doc, resp = pool.chat_failover(raw, session=session, lane=lane)
        except UpstreamError as exc:
            _gw_log(f"对话失败：{model} → HTTP {exc.status} {exc.code}：{exc.message[:160]}")
            return _error(exc)
        _gw_log(f"对话：{model} ← 「{doc.label()}」")

        if raw.get("stream"):
            def sse() -> Iterator[bytes]:
                try:
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    resp.close()
                    LEDGER.record(model=model, provider=lane, account=doc.uid[:12],
                                  prompt_tokens=estimate_tokens(raw.get("messages", [])))
            return StreamingResponse(sse(), media_type="text/event-stream",
                                     headers={"X-Wbspy-Account": doc.uid[:12]})

        # 上游恒为流式（payload 强制 stream:true），非流式请求聚合为完整对象
        body = _sse_aggregate(resp, model)
        resp.close()
        LEDGER.record(model=model, provider=lane, account=doc.uid[:12],
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
            return pool.client(doc).embeddings(model, [str(i) for i in inputs])
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
