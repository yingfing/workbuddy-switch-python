"""ZCode（智谱）客户端：Anthropic messages 线的 OpenAI 双向转码。

协议来源：dsh-xdpool vendor/channel-pack pack.js 的 zcode-upstream.ts /
zcode-transport.ts 段（全部为线上实测结论），要点：

- **两条对话线**，按模型分流：
  - start-plan（免费线）：POST zcode.z.ai/api/v1/zcode-plan/anthropic/v1/messages，
    Bearer zcode_jwt，模型 glm-5.3-flash / glm-5.2 / glm-5-turbo；
  - coding-plan（订阅线）：POST api.z.ai/api/anthropic/v1/messages，
    Bearer coding_plan_key（由第三方 OAuth token 经 biz 接口换取），
    官方白名单只确认 glm-5.3 / glm-5.3-flash。
- **headers 指纹**：UA `ZCode/<ver>` + X-Device-Mid + X-Platform: win32 +
  anthropic-version: 2023-06-01；coding-plan 线必须删除 HTTP-Referer。
- **请求体恒 stream:true**，SSE 事件流经本模块实时转码为 OpenAI chunk——
  网关的流式透传与非流式聚合两条路径因此零改动。
"""

from __future__ import annotations

import json
import time
import uuid

import requests

from .upstream import STREAM_TIMEOUT, TIMEOUT, UpstreamError

ORIGIN = "https://zcode.z.ai"
BIZ_ORIGIN = "https://api.z.ai"
MESSAGES_URL = f"{ORIGIN}/api/v1/zcode-plan/anthropic/v1/messages"
CODING_MESSAGES_URL = f"{BIZ_ORIGIN}/api/anthropic/v1/messages"
BALANCE_URL = f"{ORIGIN}/api/v1/zcode-plan/billing/balance"

# 模型白名单（pack.js CHANNEL_MODELS：只列官方 builtinModelIds 确认过的）
START_PLAN_MODELS = ["glm-5.3-flash", "glm-5.2", "glm-5-turbo"]
CODING_PLAN_MODELS = ["glm-5.3", "glm-5.3-flash"]
DEFAULT_MAX_TOKENS = 8192

_STOP_REASON = {"end_turn": "stop", "stop_sequence": "stop",
                "max_tokens": "length", "tool_use": "tool_calls",
                "refusal": "content_filter"}


def _headers(jwt: str, device_mid: str, app_version: str = "3.14.3") -> dict:
    """pack.js buildZcodeHeaders：逐头对齐，指纹缺一个都可能 403。"""
    return {"User-Agent": f"ZCode/{app_version}",
            "HTTP-Referer": ORIGIN,
            "X-ZCode-App-Version": app_version,
            "X-Release-Channel": "stable",
            "X-Client-Language": "zh-CN",
            "X-Client-Timezone": "Asia/Shanghai",
            "X-Device-Mid": device_mid or str(uuid.uuid4()),
            "X-Platform": "win32",
            "X-Os-Category": "windows",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"}


# ── OpenAI 请求 → Anthropic 请求 ─────────────────────────────────────────────

def _content_blocks(content) -> list[dict]:
    """OpenAI content（str / part 数组）→ Anthropic content blocks。

    text 原样；image_url 的 data URL 拆出 base64；远程 URL 用 source.url
    （上游不支持时会明确报错，比我们瞎猜强）。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and isinstance(part.get("text"), str):
            blocks.append({"type": "text", "text": part["text"]})
        elif ptype == "image_url":
            url = (part.get("image_url") or {})
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and url.startswith("data:"):
                head, _, b64 = url.partition(",")
                media = head[5:].split(";", 1)[0] or "image/png"
                blocks.append({"type": "image",
                               "source": {"type": "base64",
                                          "media_type": media, "data": b64}})
            elif isinstance(url, str) and url.startswith("http"):
                blocks.append({"type": "image",
                               "source": {"type": "url", "url": url}})
    return blocks


def to_anthropic_body(payload: dict, model: str) -> dict:
    """OpenAI chat payload → Anthropic messages body（恒流式，对齐 pack）。"""
    system_parts: list[str] = []
    messages: list[dict] = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts += [p.get("text", "") for p in _content_blocks(content)
                                 if p.get("type") == "text"]
            continue
        if role not in ("user", "assistant") or content is None:
            continue
        messages.append({"role": role, "content": _content_blocks(content)})
    body: dict = {"model": model, "max_tokens": DEFAULT_MAX_TOKENS,
                  "messages": messages, "stream": True}
    if system_parts:
        body["system"] = "\n\n".join(p for p in system_parts if p)
    for k_src, k_dst in (("max_tokens", "max_tokens"),
                         ("max_completion_tokens", "max_tokens"),
                         ("temperature", "temperature")):
        v = payload.get(k_src)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            body[k_dst] = v
    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        body["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        body["stop_sequences"] = [s for s in stop if isinstance(s, str) and s]
    return body


# ── Anthropic SSE → OpenAI chunk 实时转码 ────────────────────────────────────

class _OpenAIShapeResponse:
    """把 Anthropic SSE 上游响应伪装成 OpenAI SSE 的 requests.Response。

    网关两条消费路径都认它：流式透传走 iter_content（字节），非流式聚合走
    iter_lines（文本，_sse_aggregate 解析 OpenAI chunk）——形态对了就零改动。
    """

    def __init__(self, upstream: requests.Response, model: str):
        self._up = upstream
        self.status_code = 200
        self.ok = True
        self.headers = {"content-type": "text/event-stream"}
        self._model = model
        self._cid = f"chatcmpl-zcode-{uuid.uuid4().hex[:12]}"

    def close(self) -> None:
        self._up.close()

    @property
    def text(self) -> str:
        return ""

    def json(self) -> dict:
        raise ValueError("ZCode 上游恒为流式，请走 SSE 消费路径。")

    def _chunks(self):
        """逐行消费 Anthropic SSE，产出 OpenAI chunk 行（含结尾 [DONE]）。"""
        usage_in: dict = {}
        usage_out: dict = {}
        stop_reason = ""
        started = False
        for raw in self._up.iter_lines(decode_unicode=True):
            if not raw or not str(raw).startswith("data:"):
                continue
            data = str(raw)[5:].strip()
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            etype = ev.get("type")
            if etype == "message_start":
                msg = ev.get("message") or {}
                self._cid = msg.get("id") or self._cid
                self._model = msg.get("model") or self._model
                u = msg.get("usage") or {}
                if isinstance(u.get("input_tokens"), (int, float)):
                    usage_in["prompt_tokens"] = u["input_tokens"]
                started = True
                yield self._chunk({"role": "assistant", "content": ""})
            elif etype == "content_block_delta":
                delta = ev.get("delta") or {}
                dkind = delta.get("type")
                if dkind == "text_delta" and delta.get("text"):
                    yield self._chunk({"content": delta["text"]})
                elif dkind == "thinking_delta" and delta.get("thinking"):
                    yield self._chunk({"reasoning_content": delta["thinking"]})
            elif etype == "message_delta":
                d = ev.get("delta") or {}
                if d.get("stop_reason"):
                    stop_reason = _STOP_REASON.get(d["stop_reason"], "stop")
                u = ev.get("usage") or {}
                if isinstance(u.get("output_tokens"), (int, float)):
                    usage_out["completion_tokens"] = u["output_tokens"]
            elif etype == "error":
                err = ev.get("error") or {}
                note = str(err.get("message") or "上游错误")[:200]
                yield self._chunk({"content": f"\n\n[ZCode 上游错误] {note}"})
                stop_reason = stop_reason or "stop"
            elif etype == "message_stop":
                break
        if not started:
            # 上游一个事件都没给就断流：给客户端一个可读的空回复而不是静默
            yield self._chunk({"content": ""})
        final = {"finish_reason": stop_reason or "stop"}
        if usage_in or usage_out:
            final["usage"] = {**usage_in, **usage_out,
                              "total_tokens": (usage_in.get("prompt_tokens") or 0)
                              + (usage_out.get("completion_tokens") or 0)}
        yield self._chunk(final, empty_delta=True)
        yield "data: [DONE]"

    def _chunk(self, delta: dict, empty_delta: bool = False) -> str:
        body: dict = {"id": self._cid, "model": self._model,
                      "object": "chat.completion.chunk", "created": int(time.time()),
                      "choices": [{"index": 0,
                                   "delta": {} if empty_delta else delta}]}
        if "usage" in delta:
            body["usage"] = delta["usage"]
            body["choices"][0]["finish_reason"] = delta.get("finish_reason")
        return "data: " + json.dumps(body, ensure_ascii=False)

    def iter_lines(self, decode_unicode: bool = True):
        return self._chunks()

    def iter_content(self, chunk_size: int | None = None):
        for line in self._chunks():
            yield (line + "\n\n").encode()


# ── 客户端 ────────────────────────────────────────────────────────────────────

class ZCodeClient:
    """ZCode 账号客户端：start-plan 免费线开箱即用，coding-plan 需 plan key。"""

    def __init__(self, doc):
        self.doc = doc
        self.http = requests.Session()
        self.http.trust_env = False

    # -- 凭据 ---------------------------------------------------------------

    def _meta(self) -> dict:
        meta = self.doc.raw.get("zcode")
        return meta if isinstance(meta, dict) else {}

    def _jwt(self) -> str:
        if not self.doc.token:
            raise UpstreamError(401, "credential_required", "请先完成 ZCode 网页登录授权。")
        return self.doc.token

    def _coding_plan_key(self) -> str:
        """订阅线凭据：账号文档缓存 → 用第三方 OAuth token 现换（协议见
        fetchCodingPlanApiKey：getCustomerInfo → 默认机构/项目 → 复制
        zcode-api-key）。换取失败报人话错误并引导用免费线模型。"""
        meta = self._meta()
        key = meta.get("coding_plan_key_zai") or meta.get("coding_plan_key_bigmodel")
        if isinstance(key, str) and key:
            return key
        third = meta.get("third_party_access_token")
        if not isinstance(third, str) or not third:
            raise UpstreamError(400, "coding_plan_key_required",
                                "该模型走 coding-plan 订阅线，但账号还没有换取 plan key；"
                                "可先用 glm-5.3-flash / glm-5.2 / glm-5-turbo（免费线）。")
        got = self._fetch_coding_plan_key(third)
        if not got:
            raise UpstreamError(400, "coding_plan_key_required",
                                "coding-plan key 自动换取失败（可能未订阅）；"
                                "可先用 glm-5.3-flash / glm-5.2 / glm-5-turbo（免费线）。")
        from .store import save_account
        meta["coding_plan_key_" + ("zai" if meta.get("third_party_kind") == "zai"
                                   else "bigmodel")] = got
        save_account(self.doc)
        return got

    def _fetch_coding_plan_key(self, oauth_token: str) -> str:
        h = {"Authorization": f"Bearer {oauth_token}",
             "Content-Type": "application/json",
             "User-Agent": "ZCode/3.14.3", "X-Platform": "win32",
             "X-Device-Mid": self._meta().get("device_mid") or ""}
        try:
            info = self.http.get(f"{BIZ_ORIGIN}/api/biz/customer/getCustomerInfo",
                                 headers=h, timeout=TIMEOUT, allow_redirects=False)
            orgs = (((info.json().get("data") or {}).get("organizations")) or []) \
                if info.ok else []
            picked = None
            for o in orgs:
                if not isinstance(o, dict):
                    continue
                projects = [p for p in (o.get("projects") or [])
                            if isinstance(p, dict)
                            and str(p.get("projectType") or "") != "2"]
                if o.get("organizationId") and projects:
                    picked = (o["organizationId"], projects)
                    if "默认机构" in str(o.get("organizationName") or ""):
                        break
            if not picked:
                return ""
            org, projects = picked
            proj = next((p["projectId"] for p in projects
                         if "默认项目" in str(p.get("projectName") or "")),
                        projects[0].get("projectId"))
            if not org or not proj:
                return ""
            list_url = (f"{BIZ_ORIGIN}/api/biz/v1/organization/{org}"
                        f"/projects/{proj}/api_keys")
            listing = self.http.get(list_url, headers=h, timeout=TIMEOUT,
                                    allow_redirects=False)
            entries = listing.json() if listing.ok else []
            if not isinstance(entries, list):
                return ""
            api_key = next((e.get("apiKey") for e in entries
                            if isinstance(e, dict) and e.get("name") == "zcode-api-key"
                            and isinstance(e.get("apiKey"), str)), "")
            if not api_key:
                return ""
            copied = self.http.get(f"{list_url}/copy/{api_key}", headers=h,
                                   timeout=TIMEOUT, allow_redirects=False)
            return api_key if copied.ok else ""
        except (requests.RequestException, ValueError):
            return ""

    # -- 目录 / 对话 / 余额 ---------------------------------------------------

    def models(self) -> list[dict]:
        """静态目录（pack 白名单）：id 不带 lane 前缀（网关统一拼 zcode:）。"""
        out = []
        for mid, line in [(m, "start-plan") for m in START_PLAN_MODELS] \
                + [(m, "coding-plan") for m in CODING_PLAN_MODELS]:
            out.append({"id": mid, "name": mid.upper(), "region": "intl",
                        "plan": line})
        return out

    def chat(self, payload: dict) -> requests.Response:
        """OpenAI 入站 → Anthropic 出站；返回 OpenAI 形状的伪 SSE Response。"""
        model = str(payload.get("model") or "")
        line, url, token = self._route(model)
        body = to_anthropic_body(payload, model)
        h = _headers(self._jwt(), self._meta().get("device_mid") or "")
        if line == "coding-plan":
            h["Authorization"] = f"Bearer {token}"
            h.pop("HTTP-Referer", None)           # pack 实测：订阅线必须删 Referer
        else:
            h["Authorization"] = f"Bearer {token}"
        try:
            resp = self.http.post(url, headers=h, json=body,
                                  timeout=STREAM_TIMEOUT, allow_redirects=False,
                                  stream=True)
        except requests.RequestException as exc:
            raise UpstreamError(502, "upstream_transport_error",
                                "ZCode 对话连接中断（结果与消耗未知，网关会换号重试）。") from exc
        if resp.ok:
            return _OpenAIShapeResponse(resp, model)
        text = resp.text[:500]
        resp.close()
        raise self._classify(resp.status_code, text)

    def _route(self, model: str) -> tuple[str, str, str]:
        """pack resolveChannelFor：免费线优先；订阅模型需 plan key。"""
        if model in START_PLAN_MODELS:
            return "start-plan", MESSAGES_URL, self._jwt()
        if model in CODING_PLAN_MODELS:
            return "coding-plan", CODING_MESSAGES_URL, self._coding_plan_key()
        # 未知模型：给免费线试一次（上游报错比我们瞎拦诚实）
        return "start-plan", MESSAGES_URL, self._jwt()

    @staticmethod
    def _classify(status: int, text: str) -> UpstreamError:
        if status in (401, 403):
            return UpstreamError(401, "upstream_auth_expired",
                                 "ZCode 登录凭据已失效，请重新登录。")
        if status == 429:
            return UpstreamError(429, "upstream_rate_limit",
                                 "ZCode 限流或免费额度已达上限（429），稍后再试。")
        if status == 402:
            return UpstreamError(402, "upstream_quota",
                                 "ZCode 额度已用尽（402），请领取每日额度或订阅。")
        return UpstreamError(502, "upstream_error",
                             f"ZCode 对话失败（HTTP {status}）：{text[:150]}")

    def credits(self) -> dict:
        """billing/balance → 结构化余额（token 桶，对齐我们 credits 形状）。"""
        resp = self.http.get(BALANCE_URL,
                             headers=_headers(self._jwt(),
                                              self._meta().get("device_mid") or ""),
                             timeout=TIMEOUT, allow_redirects=False)
        if resp.status_code in (401, 403):
            raise UpstreamError(401, "upstream_auth_expired",
                                "ZCode 登录凭据已失效，请重新登录。")
        if not resp.ok:
            raise UpstreamError(502, "upstream_error",
                                f"ZCode 余额服务暂不可用（HTTP {resp.status_code}）。")
        data = (resp.json().get("data") or {}) if "json" in resp.headers.get(
            "content-type", "") else {}
        if data.get("displayMode") == "enterprise":
            return {"total": 0, "packages": [], "expiring_soon": 0, "unit": "tokens"}
        total, nearest = 0, None
        packages: list[dict] = []
        for raw in data.get("balances") or []:
            if not isinstance(raw, dict):
                continue
            remain = raw.get("available_units")
            if not isinstance(remain, (int, float)):
                remain = raw.get("remaining_units")
            remain = remain if isinstance(remain, (int, float)) else 0
            size = raw.get("total_units") if isinstance(
                raw.get("total_units"), (int, float)) else None
            total += remain
            exp = raw.get("expires_at")
            if isinstance(exp, (int, float)) and (nearest is None or exp < nearest):
                nearest = exp
            packages.append({"name": str(raw.get("show_name") or "套餐"),
                             "remain": remain, "size": size, "monthly": False})
        return {"total": total, "packages": packages, "expiring_soon": 0,
                "nearest_expiry": nearest, "unit": "tokens"}
