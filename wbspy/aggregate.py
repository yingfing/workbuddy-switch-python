"""多厂商统一穿透：一个 OpenAI 端点路由到 76 家厂商。

吸收 dsh-xdpool「聚合渠道 + 网关」的核心思路：

* 模型命名 ``<provider>/<model>``（如 ``deepseek/deepseek-chat``、
  ``opencode_zen/glm-4.6``），与 WorkBuddy 池的 ``cn:``/``global:`` 前缀并存；
* 71/76 家为 ``openai-chat`` 格式 → 请求体原样透传（仅重写 model 字段），
  流式 SSE 直通；``anthropic-messages`` / ``openai-responses`` 明确报错不假装支持；
* Key 存 ``~/.wbspy/keys.json``；``auth.kind == none`` 的免费通道无需 key；
* baseUrl 支持 ``{account_id}`` 占位（如 Cloudflare），从 key 条目 extra 取。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from .providers import load_providers

KEYS_PATH = Path.home() / ".wbspy" / "keys.json"

SUPPORTED_FORMATS = ("openai-chat",)


class AggregateError(Exception):
    """可分类的聚合转发失败（message 面向终端用户）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# -- Key 存储 -----------------------------------------------------------------


def load_keys() -> dict[str, dict[str, Any]]:
    if KEYS_PATH.exists():
        try:
            return json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


_KEY_LOCK = __import__("threading").Lock()


def save_key(provider_id: str, api_key: str, extra: dict | None = None) -> None:
    with _KEY_LOCK:                               # Web 并发填 key 时防止读改写互踩
        keys = load_keys()
        entry: dict[str, Any] = {}
        if api_key:
            entry["api_key"] = api_key
        if extra:
            entry["extra"] = {k: str(v) for k, v in extra.items() if v}
        entry["updated_at"] = int(time.time())
        if not entry:
            keys.pop(provider_id, None)
        else:
            keys[provider_id] = entry
        KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEYS_PATH.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            import os
            os.chmod(KEYS_PATH, 0o600)                # 凭据文件仅本用户可读
        except OSError:
            pass


def remove_key(provider_id: str) -> bool:
    keys = load_keys()
    if provider_id in keys:
        del keys[provider_id]
        KEYS_PATH.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


# -- 模型索引与路由 ------------------------------------------------------------

_INDEX: list[dict] | None = None


def _provider_index() -> list[dict]:
    global _INDEX
    if _INDEX is None:
        _INDEX = []
        for p in load_providers():
            for m in p.get("models") or []:
                _INDEX.append({
                    "route_model": f"{p['id']}/{m['id']}",
                    "provider_id": p["id"],
                    "provider": p.get("name"),
                    "model": m["id"],
                    "model_name": m.get("name"),
                    "vision": m.get("vision"),
                    "reasoning": m.get("reasoning"),
                    "context_window": m.get("contextWindow"),
                    "format": p.get("format"),
                    "base_url": p.get("baseUrl"),
                    "auth_kind": (p.get("auth") or {}).get("kind", "apiKey"),
                    "category": p.get("category"),
                    "signup_url": (p.get("auth") or {}).get("url") or p.get("website"),
                })
    return _INDEX


def resolve(route_model: str) -> dict:
    """``provider/model`` → 路由条目。找不到即 404 类错误。"""
    for row in _provider_index():
        if row["route_model"] == route_model:
            return row
    raise AggregateError(404, "model_not_found",
                         f"未知模型 {route_model}。用 GET /v1/models 查看全部可用模型。")


def models() -> list[dict]:
    """全部可路由模型（OpenAI /v1/models 形状），configured 标注 key 状态。"""
    keys = load_keys()
    out = []
    for row in _provider_index():
        if row["format"] not in (*SUPPORTED_FORMATS, "anthropic-messages"):
            continue
        configured = row["auth_kind"] == "none" or bool(keys.get(row["provider_id"], {}).get("api_key"))
        out.append({
            "id": row["route_model"],
            "object": "model",
            "owned_by": f"wbspy:{row['provider_id']}",
            "wbspy": {"configured": configured, "category": row["category"],
                      "provider": row["provider"], "auth_kind": row["auth_kind"],
                      "format": row["format"],
                      "vision": row["vision"], "reasoning": row["reasoning"],
                      "context_window": row["context_window"]},
        })
    return out


def key_status() -> list[dict]:
    keys = load_keys()
    providers = {}
    for row in _provider_index():
        providers.setdefault(row["provider_id"], {
            "provider_id": row["provider_id"], "provider": row["provider"],
            "auth_kind": row["auth_kind"], "model_count": 0, "signup_url": row["signup_url"]})
        providers[row["provider_id"]]["model_count"] += 1
    out = []
    for p in providers.values():
        entry = keys.get(p["provider_id"], {})
        masked = ""
        if entry.get("api_key"):
            k = entry["api_key"]
            masked = k[:4] + "…" + k[-4:] if len(k) > 10 else "已配置"
        out.append({**p, "configured": p["auth_kind"] == "none" or bool(entry.get("api_key")),
                    "key_hint": masked or ("免 Key" if p["auth_kind"] == "none" else ""),
                    "has_account_id": "{account_id}" in str(p.get("signup_url") or "") or None,
                    "updated_at": entry.get("updated_at")})
    return sorted(out, key=lambda x: (not x["configured"], x["provider_id"]))


# -- 转发 ------------------------------------------------------------------------


def forward(route_model: str, payload: dict, timeout: tuple = (15, 600)) -> requests.Response:
    """把 OpenAI 形状的请求转发到厂商上游。返回原始 Response（调用方消费流）。"""
    row = resolve(route_model)
    if row["format"] not in SUPPORTED_FORMATS:
        raise AggregateError(400, "unsupported_format",
                             f"厂商 {row['provider']} 使用 {row['format']} 协议，暂不支持 OpenAI 透传。")

    base = row["base_url"]
    keys = load_keys()
    entry = keys.get(row["provider_id"], {})
    extra = entry.get("extra") or {}
    # baseUrl 占位符替换（如 Cloudflare 的 {account_id}）
    for name, value in extra.items():
        base = base.replace("{" + name + "}", str(value))
    if "{" in base:
        raise AggregateError(400, "missing_account_param",
                             f"该厂商 baseUrl 需要账号参数（如 account_id），请在 Key 配置里补齐。")

    api_key = entry.get("api_key", "")
    if row["auth_kind"] == "apiKey" and not api_key:
        raise AggregateError(401, "key_required",
                             f"厂商 {row['provider']} 尚未配置 API Key，请在网页「免费可调用 API」页填入。")

    body = {**payload, "model": row["model"]}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = base.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise AggregateError(502, "upstream_transport_error",
                             f"连接厂商上游失败（{url}）：{exc}") from exc
    if not resp.ok:
        detail = resp.text[:400]
        status = resp.status_code
        resp.close()
        code = "upstream_auth" if status in (401, 403) else \
               "upstream_rate" if status == 429 else "upstream_error"
        raise AggregateError(502 if status >= 500 else status, code,
                             f"厂商 {row['provider']} 返回 HTTP {status}：{detail}")
    return resp


def forward_simple(route_model: str, payload: dict) -> dict:
    """非流式转发：收完整 JSON。"""
    resp = forward(route_model, payload)
    try:
        return resp.json()
    finally:
        resp.close()


def stream_sse(route_model: str, payload: dict) -> Iterator[bytes]:
    """流式转发：逐块 yield 上游 SSE 字节。"""
    resp = forward(route_model, payload)
    try:
        for chunk in resp.iter_content(chunk_size=None):
            if chunk:
                yield chunk
    finally:
        resp.close()


# -- 同模型多厂商 failover ------------------------------------------------------


def _candidates(route_model: str) -> list[dict]:
    """给定路由模型，返回按优先级排序的候选（精确 > 同模型跨厂商已配 Key）。"""
    keys = load_keys()
    row = resolve(route_model)
    exact = [row]
    if route_model.endswith("/*"):                     # provider/* 通配：该厂商全部模型
        pid = route_model.split("/", 1)[0]
        exact = [r for r in _provider_index()
                 if r["provider_id"] == pid and r["format"] in SUPPORTED_FORMATS]
        if not exact:
            raise AggregateError(404, "model_not_found", f"厂商 {pid} 无可路由模型。")
        return exact
    # 精确模型失败后：其他已配置厂商提供的同名模型
    seen_providers = {row["provider_id"]}
    for r in _provider_index():
        if r["model"] == row["model"] and r["provider_id"] not in seen_providers \
                and r["format"] in SUPPORTED_FORMATS:
            configured = r["auth_kind"] == "none" or bool(keys.get(r["provider_id"], {}).get("api_key"))
            if configured:
                exact.append(r)
                seen_providers.add(r["provider_id"])
    return exact


def forward_with_failover(route_model: str, payload: dict,
                          timeout: tuple = (15, 600)) -> tuple[dict, requests.Response]:
    """依次尝试候选（精确模型 → 同模型跨厂商），返回 (实际路由, Response)。"""
    candidates = _candidates(route_model)
    last_error: AggregateError | None = None
    for row in candidates:
        try:
            resp = _forward_row(row, payload, timeout)
            return row, resp
        except AggregateError as exc:
            last_error = exc
            if exc.status in (401, 404):
                continue                               # 换下一候选
            if exc.code == "key_required" and len(candidates) > 1:
                continue
            raise
    raise last_error or AggregateError(502, "upstream_error", "全部候选厂商均失败。")


def _forward_row(row: dict, payload: dict, timeout: tuple) -> requests.Response:
    """对单个路由条目执行转发（原 forward 逻辑抽取）。"""
    base = row["base_url"]
    keys = load_keys()
    entry = keys.get(row["provider_id"], {})
    extra = entry.get("extra") or {}
    for name, value in extra.items():
        base = base.replace("{" + name + "}", str(value))
    if "{" in base:
        raise AggregateError(400, "missing_account_param",
                             f"该厂商 baseUrl 需要账号参数（如 account_id），请在 Key 配置里补齐。")
    api_key = entry.get("api_key", "")
    if row["auth_kind"] == "apiKey" and not api_key:
        raise AggregateError(401, "key_required",
                             f"厂商 {row['provider']} 尚未配置 API Key，请在网页「免费可调用 API」页填入。")
    body = {**payload, "model": row["model"]}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = base.rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout, stream=True)
    except requests.RequestException as exc:
        raise AggregateError(502, "upstream_transport_error",
                             f"连接厂商上游失败（{url}）：{exc}") from exc
    if not resp.ok:
        detail = resp.text[:400]
        status = resp.status_code
        resp.close()
        code = "upstream_auth" if status in (401, 403) else \
               "upstream_rate" if status == 429 else "upstream_error"
        raise AggregateError(502 if status >= 500 else status, code,
                             f"厂商 {row['provider']} 返回 HTTP {status}：{detail}")
    return resp
