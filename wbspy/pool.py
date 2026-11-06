"""网关账号池：多账号轮换 + 按错误类型冷却 + 自动容错。

对应原版 api-gateway.mjs 的核心机制：

* 每次请求挑一个「当前可用」的账号（未冷却、token 未过期）；
* 冷却按 ``(uid, model)`` 记账：限流按 Retry-After，模型不可用 60s，
  渠道拒绝 60s，凭据失效直接标记 dead（等待 token 刷新复活）；
* 一次请求内按序 failover，最多试完全部可用账号。
"""

from __future__ import annotations

import threading
import time

from .document import Document
from .upstream import UpstreamError, WorkBuddyClient

DEFAULT_MODEL_COOLDOWN = 60


def _key(doc: Document) -> tuple[str, str]:
    """账号池内部键：(storage_scope, uid)。

    跨渠道 uid 命名空间独立（TRAE 的 "1" ≠ Qoder 的 "1"），
    裸 uid 会在池字典/冷却表里互相覆盖。
    """
    return (doc.storage_scope, doc.uid)


class CooldownTable:
    """(scope, uid, model) → 冷却截止时间。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until: dict[tuple[str, str, str], float] = {}

    def set(self, account: tuple[str, str], model: str, seconds: int) -> None:
        with self._lock:
            self._until[(account[0], account[1], model)] = time.time() + max(0, seconds)

    def get(self, account: tuple[str, str], model: str) -> float:
        with self._lock:
            return self._until.get((account[0], account[1], model), 0.0)

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for key, until in list(self._until.items()):
                if until < now:
                    del self._until[key]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"uid": f"{scope}::{uid}", "model": model, "cooldown_until": until}
                    for (scope, uid, model), until in self._until.items()]


class AccountPool:
    def __init__(self, documents: list[Document]):
        self.documents = {_key(d): d for d in documents}
        self.clients = {k: WorkBuddyClient(doc) for k, doc in self.documents.items()}
        self.dead_until: dict[tuple[str, str], float] = {}   # 凭据失效账号的复活时间
        self.cooldowns = CooldownTable()
        self._cursor = 0
        self._lock = threading.Lock()

    # -- 状态 -----------------------------------------------------------------

    def refresh_documents(self, documents: list[Document]) -> None:
        with self._lock:
            for doc in documents:
                k = _key(doc)
                self.documents[k] = doc
                self.clients[k] = WorkBuddyClient(doc)
                if doc.api_ready:
                    self.dead_until.pop(k, None)

    def healthy(self, model: str = "*") -> list[Document]:
        now = time.time()
        self.cooldowns.sweep()
        out = []
        for k, doc in self.documents.items():
            if "chat" not in doc.capabilities or not doc.api_ready or self.dead_until.get(k, 0) > now:
                continue                     # 渠道未声明 chat 能力 → 不进 WorkBuddy 上游路由
            if model != "*" and self.cooldowns.get(k, model) > now:
                continue
            out.append(doc)
        return out

    def status(self) -> list[dict]:
        now = time.time()
        return [{
            "uid": d.uid[:12], "label": d.label(), "region": d.region,
            "channel": d.channel_name,
            "api_ready": d.api_ready,
            "token_expires_at": d.token_expires_at,
            "dead": self.dead_until.get(_key(d), 0) > now,
            "cooldowns": [c for c in self.cooldowns.snapshot()
                          if c["uid"].endswith("::" + d.uid)],
        } for d in self.documents.values()]

    # -- 调度 -----------------------------------------------------------------

    def pick(self, model: str, exclude: set | None = None) -> Document | None:
        """轮换选择下一个健康账号。"""
        exclude = exclude or set()
        candidates = [d for d in self.healthy(model) if _key(d) not in exclude]
        if not candidates:
            return None
        with self._lock:
            self._cursor = (self._cursor + 1) % len(candidates)
            return candidates[self._cursor]

    def apply_error(self, doc: Document, error: UpstreamError, model: str) -> None:
        """按错误类型记账冷却 / 死亡。"""
        k = _key(doc)
        if error.kind == "auth":
            self.dead_until[k] = time.time() + 300   # 5 分钟后允许刷新重试
        elif error.kind in ("rate", "model", "channel"):
            self.cooldowns.set(k, model, error.retry_after or DEFAULT_MODEL_COOLDOWN)

    # -- 请求 -----------------------------------------------------------------

    def chat_failover(self, payload: dict):
        """带自动换号的对话。返回 (document, response)。

        生成器场景下 response 由调用方负责 close()。
        """
        model = str(payload.get("model", ""))
        tried: set = set()
        last_error: UpstreamError | None = None
        while True:
            doc = self.pick(model, exclude=tried)
            if doc is None:
                if last_error:
                    raise last_error
                raise UpstreamError(503, "no_available_accounts",
                                    "账号池中没有可用账号（全部冷却、失效或未登录）。")
            tried.add(_key(doc))
            try:
                return doc, self.clients[_key(doc)].chat(payload)
            except UpstreamError as exc:
                last_error = exc
                self.apply_error(doc, exc, model)
                if exc.kind in ("auth", "rate", "model"):
                    continue      # 换号重试
                raise             # 渠道拒绝重试过 / 上游 5xx 不换号

    def models(self) -> list[dict]:
        """聚合全部账号的模型目录（去重）。统一 ``workbuddy:`` 前缀——
        客户端里一眼可辨；区域信息在 region 字段（路由时本就不分区域，
        cn/global 同名模型自然合并）。"""
        seen: dict[str, dict] = {}
        for doc in self.healthy():
            try:
                for row in self.clients[_key(doc)].models():
                    key = f"workbuddy:{row['id']}"
                    seen.setdefault(key, {**row, "id": key})
            except UpstreamError:
                continue
        return list(seen.values())

    # -- 后台 token 自动刷新 -----------------------------------------------------

    def start_auto_refresh(self, interval_sec: int = 300) -> threading.Thread:
        """后台循环：凭据临期（<10 分钟）或已过期即用 refresh_token 续期。"""

        def worker():  # pragma: no cover
            import json as _json
            from .store import save_account
            while True:
                for k, doc in list(self.documents.items()):
                    if "refresh" not in doc.capabilities or not doc.refresh_token or doc.protected_token:
                        continue             # 渠道未声明 refresh 能力 → 不代为续期
                    exp = doc.token_expires_at or 0
                    if exp and exp > (time.time() + 600) * 1000:
                        continue                       # 还没临期
                    try:
                        auth = self.clients[k].refresh()
                        root = doc.raw
                        root["auth"] = {**root.get("auth", {}), **auth}
                        fresh = Document(
                            raw=root, uid=doc.uid,
                            token=auth["accessToken"],
                            refresh_token=auth.get("refreshToken", doc.refresh_token),
                            nickname=doc.nickname, region=doc.region, domain=doc.domain)
                        fresh.token_expires_at = auth.get("expiresAt")
                        save_account(fresh)
                        self.refresh_documents([fresh])
                    except Exception:
                        pass                           # 刷新失败等下一轮 / 等用户重新授权
                time.sleep(interval_sec)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
