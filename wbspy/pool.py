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
from typing import Callable

from .document import Document, _token_expiry
from .upstream import UpstreamError, WorkBuddyClient

DEFAULT_MODEL_COOLDOWN = 60

# -- 调度策略常量（对齐 dsh-xdpool 的池策略）-----------------------------------
SESSION_TTL = 2 * 3600          # 会话粘性映射保质期：2 小时无活动即解绑
MAX_COOLING_WAIT = 90           # 全账号冷却时排队等待上限（秒），超过仍 503
DISTRIBUTIONS = ("round-robin", "priority", "balanced")
# 免费行 → 同名付费行退路：上游按「行」分别限流，免费行被 429 冷却时
# 付费行通常还通（对齐 xdpool 的行映射）
FALLBACK_ROWS = {"hy3": "hy3-x", "deepseek-v4.1-flash": "deepseek-v4.1-flash-sg"}

# 泳道：渠道分组。同一泳道的账号共享模型路由（请求按前缀选泳道，
# 泳道内多账号轮换容错）；不同泳道的凭据绝不互相调用。
LANE_CHANNELS = {"workbuddy": ("buddy", "workbuddy"), "cline": ("cline",)}


def _lane_of(doc: Document) -> str:
    """账号所属泳道（workbuddy / cline / 其他渠道原样）。"""
    for lane, ids in LANE_CHANNELS.items():
        if doc.channel_id in ids:
            return lane
    return doc.channel_id or "other"


def _client_for(doc: Document):
    """按渠道构造客户端：Cline 走 OpenAI 兼容 api.cline.bot，Qoder 走
    静态目录客户端（推理协议未接入），ZCode 走 Anthropic 线转码客户端，
    其余走 WorkBuddy。"""
    from .upstream import ClineClient, WorkBuddyClient
    if doc.channel_id == "cline":
        return ClineClient(doc)
    if doc.channel_id in ("qoder", "qodercn"):
        from .qoder import QoderClient
        return QoderClient(doc)
    if doc.channel_id == "zcode":
        from .zcode import ZCodeClient
        return ZCodeClient(doc)
    return WorkBuddyClient(doc)


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
        self.clients = {k: _client_for(doc) for k, doc in self.documents.items()}
        self.dead_until: dict[tuple[str, str], float] = {}   # 凭据失效账号的复活时间
        self.cooldowns = CooldownTable()
        self.last_used: dict[tuple[str, str], float] = {}    # balanced 策略权重依据
        self.session_map: dict[str, tuple[tuple[str, str], float]] = {}  # 会话粘性
        self.event_log: Callable[[str], None] | None = None  # 网页日志钩子（webui 接线）
        self._cursor = 0
        self._lock = threading.Lock()

    def _emit(self, msg: str) -> None:
        """关键调度事件（换号/凭据失效/续期）推给网页日志；未接线时静默。"""
        try:
            if self.event_log:
                self.event_log(msg)
        except Exception:
            pass

    # -- 状态 -----------------------------------------------------------------

    def refresh_documents(self, documents: list[Document]) -> None:
        with self._lock:
            for doc in documents:
                k = _key(doc)
                self.documents[k] = doc
                self.clients[k] = _client_for(doc)
                if doc.api_ready:
                    self.dead_until.pop(k, None)

    def client(self, doc: Document):
        """某账号的渠道客户端（网关/自动化按此取，不手拼 key）。"""
        return self.clients[_key(doc)]

    def healthy(self, model: str = "*", lane: str | None = None) -> list[Document]:
        now = time.time()
        self.cooldowns.sweep()
        return [d for k, d in self.documents.items()
                if self._is_usable(d, model, now, lane=lane)]

    def _is_usable(self, doc: Document, model: str, now: float | None = None,
                   lane: str | None = None) -> bool:
        """账号此刻能否接「model」的活：chat 能力 + 凭据就绪 + 未死亡 + 该模型未冷却。

        lane 给定时还要求账号属于该泳道——cline 的请求绝不能落到
        workbuddy 账号上（凭据协议完全不同），反之亦然。
        """
        now = now if now is not None else time.time()
        k = _key(doc)
        if "chat" not in doc.capabilities or not doc.api_ready or self.dead_until.get(k, 0) > now:
            return False                 # 渠道未声明 chat 能力 / 死亡 → 不进上游路由
        if lane is not None and _lane_of(doc) != lane:
            return False
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

    # -- 账号优先级（同渠道同地区分组内拖拽排序） --------------------------------

    @staticmethod
    def _group_key(doc: Document) -> str:
        """账号分组键：同渠道 + 同地区才算「同种账号」——优先级只在组内生效
        （不同地区/渠道的账号模型目录不同，本就没有先后可言）。"""
        return f"{doc.channel_id or '?'}:{doc.region or ''}"

    def _order_map(self) -> dict:
        """用户拖拽的组内账号顺序（config gateway.account_order，热读）。"""
        try:
            from .store import load_config
            v = (load_config().get("gateway") or {}).get("account_order")
        except Exception:
            return {}
        return v if isinstance(v, dict) else {}

    def set_priority_order(self, group: str, uids: list) -> None:
        """保存某分组的账号顺序（网页拖拽排序落盘，priority 策略下即时生效）。"""
        from .store import load_config, save_config
        if not isinstance(group, str) or not group:
            raise ValueError("分组键不能为空")
        if not isinstance(uids, list) or not all(isinstance(u, str) for u in uids):
            raise ValueError("顺序须为 uid 字符串列表")
        cfg = load_config()
        gw = cfg.setdefault("gateway", {})
        orders = gw.get("account_order")
        orders = orders if isinstance(orders, dict) else {}
        orders[group] = [str(u) for u in uids]
        gw["account_order"] = orders
        save_config(cfg)

    def _priority_sorted(self, candidates: list) -> list:
        """priority 策略的候选排序：组间保持入库顺序，组内按用户拖拽顺序。

        未列入顺序表的账号排在本组末尾（组内相对稳定）；顺序表热读，
        网页拖完下一发请求即生效。
        """
        if len(candidates) < 2:
            return candidates
        order_map = self._order_map()
        group_seq: dict[str, int] = {}
        for d in candidates:
            group_seq.setdefault(self._group_key(d), len(group_seq))

        def _key(t):
            i, d = t
            lst = order_map.get(self._group_key(d))
            if isinstance(lst, list) and d.uid in lst:
                inner = lst.index(d.uid)
            else:
                inner = (len(lst) if isinstance(lst, list) else 0) + i
            return (group_seq[self._group_key(d)], inner, i)

        return [d for _, d in sorted(enumerate(candidates), key=_key)]

    # -- priority 策略的积分感知降级 ------------------------------------------
    # 用户语义：固定用优先级最高的账号，积分烧到阈值以下就换「还活着里积分
    # 最高的」继续用，直到全部账号都低于阈值。实现为 pick 时的过滤规则，
    # 不需要后台任务——每次取号现查缓存即可。

    def _priority_threshold(self) -> float:
        """积分降级阈值（config gateway.priority_threshold，默认 100；<=0 关闭感知）。"""
        try:
            from .store import load_config
            v = (load_config().get("gateway") or {}).get("priority_threshold")
        except Exception:
            return 100.0
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 100.0

    @staticmethod
    def set_priority_threshold(value) -> float:
        from .store import load_config, save_config
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("阈值须为数字")
        if v < 0:
            raise ValueError("阈值不能为负（0 = 关闭积分感知）")
        cfg = load_config()
        cfg.setdefault("gateway", {})["priority_threshold"] = v
        save_config(cfg)
        return v

    def _credits_cache(self) -> dict:
        """「查询余额」落盘的积分缓存（config gateway.credits_cache，热读）。"""
        try:
            from .store import load_config
            v = (load_config().get("gateway") or {}).get("credits_cache")
        except Exception:
            return {}
        return v if isinstance(v, dict) else {}

    def remember_credits(self, uid: str, total) -> None:
        """缓存某账号的剩余积分（网页「查询余额」时调用，重启不丢）。"""
        if not isinstance(total, (int, float)) or isinstance(total, bool):
            return
        from .store import load_config, save_config
        cfg = load_config()
        cache = cfg.setdefault("gateway", {}).get("credits_cache")
        cache = cache if isinstance(cache, dict) else {}
        cache[str(uid)] = {"total": total, "at": time.time()}
        cfg["gateway"]["credits_cache"] = cache
        save_config(cfg)

    def _pick_by_credits(self, ordered: list) -> Document:
        """priority 策略取号（ordered 已按拖拽顺序排好，同组内）。

        用户语义：固定用优先级最高的账号；其积分烧穿阈值 → 换剩余账号里
        「当前积分最高」的继续固定用；直到全部低于阈值。规则：
        - 已知积分且 >= 阈值 → 达标，取其中积分最高者（积分单调递减，
          「最高」天然稳定，第一名烧穿前不会抖动切换）；
        - 没查过余额（或脏数据）→ 未知，排在已知达标之后、按拖拽顺序——
          不查余额就纯按拖拽优先级用，查过余额才启用智能降级；
        - 已知且低于阈值 → 跳过；
        - 全部低于阈值 → 取已知积分最高者继续用（最耐用），服务不断供；
          下次「查询余额」刷新缓存后自动恢复正常的降级节奏。
        """
        thr = self._priority_threshold()
        if thr <= 0:
            return ordered[0]
        cache = self._credits_cache()
        known: list[tuple[float, int, Document]] = []      # (积分, 拖拽序, 账号)
        unknown: list[Document] = []
        for i, d in enumerate(ordered):
            rec = cache.get(d.uid)
            total = rec.get("total") if isinstance(rec, dict) else None
            if isinstance(total, (int, float)) and not isinstance(total, bool):
                known.append((float(total), i, d))
            else:
                unknown.append(d)
        ok = [t for t in known if t[0] >= thr]
        if ok:
            return max(ok, key=lambda t: (t[0], -t[1]))[2]  # 达标里积分最高
        if unknown:
            return unknown[0]                               # 无达标已知号 → 拖拽序第一个未知
        if known:
            return max(known, key=lambda t: (t[0], -t[1]))[2]  # 全烧穿 → 积分最高者顶住
        return ordered[0]

    def _sweep_sessions(self, now: float) -> None:
        for s in [s for s, (_, at) in self.session_map.items() if now - at > SESSION_TTL]:
            del self.session_map[s]

    def _unbind(self, session: str | None, k: tuple[str, str]) -> None:
        """会话绑定的账号失败时解除粘性，让该对话下次重新分配。"""
        if session and self.session_map.get(session, (None,))[0] == k:
            self.session_map.pop(session, None)

    def pick(self, model: str, exclude: set | None = None,
             session: str | None = None, lane: str | None = "workbuddy") -> Document | None:
        """按分配策略选择健康账号；session 给定时优先粘住上次用的号。

        lane 限定泳道（workbuddy/cline）；None 表示不限（仅目录枚举用）。
        """
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
                if doc is not None and k not in exclude and self._is_usable(doc, model, now, lane=lane):
                    self.session_map[session] = (k, now)
                    self.last_used[k] = now
                    return doc
                self.session_map.pop(session, None)   # 号废了/被排除 → 重新分配

        candidates = [d for d in self.documents.values()
                      if _key(d) not in exclude and self._is_usable(d, model, now, lane=lane)]
        if not candidates:
            return None

        strategy = self._distribution()
        if strategy == "priority":
            candidates = self._priority_sorted(candidates)
            # 拖拽顺序 + 积分感知：固定优先账号，低于阈值自动降到下一个；
            # 全部低于阈值回退队首（服务不断供），下次「查询余额」后自动恢复
            doc = self._pick_by_credits(candidates)
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

    def _shortest_cooling(self, model: str, lane: str | None = "workbuddy") -> float | None:
        """该模型全池冷却时，距离最早解冻还剩多少秒（只算泳道内活着的号）。"""
        now = time.time()
        waits = [self.cooldowns.get(_key(d), model) - now
                 for d in self.documents.values()
                 if "chat" in d.capabilities and d.api_ready
                 and (lane is None or _lane_of(d) == lane)
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
        elif error.code == "upstream_quota":
            self.cooldowns.set(k, model, 1800)       # 额度尽是持久状态：冷却 30 分钟
        elif error.kind in ("rate", "model", "channel"):
            self.cooldowns.set(k, model, error.retry_after or DEFAULT_MODEL_COOLDOWN)
        elif error.upstream_status >= 500 or error.code == "upstream_transport_error":
            self.cooldowns.set(k, model, 60)         # 上游侧故障非账号之过：短冷却轻罚

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

    @staticmethod
    def _should_failover(exc: "UpstreamError") -> bool:
        """该错误是否值得换号再试。

        * 账号侧问题（凭据失效/限流/额度尽）→ 换号；
        * 上游 5xx 与连接中断 → 也换号：可能只是单账号会话态引发的故障，
          换号循环有 tried 上限（每号只试一次），不会雪崩；
        * 404 模型不存在 → 不换号：同池账号模型集相同，一个号说没有其他号
          大概率也没有，直接原样抛给客户端；
        * 请求本身的问题（400 参数错、渠道审查拒绝且对策二已试过）→ 换号无意义。
        """
        if exc.kind in ("auth", "rate"):
            return True
        if exc.code == "upstream_quota":
            return True
        return exc.code == "upstream_transport_error" or exc.upstream_status >= 500

    def chat_failover(self, payload: dict, session: str | None = None,
                      lane: str = "workbuddy"):
        """带自动换号的对话。返回 (document, response)。

        生成器场景下 response 由调用方负责 close()。
        换号之外的两层兜底（对齐 dsh-xdpool，仅 WorkBuddy 泳道适用）：
        * 免费行全冷却 → 试同名付费行（FALLBACK_ROWS）
        * 全池冷却 → 等最早的冷却结束再试一次（≤ MAX_COOLING_WAIT 秒）
        """
        model = str(payload.get("model", ""))
        tried: set = set()
        last_error: UpstreamError | None = None
        waited = False
        while True:
            doc = self.pick(model, exclude=tried, session=session, lane=lane)
            if doc is None:
                # 兜底一：免费行冷却时降级试付费行（换行名重试，客户端无感）
                fb = FALLBACK_ROWS.get(model) if lane == "workbuddy" else None
                if fb:
                    alt = self.pick(fb, exclude=tried, session=None, lane=lane)
                    if alt is not None:
                        try:
                            return alt, self.clients[_key(alt)].chat({**payload, "model": fb})
                        except UpstreamError as exc:
                            last_error = exc
                            self.apply_error(alt, exc, fb)
                            self._unbind(session, _key(alt))
                            if not self._should_failover(exc):
                                raise
                            continue
                # 兜底二：全池冷却 → 等最早的解冻（一次，≤90 秒）
                if not waited:
                    wait = self._shortest_cooling(model, lane=lane)
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
                self._unbind(session, _key(doc))  # 粘住的号出问题 → 解绑让对话重分配
                if not self._should_failover(exc):
                    raise             # 请求本身的问题（参数错/渠道审查），换号无意义
                if exc.kind == "auth":
                    self._emit(f"「{doc.label()}」凭据被上游拒绝（401），已标记失效并换号重试")
                elif exc.code == "upstream_quota":
                    self._emit(f"「{doc.label()}」额度已用尽，已自动换号")
                else:
                    self._emit(f"「{doc.label()}」{model} 触发限流或上游故障"
                               f"（HTTP {exc.upstream_status or exc.status}），已自动换号")
                continue              # 换号重试

    @staticmethod
    def _route_id(doc: Document, row_id: str) -> str:
        """模型的对外调用 ID：WorkBuddy 系带 ``workbuddy:``，Cline 带 ``cline:``。"""
        lane = _lane_of(doc)
        return f"workbuddy:{row_id}" if lane == "workbuddy" else f"{lane}:{row_id}"

    def models(self) -> list[dict]:
        """聚合全部账号的模型目录（去重，跨渠道）。统一 ``前缀:`` 命名——
        客户端里一眼可辨路由来源；区域信息在 region 字段（WorkBuddy 泳道
        路由时本就不分区域，cn/global 同名模型自然合并）。"""
        seen: dict[str, dict] = {}
        for _doc, rows in self._models_by_account_raw():
            for row in rows:                      # rows 已带 workbuddy:/cline: 前缀
                seen.setdefault(row["id"], row)
        return list(seen.values())

    def _models_by_account_raw(self, ttl: int = 300) -> list[tuple]:
        """（账号, 该账号模型目录）明细；带 TTL 缓存，单账号失败不拦整体。"""
        now = time.time()
        cache = getattr(self, "_mba_cache", None)
        if cache is not None and now - getattr(self, "_mba_at", 0) < ttl:
            return cache
        out: list[tuple] = []
        for doc in self.healthy(lane=None):
            try:
                rows = self.clients[_key(doc)].models()
            except Exception:
                rows = []
            out.append((doc, [{**r, "id": self._route_id(doc, r["id"])} for r in rows]))
        self._mba_cache, self._mba_at = out, now
        return out

    def models_by_account(self, ttl: int = 300) -> list[dict]:
        """逐账号的可用模型索引（网页「我的模型」页）。

        单账号目录拉取失败记 0 个模型，不挡其他账号；整表缓存 5 分钟，
        避免每次打开页面都打一轮上游目录接口。
        """
        return [{
            "uid": doc.uid, "label": doc.label(), "region": doc.region,
            "channel": doc.channel_name, "channel_id": doc.channel_id,
            "count": len(rows), "models": rows,
        } for doc, rows in self._models_by_account_raw(ttl)]

    # -- 后台 token 自动刷新 -----------------------------------------------------

    def start_auto_refresh(self, interval_sec: int = 300) -> threading.Thread:
        """后台循环：凭据临期（<10 分钟）或已过期即用 refresh_token 续期。

        线程可经 stop_auto_refresh() 优雅停止（等待用 Event 替代 sleep，
        测试与热重启都能确定性收线，不用再靠杀线程）。
        """
        stop = threading.Event()
        self._refresh_stop = stop

        def worker():  # pragma: no cover
            import json as _json
            from .store import save_account
            while not stop.is_set():
                for k, doc in list(self.documents.items()):
                    if "refresh" not in doc.capabilities or not doc.refresh_token or doc.protected_token:
                        continue             # 渠道未声明 refresh 能力 → 不代为续期
                    exp = doc.token_expires_at
                    if exp is not None and not isinstance(exp, (int, float)):
                        continue             # 脏数据（如上游给了字符串到期时间）不代续期
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
                            nickname=doc.nickname, region=doc.region, domain=doc.domain,
                            # 到期时间必须归一化为毫秒整数——上游若返回字符串
                            # 原样塞进 Document，会让全池 api_ready 比较炸 TypeError
                            token_expires_at=_token_expiry(auth))
                        save_account(fresh)
                        self.refresh_documents([fresh])
                        self._emit(f"「{fresh.label()}」token 已自动续期")
                    except Exception as exc:
                        self._emit(f"「{doc.label()}」token 自动续期失败：{exc}（下轮重试，多次失败请重新登录）")
                stop.wait(interval_sec)               # 可中断的 sleep

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def stop_auto_refresh(self) -> None:
        """停止后台续期线程（下轮等待立即返回，循环收尾退出）。"""
        stop = getattr(self, "_refresh_stop", None)
        if stop is not None:
            stop.set()
