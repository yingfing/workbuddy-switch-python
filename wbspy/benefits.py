"""多渠道积分福利客户端：TRAE / Qoder / MiniMax / LobsterAI 每日签到。

协议来源（2026-11 逆向核对，参照社区实现互证）：
- TRAE      https://api.trae.cn  /trae/api/v2/ug/checkin_credits/{status,claim}
            鉴权 Cloud-IDE-JWT；x-device-id 必带（缺了 claim 返回 9004）。
- Qoder     国内 https://openapi.qoder.com.cn、国际 https://openapi.qoder.sh
            /sash/api/v1/me/campaigns[/id/claim]；Cosy-* 客户端头；
            国际版服务端有严格设备风控（Cosy-MachineToken 需官方客户端组件），
            本实现只带派生 MachineId——国际版能领则领，被拒如实汇报。
- MiniMax   https://agent.minimaxi.com /minimax-cloud/api/v1/signin/{status,claim}
            query 固定参数 + yy / x-signature 双签名（md5）。
- LobsterAI https://lobsterai-server.youdao.com /api/client-activities/*
            slot → context → check_in → context 复核；统一信封 {code,msg,data}。

统一约定：客户端只做「查询→判定→领取→复核」，结果为人类可读文字；
HTTP 失败抛 UpstreamError 交给 automation._retry 统一重试/汇报。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from urllib.parse import quote

import requests

from .upstream import TIMEOUT, UpstreamError

UA_TRAE = "VSCode 1.107.1 (TRAE SOLO CN)"
UA_QODER = "Qoder"
UA_MINIMAX = "MiniMaxCode"
_QODER_SITES = {"qodercn": "https://openapi.qoder.com.cn", "qoder": "https://openapi.qoder.sh"}


def _stable_device(seed: str) -> str:
    """同账号恒定设备号（避免每次换 device 触发风控）。"""
    return hashlib.sha256(f"wbspy-device|{seed}".encode()).hexdigest()[:32]


def _json_or(resp: requests.Response) -> dict:
    if resp.status_code in (401, 403):
        raise UpstreamError(401, "upstream_auth_expired", "登录授权已失效，请重新授权。")
    if resp.status_code >= 400:
        raise UpstreamError(502, "upstream_error", f"活动服务暂不可用（HTTP {resp.status_code}）。")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise UpstreamError(502, "upstream_error", "活动服务返回了无法解析的数据。") from exc
    return doc if isinstance(doc, dict) else {}


def _bearer_headers(token: str, ua: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": ua}


# -- TRAE ---------------------------------------------------------------------

class TraeCheckin:
    """TRAE 每日签到（Cloud-IDE-JWT 即登录换票 token）。"""

    BASE = "https://api.trae.cn"
    PATH = "/trae/api/v2/ug/checkin_credits"

    def __init__(self, doc):
        self.doc = doc
        # 登录时落盘的设备号优先；老账号用 uid 派生，保持恒定。
        self.device_id = str((doc.raw.get("device_id") or "")
                             or _stable_device(f"trae|{doc.uid}"))

    def _post(self, action: str) -> dict:
        resp = requests.post(
            f"{self.BASE}{self.PATH}/{action}",
            json={"req_source": 1}, timeout=TIMEOUT,
            headers={"Authorization": f"Cloud-IDE-JWT {self.doc.token}",
                     "x-ide-token": self.doc.token, "x-device-id": self.device_id,
                     "Content-Type": "application/json", "User-Agent": UA_TRAE})
        return _json_or(resp)

    @staticmethod
    def _flags(doc: dict) -> dict:
        """status 载荷兼容顶层与 data 包裹两种形状。"""
        data = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        code = doc.get("code")
        if code is not None and code not in (0, "0", 200, "200"):
            detail = str(doc.get("message") or doc.get("msg") or "")[:120]
            raise UpstreamError(502, "service_rejected", detail or f"业务码 {code}")
        return {"enable": data.get("enable"),
                "checked_in": data.get("checked_in"),
                "credits": data.get("credits")}

    def run(self) -> dict:
        st = self._flags(self._post("status"))
        if st["enable"] is False:
            return {"checkin": "未开放签到活动"}
        if st["checked_in"]:
            return {"checkin": f"今日已签（{st['credits']} 积分）" if st["credits"] else "今日已签"}
        self._post("claim")
        # 空 200 不算到账凭证：复查 status，checked_in 确实翻转才算成功
        after = self._flags(self._post("status"))
        if after["checked_in"] is not True:
            return {"checkin": "领取后未确认到账，下轮复查"}
        return {"checkin": f"签到成功 +{st['credits']} 积分" if st["credits"] else "签到成功"}


# -- Qoder --------------------------------------------------------------------

class QoderCheckin:
    """Qoder 每日积分活动（campaigns 过滤 CLAIM_BENEFIT + CREDITS + 每日字样）。"""

    _DAILY = re.compile(r"daily|check.?in|每日|每天|签到", re.I)

    def __init__(self, doc):
        self.doc = doc
        self.base = _QODER_SITES.get(doc.channel_id or "", _QODER_SITES["qoder"])
        self.machine_id = _stable_device(f"qoder|{doc.uid}")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.doc.token}", "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": UA_QODER,
                "Cosy-ClientType": "10", "Cosy-Version": "0.4.8",
                "Cosy-MachineOS": "linux", "Cosy-MachineId": self.machine_id}

    def _campaigns(self) -> list[dict]:
        resp = requests.get(f"{self.base}/sash/api/v1/me/campaigns",
                            headers=self._headers(), timeout=TIMEOUT)
        if resp.status_code in (404, 410):
            return []
        doc = _json_or(resp)
        code = doc.get("code")
        if code is not None and code not in (0, "0", 200, "200") or doc.get("success") is False:
            raise UpstreamError(502, "service_rejected",
                                f"业务码 {code}（{str(doc.get('message') or '')[:120]}）")
        holder = doc.get("data") if isinstance(doc.get("data"), dict) else doc
        rows = holder.get("campaigns")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def run(self) -> dict:
        rows = self._campaigns()
        daily = [r for r in rows
                 if r.get("actionType") == "CLAIM_BENEFIT"
                 and isinstance(r.get("benefit"), dict)
                 and r["benefit"].get("kind") == "CREDITS"
                 and self._DAILY.search(json.dumps(
                     {k: r.get(k) for k in
                      ("campaignId", "campaignKey", "title", "name", "campaignName", "campaignType")},
                     ensure_ascii=False) or "")]
        if not daily:
            return {"checkin": "当前没有每日积分活动"}
        target = next((r for r in daily if r.get("claimStatus") == "CLAIMABLE"
                       and r.get("campaignId")), None)
        if target is None:
            if any(r.get("claimStatus") == "CLAIMED" for r in daily):
                return {"checkin": "今日已领"}
            return {"checkin": "暂无可领的每日积分"}
        amount = target["benefit"].get("amount")
        cid = target["campaignId"]
        requests.post(f"{self.base}/sash/api/v1/me/campaigns/{cid}/claim",
                      headers=self._headers(), json={}, timeout=TIMEOUT)
        after = next((r for r in self._campaigns() if r.get("campaignId") == cid), {})
        if after.get("claimStatus") == "CLAIMED":
            return {"checkin": f"领取成功 +{amount} Credits" if amount else "领取成功"}
        return {"checkin": "领取后未确认到账，下轮复查"}


# -- MiniMax ------------------------------------------------------------------

class MiniMaxCheckin:
    """MiniMax Code 七日签到（yy + x-signature 双 md5 签名）。"""

    BASE = "https://agent.minimaxi.com"
    _SALT = "I*7Cf%WZ#S&%1RlZJ&C2"

    def __init__(self, doc):
        self.doc = doc

    def _request(self, path: str, body: str = "") -> tuple[str, dict]:
        """构造带签名头的请求（签名契约：yy 掺 '{}'/时间戳，x-signature 掺秒级盐）。"""
        now = int(time.time() * 1000)
        second = now // 1000
        query = "&".join([
            "device_platform=web", "biz_id=3", "app_id=3001", "version_code=22201",
            "is_desktop=1", "desktop_version=0.6.5", f"unix={now}",
            f"timezone_offset={-int(time.localtime().tm_gmtoff)}",
            "sys_language=zh", "lang=zh", "device_id=0", "os_name=linux",
            "browser_name=mcode", f"user_id={self.doc.uid or 0}", "client=mcode"])
        signed_body = body or "{}"                     # status 无体也按 '{}' 签
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": UA_MINIMAX, "Authorization": f"Bearer {self.doc.token}",
                   "yy": hashlib.md5(
                       f"{quote(path + '?' + query)}_{signed_body}"
                       f"{hashlib.md5(str(now).encode()).hexdigest()}ooui".encode()).hexdigest(),
                   "x-timestamp": str(second),
                   "x-signature": hashlib.md5(
                       f"{second}{self._SALT}{body}".encode()).hexdigest()}
        return f"{self.BASE}{path}?{query}", headers

    @staticmethod
    def _unwrap(doc: dict) -> dict:
        base = doc.get("base_resp") if isinstance(doc.get("base_resp"), dict) else {}
        code = base.get("status_code")
        if code != 0:
            detail = str(base.get("status_msg") or "")[:120]
            raise UpstreamError(502, "service_rejected", detail or f"业务码 {code}")
        data = doc.get("data")
        return data if isinstance(data, dict) else {}

    def run(self) -> dict:
        url, headers = self._request("/minimax-cloud/api/v1/signin/status")
        panel = self._unwrap(_json_or(requests.get(url, headers=headers, timeout=TIMEOUT))).get("panel")
        days = panel.get("days") if isinstance(panel, dict) else None
        if not isinstance(days, list) or not days:
            return {"checkin": "签到面板数据异常，下轮复查"}
        today = next((d for d in days if isinstance(d, dict) and d.get("is_today")), None)
        if today and today.get("status") == 3:
            return {"checkin": "今日已签"}
        url, headers = self._request("/minimax-cloud/api/v1/signin/claim", body="{}")
        claim = self._unwrap(_json_or(requests.post(url, headers=headers,
                                                    data="{}", timeout=TIMEOUT)))
        result, points = claim.get("claim_result"), claim.get("points")
        if result == 2:
            return {"checkin": "今日已签"}
        if result == 1:
            return {"checkin": f"签到成功 +{points} 积分" if points is not None else "签到成功"}
        return {"checkin": f"领取未确认（result={result}），下轮复查"}


# -- LobsterAI ----------------------------------------------------------------

class LobsterCheckin:
    """LobsterAI 每日签到：slot → context → check_in → context 复核。"""

    BASE = "https://lobsterai-server.youdao.com"
    VERSION = "2026.9.4"

    def __init__(self, doc):
        self.doc = doc
        self.s = requests.Session()
        self.s.headers.update(_bearer_headers(doc.token, f"LobsterAI/{self.VERSION}"))

    def _get(self, path: str) -> dict:
        doc = _json_or(self.s.get(self.BASE + path, timeout=TIMEOUT))
        if doc.get("code", 0) not in (0, "0"):
            raise UpstreamError(502, "service_rejected",
                                str(doc.get("msg") or f"业务码 {doc.get('code')}")[:150])
        return doc.get("data") or {}

    def _post(self, path: str, body: dict) -> dict:
        doc = _json_or(self.s.post(self.BASE + path, json=body, timeout=TIMEOUT))
        if doc.get("code", 0) not in (0, "0"):
            raise UpstreamError(502, "service_rejected",
                                str(doc.get("msg") or f"业务码 {doc.get('code')}")[:150])
        return doc.get("data") or {}

    @staticmethod
    def _granted(data) -> int | float | None:
        """发放数字段上游改过多次，逐个试并下钻 result。"""
        if not isinstance(data, dict):
            return None
        for key in ("creditsGranted", "rewardCredits", "credits", "grantedCredits", "creditAmount"):
            val = data.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return val
        return LobsterCheckin._granted(data.get("result"))

    def run(self) -> dict:
        slot = self._get("/api/client-activities/slot?placement=desktop_sidebar"
                         f"&clientVersion={self.VERSION}&containerApiVersion=2&platform=win32")
        activity = slot.get("activity") or {}
        code, rev = activity.get("activityCode"), activity.get("configRevision", 0)
        if slot.get("slotState") != "available" or not code:
            return {"checkin": "上游未投放签到活动"}

        def context() -> dict:
            nonlocal rev
            ctx = self._get(f"/api/client-activities/{code}/context?configRevision={rev}")
            rev = ctx.get("configRevision") or rev
            return ctx

        ctx = context()
        state = ctx.get("state") or {}
        actions = [str(a).lower().strip() for a in (ctx.get("actions") or [])]
        if state.get("claimedToday") or "check_in" not in actions:
            streak = state.get("streakDays") or state.get("streak") or 0
            return {"checkin": "今日已签" + (f"（连登 {streak} 天）" if streak else "")}

        granted = self._granted(self._post(
            f"/api/client-activities/{code}/actions/check_in",
            {"configRevision": rev, "idempotencyKey": str(uuid.uuid4()), "payload": {}}))
        after = (context().get("state") or {}).get("claimedToday") or granted
        if not after:
            return {"checkin": "本次未发放（今日已领过）"}
        return {"checkin": f"签到成功 +{granted} 积分" if granted else "签到成功"}


# -- 分派 ----------------------------------------------------------------------

_OTHER = {"trae": TraeCheckin, "qoder": QoderCheckin, "qodercn": QoderCheckin,
          "minimax": MiniMaxCheckin, "lobsterai": LobsterCheckin}


def checkin_other(doc) -> dict:
    """非 WorkBuddy 渠道的签到入口；渠道未接入返回 skip 说明。"""
    cls = _OTHER.get(doc.channel_id or "")
    if cls is None:
        return {"skip": f"渠道 {doc.channel_name} 暂无签到托管"}
    return cls(doc).run()


def sweep_other(doc) -> dict:
    """签到兜底轮询入口：与 run 相同（各渠道实现本身幂等）。"""
    return checkin_other(doc)
