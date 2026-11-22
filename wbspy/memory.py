"""WorkBuddy 服务端记忆同步与网关注入。

## 机制（2026-11-22 协议侦察结论）

* 记忆本体是服务端总结的 ``foryou_prompt`` 文本（工作背景/个人背景/当前关注/
  近期动态四段式），按账号维度存储，``version`` 单调递增、``updated_at`` 是
  最近一次总结时间（ISO 8601 带时区字符串）。
* 读取接口：``GET {origin}/api/memory/profile``，鉴权 ``Authorization: Bearer
  <JWT>`` + ``x-user-id: <uid>, <uid>``（uid 逗号分隔出现两次），UA ``node``。
* 触发节奏：桌面端每 10 分钟整点轮询一次快照（带 polling lease）；总结本身
  是服务端异步任务，对话响应不携带任何记忆字段——何时升级 version 由服务端
  决定（实测 version=4 停在三天前的对话，API 调用后并不立即重总结）。
* 手机 App「记忆」页与网关对话共享同一份服务端记忆；桌面端对话请求体不含
  记忆内容（推理帧走服务端内部通道，注入发生在服务端侧）。

## 网关注入

``inject_payload(doc, payload, peers=...)`` 在对话 messages 头部插入一条 system
消息，把账号池**全部** WorkBuddy 账号的记忆合并注入（每段标注来源账号与版本）
——调出哪个号都能用上别的号记住的事。选中账号排在最前。带 TTL 缓存（10 分钟，
对齐桌面端节奏），其余账号记忆并行拉取，失败静默不阻塞对话。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import requests

TTL_S = 600                       # 与桌面端 10 分钟同步节奏对齐
NEG_TTL_S = 60                    # 失败负缓存：60 秒内不重试（接口挂时防每发卡超时）
_FETCH_TIMEOUT = 12

_cache: dict[str, tuple[float, dict[str, Any]]] = {}   # uid -> (fetched_at, data)
_lock = threading.Lock()


def fetch_profile(doc, timeout: int = _FETCH_TIMEOUT) -> dict[str, Any] | None:
    """拉取账号的服务端记忆快照。失败返回 None（不抛——注入是加分项）。"""
    token = getattr(doc, "token", None)
    uid = getattr(doc, "uid", None)
    if not token or not uid:
        return None
    try:
        r = requests.get(
            f"{doc.origin}/api/memory/profile",
            headers={"Authorization": f"Bearer {token}",
                     "x-user-id": f"{uid}, {uid}",
                     "User-Agent": "node"},
            timeout=timeout)
        if r.status_code != 200:
            return None
        d = (r.json() or {}).get("data") or {}
        prompt = d.get("foryou_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        return {"version": d.get("version"),
                "updated_at": d.get("updated_at") or "",
                "foryou_prompt": prompt}
    except Exception:
        return None


def get_profile(doc, force: bool = False) -> dict[str, Any] | None:
    """带 TTL 缓存的记忆快照；force=True 跳过缓存。

    成功缓存 TTL_S（10 分钟）；失败也缓存（负缓存 NEG_TTL_S，60 秒）——
    接口挂掉时每分钟最多白等一次，不会每个对话都卡满超时。
    """
    uid = getattr(doc, "uid", None)
    if not uid:
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(uid)
        if hit and not force:
            ts, data = hit
            if now - ts < (TTL_S if data is not None else NEG_TTL_S):
                return data or None
    data = fetch_profile(doc)
    with _lock:
        _cache[uid] = (now, data)
    return data


def invalidate(uid: str | None = None) -> None:
    """清缓存（手动刷新用）；uid=None 全清。"""
    with _lock:
        if uid is None:
            _cache.clear()
        else:
            _cache.pop(uid, None)


def _label(doc) -> str:
    """账号展示名（label() 异常时退回 uid 前 12 位）。"""
    try:
        return doc.label()
    except Exception:
        return str(getattr(doc, "uid", "?"))[:12]


def _seg_text(label: str, profile: dict[str, Any]) -> str:
    ts = str(profile.get("updated_at") or "").replace("T", " ")
    head = f"── 账号 {label}"
    if profile.get("version") is not None:
        head += f" · v{profile['version']}"
    if ts:
        head += f" · 更新于 {ts}"
    return head + "\n" + profile["foryou_prompt"]


def _memory_text(entries: list[tuple[str, dict[str, Any]]]) -> str:
    head = (f"【用户长期记忆】（WorkBuddy 记忆系统 · {len(entries)} 个账号）\n"
            "以下是跨对话有效的既有了解；用户在任一端让你「记住」的事也会总结进这里。"
            "各账号记忆相互独立，段落开头标注了来源账号：")
    return head + "\n" + "\n\n".join(_seg_text(lb, p) for lb, p in entries)


def inject_messages(messages: list, entries: list[tuple[str, dict[str, Any]]]) -> list:
    """在 messages 头部插入记忆 system 消息（返回新列表，不改原列表）。"""
    if not isinstance(messages, list):
        return messages
    mem = {"role": "system", "content": _memory_text(entries)}
    return [mem, *messages]


def collect_entries(doc, peers: Iterable | None = None) -> list:
    """收集选中账号 + 其余账号的记忆快照，选中账号排最前。

    peers 并行拉取（TTL 缓存复用），失败静默跳过该账号。
    返回 [(label, profile), …]；全空返回 []。
    """
    entries: list[tuple[str, dict[str, Any]]] = []
    main = get_profile(doc)
    if main:
        entries.append((_label(doc), main))
    peer_docs = [d for d in (peers or []) if d is not doc]
    if peer_docs:
        with ThreadPoolExecutor(max_workers=min(6, len(peer_docs))) as ex:
            for p_doc, prof in zip(peer_docs, ex.map(get_profile, peer_docs)):
                if prof:
                    entries.append((_label(p_doc), prof))
    return entries


def inject_payload(doc, payload: dict, peers: Iterable | None = None) -> dict:
    """chat_failover 注入回调：合并选中账号与其余账号的记忆插进 payload
    （选号后才注入，换号容错时对新号重新收集，保证选中账号的记忆排最前）。
    失败静默原样返回。"""
    entries = collect_entries(doc, peers)
    if not entries:
        return payload
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return payload
    return {**payload, "messages": inject_messages(msgs, entries)}
