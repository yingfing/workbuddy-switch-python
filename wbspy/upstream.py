"""WorkBuddy 上游协议客户端（对话 / 积分 / 签到 / 旅行 / 任务 / 刷新）。

协议要点（提炼自 koi128bit/WorkBuddy-Switch 的 api-upstream.mjs、quota.mjs、
rewards.mjs、growth-tasks.mjs，均为实测验证的 wire 行为）：

* 对话走 ``POST /v2/chat/completions``；请求头缺一不可：X-Agent-Purpose、
  X-Conversation-Request-ID、X-Conversation-Message-ID，缺任一回 11128。
* 上游按首条 system 消息开头做「渠道锚点」检查，识别第三方 agent 指纹即拒
  （11128）。对策一：前置中性导语；对策二：被拒后把 system 原文降级为 user。
* 错误分类：12153→凭据失效；429 族→限流（带 Retry-After）；11128/403→渠道
  拒绝（可重定位重试）；11102/11158/model_not_found→模型不可用。
* 积分：``/billing/meter/get-user-resource-summary`` → 404 回退
  ``/v2/billing/meter/get-user-resource``。
* 签到：``checkin-activity-status`` → 回退 ``checkin-status`` → ``daily-checkin``。
* 旅行：``/activity/growth/buddy/travel/{config,status,depart,claim}``。
* 任务：``GET /v2/activity/growth/tasks``、``POST /activity/growth/tasks/accept``、
  ``POST /activity/growth/tasks/{code}/claim``。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Iterable

import requests

from .document import Document

TIMEOUT = 15
STREAM_TIMEOUT = (15, 600)
UA = "WorkBuddy/5.8.3 wbspy"

# chat 请求允许透传的字段白名单
PAYLOAD_FIELDS = (
    "model", "messages", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "temperature", "top_p", "tools", "tool_choice",
    "parallel_tool_calls", "response_format", "frequency_penalty",
    "presence_penalty", "stop", "seed", "reasoning_effort", "thinking",
    "conversation_id", "user", "n", "metadata", "store", "service_tier",
    "prompt_cache_key",
)

# 11128 对策一：中性导语，把第三方指纹挪离首条 system 的锚点
CHANNEL_LEAD_IN = "You are WorkBuddy, a helpful AI assistant. Follow the instructions below."

# 第三方 agent 自我介绍族指纹
CHANNEL_FINGERPRINTS = (
    re.compile(r"^x-anthropic-billing-header\s*:", re.I),
    re.compile(r"^You are (?:[A-Z][\w.'-]* ){1,3}(?:Code|CLI)\b"),
    re.compile(
        r"^You are an? [^.\n]{0,80}\b(?:agent|assistant|CLI)\b[^.\n]{0,60}"
        r"\b(?:powered by|built on|running in|running on|based on)\b", re.I),
    re.compile(
        r"^You are an? [^.\n]{0,80}\b(?:agent|assistant|CLI)\b[^.\n]{0,40}\bofficial CLI\b",
        re.I),
)
RELOCATION_NOTICE = "以下是本次会话的系统说明，请原样遵守：\n\n"

# embeddings：官方只认这两个 id；单条输入超 8188 字符必 413/11353
EMBEDDING_MODELS = (
    {"id": "bge-m3", "name": "BGE-M3", "dimensions": 1024},
    {"id": "hunyuan-embedding", "name": "Hunyuan Embedding", "dimensions": 1024},
)
EMBEDDING_INPUT_CHARS = 8188

NOT_FOUND = {"__not_found__": True}


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class UpstreamError(Exception):
    """本地可分类的上游失败。"""

    def __init__(self, status: int, code: str, message: str, retry_after: int = 0,
                 upstream_status: int = 0, upstream_code: str = ""):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retry_after = max(0, int(retry_after))
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code

    @property
    def kind(self) -> str:
        return {
            "upstream_auth_expired": "auth",
            "credential_required": "auth",
            "upstream_rate_limit": "rate",
            "model_not_available": "model",
            "upstream_access_denied": "channel",
        }.get(self.code, "server")


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )
    return ""


def is_third_party_opening(value: str) -> bool:
    return any(p.match(value) for p in CHANNEL_FINGERPRINTS)


def apply_channel_lead_in(messages: list[dict]) -> list[dict]:
    """对策一：首条 system 若以第三方指纹开头，前置中性导语。"""
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            text = _system_text(msg.get("content"))
            if not is_third_party_opening(text):
                return messages
            if isinstance(msg["content"], str):
                lead = {**msg, "content": f"{CHANNEL_LEAD_IN}\n\n{text}"}
            else:
                lead = {**msg, "content": [
                    {"type": "text", "text": CHANNEL_LEAD_IN}, *msg["content"]]}
            return messages[:i] + [lead] + messages[i + 1:]
    return messages


def relocate_system_role(payload: dict) -> dict | None:
    """对策二：system 原文降级为 user 回合（原样字节、只换角色）。"""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    index = next((i for i, m in enumerate(messages) if m.get("role") == "system"), -1)
    if index < 0:
        return None
    text = _system_text(messages[index].get("content")).strip()
    if not text:
        return None
    rest = [m for i, m in enumerate(messages) if i != index]
    return {
        **payload,
        "messages": [
            {"role": "system", "content": CHANNEL_LEAD_IN},
            {"role": "user", "content": RELOCATION_NOTICE + text},
            *rest,
        ],
    }


def normalize_payload(raw: dict) -> dict:
    """白名单过滤 + developer→system + tool_choice 压平 + 强制流式。"""
    if not isinstance(raw, dict):
        raise UpstreamError(400, "invalid_request", "请求须为 JSON 对象。")
    payload = {k: v for k, v in raw.items() if k in PAYLOAD_FIELDS and v is not None}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise UpstreamError(400, "invalid_request", "messages 不能为空。")
    if len(messages) > 20000:
        raise UpstreamError(400, "invalid_request", "messages 数量超出上游上限（20000）。")

    fixed: list[dict] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "developer":
            fixed.append({**msg, "role": "system"})
        else:
            fixed.append(msg)
    payload["messages"] = apply_channel_lead_in(fixed)

    if isinstance(payload.get("tool_choice"), dict):
        payload["tool_choice"] = payload["tool_choice"].get("function", {}).get("name", "auto")
    payload["stream"] = True
    return payload


def classify_business_error(status: int, body: str, retry_header: str = "",
                            content_type: str = "") -> UpstreamError:
    """上游响应 → 本地可分类错误（对应原版 businessError）。"""
    code = ""
    try:
        value = json.loads(body)
        err = value.get("error") if isinstance(value, dict) else None
        raw_code = (err.get("code") if isinstance(err, dict) else None) or \
                   (value.get("code") if isinstance(value, dict) else None)
        code = str(raw_code or "")
    except (json.JSONDecodeError, AttributeError):
        pass
    upstream_code = code if re.fullmatch(r"\d{1,8}", code) else ""
    retry = min(3600, max(1, int(retry_header))) if (retry_header or "").isdigit() else 60
    official = f"（官方代码 {upstream_code}）" if upstream_code else ""

    if code == "12153":
        return UpstreamError(401, "upstream_auth_expired", "登录凭据已失效，请重新登录授权。",
                             upstream_status=status, upstream_code=upstream_code)
    if status == 429 or code in ("11101", "11105", "11129", "11306", "11308", "rate_limit", "quota_exceeded"):
        return UpstreamError(429, "upstream_rate_limit",
                             f"上游模型使用量或请求频率已达到限制{official}。",
                             retry_after=retry, upstream_status=status, upstream_code=upstream_code)
    if code == "11128" or status == 403:
        return UpstreamError(403, "upstream_access_denied",
                             f"上游渠道检查拒绝本次请求{official}。",
                             retry_after=retry, upstream_status=status, upstream_code=upstream_code)
    if status == 404 or code in ("11102", "11134", "11158", "model_not_found", "unsupported_model"):
        return UpstreamError(404, "model_not_available", f"该账号暂不能使用所选模型{official}。",
                             retry_after=60, upstream_status=status, upstream_code=upstream_code)
    local = 400 if 400 <= status < 500 else 502
    return UpstreamError(local, "upstream_error",
                         f"官方服务未完成本次请求（HTTP {status}{official}）。",
                         upstream_status=status, upstream_code=upstream_code)


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class WorkBuddyClient:
    """单个凭据文档对应的上游客户端。"""

    def __init__(self, document: Document, session: requests.Session | None = None):
        self.doc = document
        self.http = session or requests.Session()

    # -- 通用请求 -----------------------------------------------------------

    def _meter_headers(self, referer: str = "/profile/plans-usage") -> dict:
        origin = self.doc.api
        return {
            "Authorization": f"Bearer {self.doc.token}",
            "Content-Type": "application/json",
            "X-Client-Platform": "web",
            "X-Domain": origin.split("//", 1)[1],
            "Origin": origin,
            "Referer": f"{origin}{referer}",
            "User-Agent": "WorkBuddy",
        }

    def _reward_headers(self) -> dict:
        origin = self.doc.origin
        headers = {
            "Authorization": f"Bearer {self.doc.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-User-Id": self.doc.uid,
            "X-Domain": origin.split("//", 1)[1],
            "X-Client-Platform": "web",
            "Origin": origin,
            "Referer": f"{origin}/profile/growth-center",
            "User-Agent": UA,
        }
        tenant = (self.doc.raw.get("account") or {}).get("enterpriseId") or \
                 (self.doc.raw.get("account") or {}).get("enterprise_id")
        if isinstance(tenant, (str, int)) and re.fullmatch(r"[\w-]{1,200}", str(tenant)):
            headers["X-Enterprise-Id"] = str(tenant)
            headers["X-Tenant-Id"] = str(tenant)
        return headers

    def _reward_request(self, endpoint: str, method: str = "POST",
                        body: Any = None) -> tuple[Any, bool]:
        """返回 (data, already)。already=True 表示服务端判定重复操作。"""
        if not self.doc.token:
            raise UpstreamError(401, "credential_required",
                                "此账号使用本机加密凭据，请先网页登录授权（wbspy login）。")
        resp = self.http.request(
            method, f"{self.doc.origin}{endpoint}",
            headers=self._reward_headers(),
            data=json.dumps(body) if method == "POST" else None,
            timeout=TIMEOUT, allow_redirects=False,
        )
        if resp.status_code in (401, 403):
            raise UpstreamError(401, "upstream_auth_expired", "登录授权已失效，请重新授权。")
        if resp.status_code == 404:
            raise UpstreamError(404, "endpoint_missing", "当前账号的活动接口暂不支持。")
        if resp.status_code >= 400:
            raise UpstreamError(502, "upstream_error", f"活动服务暂不可用（HTTP {resp.status_code}）。")
        try:
            result = resp.json()
        except ValueError as exc:
            raise UpstreamError(502, "upstream_error", "活动服务返回了无法解析的数据。") from exc
        code = result.get("code")
        if code in (0, 200, "0", "200"):
            return result.get("data"), False
        if code in (401, 403, 12153, "401", "403", "12153"):
            raise UpstreamError(401, "upstream_auth_expired", "登录授权已失效，请重新授权。")
        detail = str(result.get("message") or result.get("msg") or "")[:180]
        already = endpoint.endswith("/daily-checkin") and bool(
            re.search(r"已签到|already\s+(checked|signed)|repeat", detail, re.I))
        if already:
            return None, True
        raise UpstreamError(502, "service_rejected", detail or "活动服务返回了未知的结果。")

    @staticmethod
    def _not_found(reply: dict) -> bool:
        return isinstance(reply, dict) and reply.get("__not_found__") is True

    # -- 积分 ---------------------------------------------------------------

    def quota(self) -> dict:
        """可用积分汇总。新客户端接口 404 时回退旧接口。"""
        if not self.doc.token:
            raise UpstreamError(401, "credential_required",
                                "此登录凭据由客户端加密，请先网页登录授权。")
        origin = self.doc.api
        summary = self.http.post(
            f"{origin}/billing/meter/get-user-resource-summary",
            headers=self._meter_headers(), data=json.dumps({"ProductCode": "p_tcaca"}),
            timeout=TIMEOUT, allow_redirects=False,
        )
        if summary.status_code in (401, 403):
            raise UpstreamError(401, "upstream_auth_expired", "登录凭据已失效，请重新登录。")
        if summary.status_code != 404 and summary.ok:
            return self._parse_quota(summary.json())
        legacy = self.http.post(
            f"{origin}/v2/billing/meter/get-user-resource",
            headers=self._meter_headers(),
            data=json.dumps({"ProductCode": "p_tcaca", "PageNumber": 1,
                             "PageSize": 100, "Status": [0, 3]}),
            timeout=TIMEOUT, allow_redirects=False,
        )
        if legacy.status_code in (401, 403):
            raise UpstreamError(401, "upstream_auth_expired", "登录凭据已失效，请重新登录。")
        if legacy.status_code == 404:
            raise UpstreamError(404, "endpoint_missing", "当前 WorkBuddy 版本的积分接口暂不支持。")
        if not legacy.ok:
            raise UpstreamError(502, "upstream_error", f"积分服务暂不可用（HTTP {legacy.status_code}）。")
        return self._parse_quota(legacy.json())

    @staticmethod
    def _parse_quota(data: dict) -> dict:
        code = data.get("code", data.get("data", {}).get("code") if isinstance(data.get("data"), dict) else None)
        if code is not None and code not in (0, 200, "0", "200"):
            if code in (401, 403, 12153, "401", "403", "12153"):
                raise UpstreamError(401, "upstream_auth_expired", "登录凭据已失效，请重新登录。")
            raise UpstreamError(502, "service_rejected", f"积分服务拒绝请求（{str(code)[:12]}）。")
        return data.get("data", data)

    # -- token 续期 ----------------------------------------------------------

    def refresh(self) -> dict:
        """用 X-Refresh-Token 头续期，返回新 auth 字段。"""
        refresh = self.doc.refresh_token
        if not refresh:
            raise UpstreamError(401, "credential_required", "登录授权已过期，请重新网页授权。")
        origin = self.doc.api
        resp = self.http.post(
            f"{origin}/v2/plugin/auth/token/refresh",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.doc.token}",
                "X-Refresh-Token": refresh,
                "X-Auth-Refresh-Source": "plugin",
                "X-Domain": origin.split("//", 1)[1],
            }, data="{}", timeout=TIMEOUT, allow_redirects=False,
        )
        if not resp.ok:
            raise UpstreamError(401, "upstream_auth_expired", "登录续期失败，请重新网页授权。")
        data = resp.json()
        token = (data.get("data") or {}).get("accessToken") or (data.get("data") or {}).get("access_token")
        if data.get("code") not in (0, 200, "0", "200") or not token:
            raise UpstreamError(401, "upstream_auth_expired", "登录续期失败，请重新网页授权。")
        inner = data["data"]
        return {
            **inner,
            "accessToken": token,
            "refreshToken": inner.get("refreshToken") or inner.get("refresh_token") or refresh,
            "expiresAt": inner.get("expiresAt") or (
                int(time.time() * 1000 + float(inner["expiresIn"]) * 1000)
                if inner.get("expiresIn") else None),
        }

    # -- 签到 -----------------------------------------------------------------

    def checkin_status(self) -> dict:
        """今日是否已签。activity-status 404 时回退 checkin-status。"""
        try:
            data, _ = self._reward_request("/v2/billing/meter/checkin-activity-status")
        except UpstreamError as exc:
            if exc.code != "endpoint_missing":
                raise
            data, _ = self._reward_request("/v2/billing/meter/checkin-status")
        if data and (data.get("enabled") is False or data.get("active") is False):
            raise UpstreamError(502, "inactive", "当前账号的签到活动尚未开放。")
        checked = bool(data and (data.get("today_checked_in") if data.get("today_checked_in") is not None
                                 else data.get("todayCheckedIn")))
        return {"checked_in": checked}

    def checkin(self) -> dict:
        data, already = self._reward_request("/v2/billing/meter/daily-checkin", body={})
        credit = None
        if not already and isinstance(data, dict):
            for key in ("credit", "reward_credit", "points"):
                if isinstance(data.get(key), (int, float)):
                    credit = data[key]
                    break
        return {"already": already, "credit": credit}

    # -- 猫猫旅行 --------------------------------------------------------------

    TRAVEL = "/activity/growth/buddy/travel"

    def travel_config(self) -> dict:
        data, _ = self._reward_request(f"{self.TRAVEL}/config", method="GET")
        if not isinstance(data, dict) or data.get("enabled") is False:
            return {"enabled": False, "locations": []}
        locations = [
            {"id": loc.get("id"), "name": str(loc.get("name", ""))[:80]}
            for loc in (data.get("locations") or [])[:200]
            if isinstance(loc, dict) and loc.get("id") is not None
        ]
        return {"enabled": bool(locations), "locations": locations}

    def travel_status(self) -> dict:
        data, _ = self._reward_request(f"{self.TRAVEL}/status", method="GET")
        if not isinstance(data, dict) or data.get("state") not in ("idle", "traveling", "arrived"):
            raise UpstreamError(502, "upstream_error", "旅行服务返回了未知状态。")
        return {
            "state": data["state"],
            "location_name": str((data.get("location") or {}).get("name", ""))[:80],
            "record_id": int(data["record_id"]) if str(data.get("record_id", "")).isdigit() else None,
            "daily_limit_reached": data.get("daily_limit_reached") is True,
            "reward_credit": data.get("credit", data.get("reward_credit")),
        }

    def travel_depart(self, location_id) -> None:
        """出发。地点奖励相同，缺省 4（古镇客栈，XD Pool 实测结论）。"""
        self._reward_request(f"{self.TRAVEL}/depart", body={"location_id": location_id})

    def travel_claim(self, record_id: int | None = None) -> dict:
        """领取到达奖励。上游要求 record_id（缺了会被拒），来自 status 读取。"""
        body = {"record_id": record_id} if record_id is not None else {}
        data, _ = self._reward_request(f"{self.TRAVEL}/claim", body=body)
        credit = None
        if isinstance(data, dict):
            credit = data.get("credit", data.get("reward_credit"))
        return {"credit": credit}

    # -- 猫猫领养（未领养则旅行不可用）-----------------------------------------

    def buddy_ensure(self) -> str:
        """确保有猫猫：info → (agreement) → adopt first。返回状态说明。"""
        data, _ = self._reward_request("/activity/growth/buddy/info", method="GET")
        buddy = (data or {}).get("buddy") if isinstance(data, dict) else None
        if isinstance(buddy, dict) and buddy:
            return f"已有猫猫：{buddy.get('name', '')}"
        # 未领养：同意协议 → 领养（需当日有活跃，失败视为"还没好"）
        try:
            self._reward_request("/activity/growth/buddy/agreement", body={"agree": True})
            self._reward_request("/activity/growth/buddy/first", body={})
            return "已领养第一只猫猫"
        except UpstreamError as exc:
            if exc.status == 400:
                return "今日活跃不足，领养任务未完成（明天再试）"
            raise

    # -- 连登兑换（streak tiers）-------------------------------------------------

    def streak_full(self) -> dict:
        """连登全貌：天数、各档位解锁与奖励、补签卡余额。

        注意：路径无 /v2 前缀；计数在 data.streak.days（嵌套）。
        """
        data, _ = self._reward_request("/activity/growth/streak", method="GET")
        data = data or {}
        streak = data.get("streak") or {}
        redemption = data.get("redemption_status") or {}
        tiers = []
        for entry in redemption.get("tiers") or []:
            if not isinstance(entry, dict) or not entry.get("tier"):
                continue
            name = entry["tier"]
            tiers.append({
                "tier": name, "days": entry.get("days"),
                "credit": entry.get("credit"), "energy": entry.get("energy"),
                # 解锁状态在 redemption 的扁平字段 tier_<name>_status
                "status": str(redemption.get(f"tier_{name}_status", "")),
            })
        makeup = data.get("makeup_cards") or {}
        return {
            "days": streak.get("days", 0),
            "month_total_days": streak.get("month_total_days", 0),
            "next_tier": streak.get("next_tier", ""),
            "next_tier_remaining": streak.get("next_tier_remaining"),
            "makeup_cards": makeup.get("balance", 0),
            "tiers": tiers,
        }

    def redeem_streak_available(self) -> list[str]:
        """兑换全部已解锁未领取的档位，返回已兑换档位名。"""
        info = self.streak_full()
        redeemed = []
        for tier in info["tiers"]:
            if tier["status"] == "claimed" or tier["status"] == "":
                continue
            if not tier["status"].endswith("unclaimed") and tier["status"] not in ("unlocked", "redeemable"):
                continue
            try:
                self._reward_request("/activity/growth/redeem",
                                     body={"tier": tier["tier"],
                                           "client_token": str(uuid.uuid4())})
                redeemed.append(tier["tier"])
            except UpstreamError as exc:
                if exc.status == 403:      # 档位未解锁（连续登录天数不足）
                    continue
                raise
        return redeemed

    # -- 幸运抽奖 ------------------------------------------------------------------

    def lottery_chances(self) -> int:
        data, _ = self._reward_request("/activity/growth/lottery/summary", method="GET")
        return int((data or {}).get("chances") or 0)

    def lottery_draw_all(self) -> list[dict]:
        """抽完当前全部次数，返回奖品列表（形状随活动，原样透传）。"""
        prizes = []
        for _ in range(20):                      # 单日保护上限
            chances = self.lottery_chances()
            if chances <= 0:
                break
            data, _ = self._reward_request("/activity/growth/lottery/draw",
                                           body={"client_token": str(uuid.uuid4())})
            prizes.append(data)
        return prizes

    # -- 补签卡（活跃度热力图断签补救）----------------------------------------------

    def heatmap_missed_yesterday(self) -> str | None:
        """昨日是否断签，返回日期 key（可补签）或 None。"""
        data, _ = self._reward_request("/activity/growth/heatmap", method="GET")
        cells = (data or {}).get("cells")
        if not isinstance(cells, list):
            return None
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        for cell in cells:
            if isinstance(cell, dict) and cell.get("date") == yesterday:
                return yesterday if not cell.get("score") else None
        return None

    def use_makeup_card(self, date: str) -> None:
        """用补签卡填补某日（已填日期幂等）。"""
        self._reward_request("/activity/growth/makeup-cards/use", body={"date": date})

    # -- 成长任务 ----------------------------------------------------------------

    def growth_tasks(self) -> list[dict]:
        data, _ = self._reward_request("/v2/activity/growth/tasks", method="GET")
        tasks = data.get("tasks") if isinstance(data, dict) else data
        out = []
        for raw in tasks or []:
            if not isinstance(raw, dict) or not raw.get("task_code"):
                continue
            status = str(raw.get("status", ""))
            claimed = raw.get("claimed") is True or status == "claimed"
            locked = raw.get("locked") is True
            progress = raw.get("progress") or {}
            out.append({
                "code": raw["task_code"],
                "title": str(raw.get("title") or raw["task_code"])[:120],
                "status": status,
                "claimed": claimed,
                "locked": locked,
                "claimable": not claimed and not locked and (
                    raw.get("claimable") is True or
                    (raw.get("claimable") is not False and status == "completed")),
                "reward_credit": raw.get("reward_credit"),
            })
        return out

    def accept_tasks(self, codes: Iterable[str]) -> None:
        selected = list(dict.fromkeys(c.strip() for c in codes if c.strip()))[:200]
        if not selected:
            raise UpstreamError(400, "invalid_request", "成长任务选择无效。")
        self._reward_request("/activity/growth/tasks/accept", body={"task_codes": selected})

    def claim_task(self, code: str) -> dict:
        data, _ = self._reward_request(f"/activity/growth/tasks/{code}/claim", body={})
        already = bool(isinstance(data, dict) and data.get("already_claimed"))
        return {"already": already,
                "credit": None if already else (data or {}).get("credit", (data or {}).get("reward_credit"))}

    # -- 模型目录 -----------------------------------------------------------------

    def _catalog_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.doc.token}",
                "Accept": "application/json",
                "Origin": self.doc.origin, "Referer": f"{self.doc.origin}/",
                "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"}

    def _parse_catalog(self, payload: dict) -> list[dict]:
        """解析目录响应。global 的 /v3/config 走 availableModels 过滤（对应
        WorkBuddy-Switch api-upstream.mjs 的 catalog 逻辑）。"""
        data = payload.get("data")
        if isinstance(data, dict):
            catalog = data.get("models")
            allowed = data.get("availableModels")
        else:
            catalog, allowed = data, None
        if not isinstance(catalog, list):
            return []
        allowed_ids = None
        if isinstance(allowed, list):
            allowed_ids = {v if isinstance(v, str) else v.get("id")
                           for v in allowed if isinstance(v, (str, dict))}
        out, seen = [], set()
        for row in catalog:
            if not isinstance(row, dict):
                continue
            mid = row.get("model") or row.get("id") or row.get("name")
            if not (isinstance(mid, str) and mid) or mid in seen:
                continue
            if row.get("disabled") or row.get("enabled") is False:
                continue
            if allowed_ids is not None and mid not in allowed_ids:
                continue
            seen.add(mid)
            out.append({"id": mid, "name": str(row.get("display_name") or row.get("name") or mid)[:80],
                        "region": self.doc.region})
        return out

    def models(self) -> list[dict]:
        if not self.doc.token:
            raise UpstreamError(401, "credential_required", "请先完成网页登录授权。")
        # 区域化目录（对齐 WorkBuddy-Switch）：global 的 personal 端点只服务旧
        # 别名目录，优先产品配置源 /v3/config，失败回落 personal/models；
        # CN 反之（personal/models 已实战校准：只认 GET + CLI UA）。
        routes = (["/v3/config", "/v2/enterprises/personal/models"]
                  if self.doc.region == "global"
                  else ["/v2/enterprises/personal/models", "/v3/config"])
        last_status = 0
        for route in routes:
            resp = self.http.get(
                f"{self.doc.api}{route}", headers=self._catalog_headers(),
                timeout=TIMEOUT, allow_redirects=False,
            )
            last_status = resp.status_code
            if resp.status_code in (401, 403):
                raise UpstreamError(401, "upstream_auth_expired", "登录凭据已失效。")
            if resp.ok:
                try:
                    rows = self._parse_catalog(resp.json())
                except ValueError:
                    rows = []
                if rows:
                    return rows
        raise UpstreamError(502, "upstream_error",
                            f"模型目录暂不可用（HTTP {last_status}）。")

    # -- 对话 -----------------------------------------------------------------

    def _chat_headers(self, conversation_id: str) -> dict:
        return {
            "Authorization": f"Bearer {self.doc.token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": UA,
            "X-User-Id": self.doc.uid,
            "X-Domain": self.doc.origin.split("//", 1)[1],
            "X-Product": "SaaS",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Version": "5.8.3",
            "X-Requested-With": "XMLHttpRequest",
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-Agent-Purpose": "conversation",
            "X-Conversation-ID": conversation_id,
            "X-Conversation-Request-ID": str(uuid.uuid4()),
            "X-Conversation-Message-ID": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4()),
        }

    def chat(self, payload: dict, conversation_id: str | None = None) -> requests.Response:
        """发起点对话。返回原始 requests.Response（流式 SSE 由调用方消费）。

        11128 渠道拒绝时自动执行对策二（system 降级 user）重试一次。
        """
        if not self.doc.token:
            raise UpstreamError(401, "credential_required", "请先完成网页登录授权。")
        conv = conversation_id or str(uuid.uuid4())
        url = f"{self.doc.origin}/v2/chat/completions"
        # 国际版上游要求请求必须含 system 消息（对齐 WorkBuddy-Switch）。
        if self.doc.region == "global" and not any(
                m.get("role") == "system" for m in payload.get("messages", [])):
            payload = {**payload, "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                *payload.get("messages", []),
            ]}
        body = json.dumps(normalize_payload(payload))

        def send(payload_text: str) -> requests.Response:
            try:
                resp = self.http.post(
                    url, headers=self._chat_headers(conv), data=payload_text,
                    timeout=STREAM_TIMEOUT, allow_redirects=False, stream=True,
                )
            except requests.RequestException as exc:
                raise UpstreamError(502, "upstream_transport_error",
                                    "对话连接中断，结果和消耗尚未确认，不会自动重发。") from exc
            return resp

        resp = send(body)
        if not resp.ok:
            error = classify_business_error(
                resp.status_code, resp.text[:1024 * 1024],
                resp.headers.get("Retry-After", ""), resp.headers.get("Content-Type", ""))
            resp.close()
            if error.code == "upstream_access_denied":
                relocated = relocate_system_role(json.loads(body))
                if relocated:
                    retry = send(json.dumps(relocated))
                    if retry.ok:
                        return retry
                    retry.close()
            raise error
        return resp

    def embeddings(self, model: str, input_items: list[str]) -> dict:
        if model not in {m["id"] for m in EMBEDDING_MODELS}:
            raise UpstreamError(404, "model_not_available", "该 embedding 模型不在官方支持列表。")
        if any(len(text) > EMBEDDING_INPUT_CHARS for text in input_items):
            raise UpstreamError(400, "invalid_request",
                                f"单条输入超过 {EMBEDDING_INPUT_CHARS} 字符。")
        resp = self.http.post(
            f"{self.doc.origin}/v2/embeddings",
            headers=self._chat_headers(str(uuid.uuid4())),
            data=json.dumps({"model": model, "input": input_items}),
            timeout=TIMEOUT, allow_redirects=False,
        )
        if not resp.ok:
            raise classify_business_error(resp.status_code, resp.text[:65536])
        return resp.json()
