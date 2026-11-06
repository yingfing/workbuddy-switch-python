"""网关账号池：多账号轮换 + 按错误类型冷却 + 自动容错。

对应原版 api-gateway.mjs 的核心机制：

* 每次请求挑一个「当前可用」的账号（未冷却、token 未过期）；
* 冷却按 ``(uid, model)`` 记账：限流按 Retry-After，模型不可用 60s，
  渠道拒绝 60s，凭据失效直接标记 dead（等待 token 刷新复活）；
* 一次请求内按序 failover，最多试完全部可用账号。
"""

from __future__ import annotations

import random
import threading
import time

from .document import Document
from .upstream import UpstreamError, WorkBuddyClient

DEFAULT_MODEL_COOLDOWN = 60

# -- 调度策略常量（对齐 dsh-xdpool 的池策略）-----------------------------------
SESSION_TTL = 2 * 3600          # 会话粘性映射保质期：2 小时无活动即解绑
MAX_COOLING_WAIT = 90           # 全账号冷却时排队等待上限（秒），超过仍 503
DISTRIBUTIONS = ("round-robin", "priority", "balanced")
# 免费行 → 同名付费行退路：上游按「行」分别限流，免费行被 429 冷却时
# 付费行通常还通（对齐 xdpool 的行映射）
FALLBACK_ROWS = {"hy3": "hy3-x", "deepseek-v4.1-flash": "deepseek-v4.1-flash-sg"}


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
        self.last_used: dict[tuple[str, str], float] = {}    # balanced 策略权重依据
        self.session_map: dict[str, tuple[tuple[str, str], float]] = {}  # 会话粘性
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
        return [d for k, d in self.documents.items() if self._is_usable(d, model, now)]

    def _is_usable(self, doc: Document, model: str, now: float | None = None) -> bool:
        """账号此刻能否接「model」的活：chat 能力 + 凭据就绪 + 未死亡 + 该模型未冷却。"""
        now = now if now is not None else time.time()
        k = _key(doc)
        if "chat" not in doc.capabilities or not doc.api_ready or self.dead_until.get(k, 0) > now:
            return False                 # 渠道未声明 chat 能力 / 死亡 → 不进 WorkBuddy 上游路由
        if model != "*" and self.cooldowns.get(k, model) > now:
            return False
        return True

    def _distribution(self) -> str:
        """账号分配策略（config gateway.distribution，热读支持网页/CLI 改后生效）。"""
        try:
            from .store import load_config
            v = str((load_config().get("gateway") or {}).get("distribution") or "round-robin")
        except Exception:
            v = "round-robin"
        return v if v in DISTRIBUTIONS else "round-robin"

    def _sweep_sessions(self, now: float) -> None:
        for s in [s for s, (_, at) in self.session_map.items() if now - at > SESSION_TTL]:
            del self.session_map[s]

    def _unbind(self, session: str | None, k: tuple[str, str]) -> None:
        """会话绑定的账号失败时解除粘性，让该对话下次重新分配。"""
        if session and self.session_map.get(session, (None,))[0] == k:
            self.session_map.pop(session, None)

    def pick(self, model: str, exclude: set | None = None,
             session: str | None = None) -> Document | None:
        """按分配策略选择健康账号；session 给定时优先粘住上次用的号。"""
        exclude = exclude or set()
        now = time.time()
        self.cooldowns.sweep()

        # -- sticky：会话粘住上次成功服务的账号（保护上游 prompt 缓存） --------
        if session:
            self._sweep_sessions(now)
            bound = self.session_map.get(session)
            if bound:
                k = bound[0]
                doc = self.documents.get(k)
                if doc is not None and k not in exclude and self._is_usable(doc, model, now):
                    self.session_map[session] = (k, now)
                    self.last_used[k] = now
                    return doc
                self.session_map.pop(session, None)   # 号废了/被排除 → 重新分配

        candidates = [d for d in self.documents.values()
                      if _key(d) not in exclude and self._is_usable(d, model, now)]
        if not candidates:
            return None

        strategy = self._distribution()
        if strategy == "priority":
            doc = candidates[0]                       # 按入库顺序，用完一个再换下一个
        elif strategy == "balanced":
            # 随机加权、偏向闲置久的：距上次使用越久权重越高
            weights = [1.0 + now - self.last_used.get(_key(d), 0) for d in candidates]
            doc = random.choices(candidates, weights=weights)[0]
        else:                                         # round-robin（默认）
            with self._lock:
                self._cursor = (self._cursor + 1) % len(candidates)
                doc = candidates[self._cursor]

        k = _key(doc)
        self.last_used[k] = now
        if session:
            self.session_map[session] = (k, now)      # 绑定会话
        return doc

    def _shortest_cooling(self, model: str) -> float | None:
        """该模型全池冷却时，距离最早解冻还剩多少秒（只算活着能刷新的号）。"""
        now = time.time()
        waits = [self.cooldowns.get(_key(d), model) - now
                 for d in self.documents.values()
                 if "chat" in d.capabilities and d.api_ready
                 and self.dead_until.get(_key(d), 0) <= now]
        waits = [w for w in waits if w > 0]
        return min(waits) if waits else None

    def apply_error(self, doc: Document, error: UpstreamError, model: str) -> None:
        """按错误类型记账冷却 / 死亡。死亡时解绑其全部会话粘性。"""
        k = _key(doc)
        if error.kind == "auth":
            self.dead_until[k] = time.time() + 300   # 5 分钟后允许刷新重试
            for s in [s for s, (bk, _) in self.session_map.items() if bk == k]:
                del self.session_map[s]
        elif error.kind in ("rate", "model", "channel"):
            self.cooldowns.set(k, model, error.retry_after or DEFAULT_MODEL_COOLDOWN)

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

    # （pick / apply_error 已合并到上方健康判定的统一实现）

    # -- 请求 -----------------------------------------------------------------

    def chat_failover(self, payload: dict, session: str | None = None):
        """带自动换号的对话。返回 (document, response)。

        生成器场景下 response 由调用方负责 close()。
        换号之外的两层兜底（对齐 dsh-xdpool）：
        * 免费行全冷却 → 试同名付费行（FALLBACK_ROWS）
        * 全池冷却 → 等最早的冷却结束再试一次（≤ MAX_COOLING_WAIT 秒）
        """
        model = str(payload.get("model", ""))
        tried: set = set()
        last_error: UpstreamError | None = None
        waited = False
        while True:
            doc = self.pick(model, exclude=tried, session=session)
            if doc is None:
                # 兜底一：免费行冷却时降级试付费行（换行名重试，客户端无感）
                fb = FALLBACK_ROWS.get(model)
                if fb:
                    alt = self.pick(fb, exclude=tried, session=None)
                    if alt is not None:
                        try:
                            return alt, self.clients[_key(alt)].chat({**payload, "model": fb})
                        except UpstreamError as exc:
                            last_error = exc
                            self.apply_error(alt, exc, fb)
                            self._unbind(session, _key(alt))
                            if exc.kind in ("auth", "rate", "model"):
                                continue
                            raise
                # 兜底二：全池冷却 → 等最早的解冻（一次，≤90 秒）
                if not waited:
                    wait = self._shortest_cooling(model)
                    if wait and wait <= MAX_COOLING_WAIT:
                        waited = True
                        time.sleep(wait + 0.5)
                        tried.clear()             # 解冻的号重新参与竞争
                        continue
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
                self._unbind(session, _key(doc))  # 粘住的号废了 → 解绑让对话重分配
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
