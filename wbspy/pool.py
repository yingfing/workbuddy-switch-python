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


class CooldownTable:
    """(uid, model) → 冷却截止时间。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until: dict[tuple[str, str], float] = {}

    def set(self, uid: str, model: str, seconds: int) -> None:
        with self._lock:
            self._until[(uid, model)] = time.time() + max(0, seconds)

    def get(self, uid: str, model: str) -> float:
        with self._lock:
            return self._until.get((uid, model), 0.0)

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for key, until in list(self._until.items()):
                if until < now:
                    del self._until[key]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"uid": uid, "model": model, "cooldown_until": until}
                    for (uid, model), until in self._until.items()]


class AccountPool:
    def __init__(self, documents: list[Document]):
        self.documents = {d.uid: d for d in documents}
        self.clients = {uid: WorkBuddyClient(doc) for uid, doc in self.documents.items()}
        self.dead_until: dict[str, float] = {}      # 凭据失效账号的复活时间
        self.cooldowns = CooldownTable()
        self._cursor = 0
        self._lock = threading.Lock()

    # -- 状态 -----------------------------------------------------------------

    def refresh_documents(self, documents: list[Document]) -> None:
        with self._lock:
            for doc in documents:
                self.documents[doc.uid] = doc
                self.clients[doc.uid] = WorkBuddyClient(doc)
                if doc.api_ready:
                    self.dead_until.pop(doc.uid, None)

    def healthy(self, model: str = "*") -> list[Document]:
        now = time.time()
        self.cooldowns.sweep()
        out = []
        for doc in self.documents.values():
            if not doc.api_ready or self.dead_until.get(doc.uid, 0) > now:
                continue
            if model != "*" and self.cooldowns.get(doc.uid, model) > now:
                continue
            out.append(doc)
        return out

    def status(self) -> list[dict]:
        now = time.time()
        return [{
            "uid": d.uid[:12], "label": d.label(), "region": d.region,
            "api_ready": d.api_ready,
            "token_expires_at": d.token_expires_at,
            "dead": self.dead_until.get(d.uid, 0) > now,
            "cooldowns": [c for c in self.cooldowns.snapshot() if c["uid"] == d.uid],
        } for d in self.documents.values()]

    # -- 调度 -----------------------------------------------------------------

    def pick(self, model: str, exclude: set[str] | None = None) -> Document | None:
        """轮换选择下一个健康账号。"""
        exclude = exclude or set()
        candidates = [d for d in self.healthy(model) if d.uid not in exclude]
        if not candidates:
            return None
        with self._lock:
            self._cursor = (self._cursor + 1) % len(candidates)
            return candidates[self._cursor]

    def apply_error(self, doc: Document, error: UpstreamError, model: str) -> None:
        """按错误类型记账冷却 / 死亡。"""
        if error.kind == "auth":
            self.dead_until[doc.uid] = time.time() + 300   # 5 分钟后允许刷新重试
        elif error.kind in ("rate", "model", "channel"):
            self.cooldowns.set(doc.uid, model, error.retry_after or DEFAULT_MODEL_COOLDOWN)

    # -- 请求 -----------------------------------------------------------------

    def chat_failover(self, payload: dict):
        """带自动换号的对话。返回 (document, response)。

        生成器场景下 response 由调用方负责 close()。
        """
        model = str(payload.get("model", ""))
        tried: set[str] = set()
        last_error: UpstreamError | None = None
        while True:
            doc = self.pick(model, exclude=tried)
            if doc is None:
                if last_error:
                    raise last_error
                raise UpstreamError(503, "no_available_accounts",
                                    "账号池中没有可用账号（全部冷却、失效或未登录）。")
            tried.add(doc.uid)
            try:
                return doc, self.clients[doc.uid].chat(payload)
            except UpstreamError as exc:
                last_error = exc
                self.apply_error(doc, exc, model)
                if exc.kind in ("auth", "rate", "model"):
                    continue      # 换号重试
                raise             # 渠道拒绝重试过 / 上游 5xx 不换号

    def models(self) -> list[dict]:
        """聚合全部账号的模型目录（去重）。"""
        seen: dict[str, dict] = {}
        for doc in self.healthy():
            try:
                for row in self.clients[doc.uid].models():
                    key = f"{'global' if doc.region == 'global' else 'cn'}:{row['id']}"
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
                for uid, doc in list(self.documents.items()):
                    if not doc.refresh_token or doc.protected_token:
                        continue
                    exp = doc.token_expires_at or 0
                    if exp and exp > (time.time() + 600) * 1000:
                        continue                       # 还没临期
                    try:
                        auth = self.clients[uid].refresh()
                        root = doc.raw
                        root["auth"] = {**root.get("auth", {}), **auth}
                        fresh = Document(
                            raw=root, uid=uid,
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
