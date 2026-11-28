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

    def __init__(self, doc, log=print):
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

    def __init__(self, doc, log=print):
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

    def __init__(self, doc, log=print):
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

    def __init__(self, doc, log=print):
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


# -- 码道 CodeArts ------------------------------------------------------------

class CodeartsCheckin:
    """码道活动福利每日领取：优先走 snap 网关 IDE 通道（AK/SK 签名）。

    账号里有 codeartsCredential（AK/SK/security_token）时，直接用它调
    ``ops/delivery → ops/claim → ops/confirm``，与模型调用同一套
    SDK-HMAC-SHA256 签名（协议对照 FreeModelHub CodeArtsAdapter，
    needsChromium=false）；只有缺凭据时才回落 CDP。
    """

    _SNAP = "https://snap-access.cn-north-4.myhuaweicloud.com"
    _PACKAGE_INFO = f"{_SNAP}/snap-manager/v1/statistics/plugin"
    _OPS_DELIVERY = f"{_SNAP}/v1/ops/delivery"
    _OPS_CLAIM = f"{_SNAP}/v1/ops/claim"
    _OPS_CONFIRM = f"{_SNAP}/v1/ops/confirm"
    _CHANNEL = "IDE"
    _DAILY_TYPE = "USER_LOGIN"
    _CLAIMED = {"CLAIMED", "CONFIRMED", "CONSUMED"}

    def __init__(self, doc, log=print):
        self.doc = doc
        self.log = log

    def run(self) -> dict:
        cred = dict((self.doc.raw or {}).get("codeartsCredential") or {})
        if not (cred.get("access_key_id") and cred.get("security_token")):
            from .codearts_browser import checkin_by_browser
            return checkin_by_browser(self.log)
        try:
            return self._claim_by_ak(cred)
        except Exception as exc:
            return {"checkin": f"失败：{str(exc)[:120]}"}

    def _sign(self, cred: dict, method: str, url: str, body: str = "") -> dict:
        """SDK-HMAC-SHA256 全头签名（与模型调用同一套）。"""
        import datetime as _dt
        import hashlib
        import hmac as _hmac
        from urllib.parse import urlparse
        parsed = urlparse(url)
        canonical_uri = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
        sdk_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        signed = {"host": parsed.hostname or "", "x-sdk-date": sdk_date}
        sts = cred.get("security_token") or ""
        if sts:
            signed["x-security-token"] = sts
        names = sorted(signed)
        canonical_headers = "".join(f"{n}:{signed[n]}\n" for n in names)
        signed_headers = ";".join(names)
        canonical_request = "\n".join([
            method.upper(), canonical_uri, parsed.query or "",
            canonical_headers, signed_headers, body_hash])
        string_to_sign = "\n".join([
            "SDK-HMAC-SHA256", sdk_date,
            hashlib.sha256(canonical_request.encode()).hexdigest()])
        signature = _hmac.new((cred.get("secret_access_key") or "").encode(),
                              string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {"user-agent": "codearts-agent", "x-sdk-date": sdk_date,
                   "Agent-Type": "PromptCenter", "X-Language": "zh-cn"}
        if sts:
            headers["x-security-token"] = sts
        headers["authorization"] = (
            f"SDK-HMAC-SHA256 Access={cred.get('access_key_id', '')}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
        if body:
            headers["content-type"] = "application/json"
        return headers

    def _call(self, cred: dict, method: str, url: str, body=None, timeout=20):
        import json as _json
        import requests as _requests
        body_text = "" if body is None else _json.dumps(body, ensure_ascii=False)
        headers = self._sign(cred, method, url, body_text)
        r = _requests.request(method, url, headers=headers,
                              data=body_text if body is not None else None,
                              timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {str(data)[:120]}")
        if isinstance(data, dict) and isinstance(data.get("code"), int):
            if data["code"] != 0:
                msg = data.get("message") or data.get("msg") or f"业务码 {data['code']}"
                raise RuntimeError(msg)
            return data.get("data")
        return data

    def _claim_by_ak(self, cred: dict) -> dict:
        """三层请求，顺序与字段严格照 FreeModelHub CodeArtsAdapter。"""
        info = self._call(cred, "GET", self._PACKAGE_INFO) or {}
        pkg = info.get("package_info") if isinstance(info, dict) else None
        pkg = pkg if isinstance(pkg, dict) else {}
        if not pkg.get("is_credit_package"):
            return {"checkin": "Token 计费账号，不在积分活动范围"} if pkg.get("is_token_package") \
                else {"checkin": "非积分计费账号，不参与签到"}
        delivery = self._call(cred, "GET",
                              f"{self._OPS_DELIVERY}?channel={self._CHANNEL}") or {}
        items = delivery.get("items") if isinstance(delivery, dict) else None
        items = items if isinstance(items, list) else []
        activity = next((i for i in items
                         if isinstance(i, dict) and i.get("type") == self._DAILY_TYPE), None)
        if activity is None:
            return {"checkin": "未找到每日签到活动"}
        if not activity.get("claimable"):
            status = activity.get("status") or ""
            if status in self._CLAIMED:
                return {"checkin": "今日已签到"}
            return {"checkin": f"当前不可领取（status={status}）"}
        cid = activity.get("campaignId")
        if cid is None or cid == "":
            return {"checkin": "活动缺 campaignId，无法领取"}
        claim = self._call(cred, "POST", self._OPS_CLAIM,
                          {"campaignId": str(cid), "channel": self._CHANNEL}) or {}
        claim = claim if isinstance(claim, dict) else {}
        if claim.get("id") not in (None, ""):
            try:
                self._call(cred, "POST", self._OPS_CONFIRM,
                          {"campaignId": str(cid)}, timeout=15)
            except Exception:
                pass
        credit = (claim.get("benefitAmount") or claim.get("credit")
                  or activity.get("benefitAmount") or 0)
        return {"checkin": f"签到成功 +{credit} 积分" if credit else "签到成功"}


# -- Loomy（讯飞星火）----------------------------------------------------------

_LOOMY_BASE = "https://loomyad.xunfei.cn/api/v1"
_LOOMY_VERSION = "0.9.39"          # 官方客户端版本号，网关只做存在性校验


class LoomyCheckin:
    """Loomy 每日签到：POST /points/first-login（幂等，重复调用返回已处理）。

    协议参照 loomy2api（2026-11）：token + Authorization 双头（网关任一即可，
    客户端双发）；traceparent 缺失上游会挂起超时；loomy-version 只查存在性。
    业务码 000000 成功；100002/020002 会话失效——无续期接口，需重新扫码。
    """

    def __init__(self, doc, log=print):
        self.doc = doc
        self.log = log

    def _headers(self) -> dict:
        session = self.doc.token
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {session}",
            "token": session,
            "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
            "loomy-version": _LOOMY_VERSION,
        }

    def run(self) -> dict:
        resp = requests.post(f"{_LOOMY_BASE}/points/first-login",
                             json={}, headers=self._headers(), timeout=TIMEOUT)
        payload = _json_or(resp)
        code = str(payload.get("code") or "")
        if code in ("100002", "020002"):
            raise UpstreamError(401, "upstream_auth_expired",
                                f"会话已失效（业务码 {code}），请重新扫码登录。")
        if code != "000000":
            raise UpstreamError(502, "upstream_error",
                                f"签到被拒：code={code} {payload.get('desc') or ''}".strip())
        data = payload.get("data") or {}
        if data.get("alreadyProcessed"):
            return {"checkin": "今日已领（幂等跳过）"}
        daily = data.get("dailyBalance")
        text = f"签到成功，余额 {data.get('currentBalance')}"
        if daily not in (None, ""):
            text += f"（今日赠送 {daily}）"
        return {"checkin": text}


# -- ZCode（智谱）--------------------------------------------------------------

_ZCODE_ORIGIN = "https://zcode.z.ai"
_ZCODE_PREVIEW = "/api/v1/zcode-plan/billing/preview"
_ZCODE_EVENT = "/api/v1/event/report"
_ZCODE_CLAIM = "/api/v1/zcode-plan/billing/claim"

_ZCODE_CODE_TEXT = {
    1003: "本期已领取过",
    1005: "今日领取次数已用完",
    3007: "需要阿里云验证码参数（无感验证参数一次性、几分钟时效，纯接口取不到）",
}


class ZCodeCheckin:
    """ZCode 限时活动套餐领取：preview 查活动 → 补报活跃事件 → claim。

    协议参照 L0NE-6/ZCode-Daily（2026-11）：platform 必须传 windows（win32
    返 3001）；claim 前补报 app_launch / app_daily_active 提升资格（失败只记
    日志不阻塞）；1003 = 本期已领（幂等）；3007 = 缺验证码参数——如实报日志，
    不静默也不假装成功。
    """

    def __init__(self, doc, log=print):
        self.doc = doc
        self.log = log
        zc = doc.raw.get("zcode") or {}
        self.device_mid = str(zc.get("device_mid") or "")
        self.app_version = str(zc.get("app_version") or "3.16.2")

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.doc.token}",
             "X-ZCode-App-Version": self.app_version,
             "X-Platform": "windows"}
        if self.device_mid:
            h["X-Device-Mid"] = self.device_mid
        return h

    def _report_events(self) -> None:
        """补报 app_launch / app_daily_active（运营系统据此发领取资格）。"""
        import platform as _plat
        base = {
            "client_timezone": "Asia/Shanghai",
            "client_language": "zh-CN",
            "event_region": "app",
            "event_type": "view",
            "event_text": "",
            "event_extra_detail": {},
            "user_id": self.doc.uid,
            "screen_resolution": "2560x1440",
            "app_version": self.app_version,
            "device_os_category": _plat.system().lower() or "linux",
            "device_os_version": _plat.release(),
        }
        if self.device_mid:
            base["device_mid"] = self.device_mid
        fails = []
        for name in ("app_launch", "app_daily_active"):
            body = dict(base, event_id=str(uuid.uuid4()), element_name=name)
            try:
                r = requests.post(_ZCODE_ORIGIN + _ZCODE_EVENT, json=body,
                                  headers=self._headers(), timeout=TIMEOUT)
                if r.status_code != 200 or r.json().get("code") != 0:
                    fails.append(name)
            except Exception:
                fails.append(name)
        if fails:
            self.log(f"    zcode: 活跃事件补报失败（不阻塞领取）：{'、'.join(fails)}")

    def run(self) -> dict:
        r = requests.get(f"{_ZCODE_ORIGIN}{_ZCODE_PREVIEW}",
                         params={"app_version": self.app_version, "platform": "windows"},
                         headers=self._headers(), timeout=TIMEOUT)
        payload = _json_or(r)
        plans = [p for p in (payload.get("data") or {}).get("plans") or []
                 if isinstance(p, dict) and (p.get("plan_id") or p.get("planId"))]
        if not plans:
            return {"skip": "今日无可领活动"}
        self._report_events()
        best = max(plans, key=lambda p: p.get("priority") or 0)
        plan_id = str(best.get("plan_id") or best.get("planId"))
        cr = requests.post(_ZCODE_ORIGIN + _ZCODE_CLAIM, json={"plan_id": plan_id},
                           headers=self._headers(), timeout=TIMEOUT)
        cpayload = _json_or(cr)
        code = cpayload.get("code")
        if code == 0:
            data = cpayload.get("data") or {}
            name = best.get("name") or plan_id
            ends = data.get("ends_at") or data.get("endsAt")
            when = ""
            if isinstance(ends, (int, float)) and ends > 0:
                when = "，有效期至 " + time.strftime(
                    "%m-%d %H:%M", time.localtime(ends / 1000 if ends > 10**11 else ends))
            return {"checkin": f"领取成功：{name}{when}"}
        if code == 1003:
            return {"checkin": "本期已领取过"}
        text = _ZCODE_CODE_TEXT.get(code) or f"code={code} {cpayload.get('msg') or ''}".strip()
        self.log(f"    zcode: 活动领取未完成（{plan_id}）：{text}")
        return {"checkin": f"发现活动但领取未完成：{text}"}


# -- 豆包（豆包工作 / 豆包电脑版）----------------------------------------------

_DOUBAO_BASE = "https://www.doubao.com"
_DOUBAO_SCENE = "pc_incentive_task_mvp_v3"     # 客户端「免费用豆包」签到活动组
_DOUBAO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 "
              "SamanthaDoubao/2.36.14")
# 桌面端公共 query（参照官方客户端 2.36.14 + doubao2api 实测；缺了回 code=1 invalid param）
_DOUBAO_QUERY = {
    "aid": "582478", "real_aid": "582478", "channel": "win",
    "client_platform": "pc_client", "device_platform": "web",
    "doubao_device_platform": "desktop", "doubao_pc_version": "2.36.14",
    "pc_version": "2.36.14", "pkg_type": "release_version", "language": "zh",
    "region": "CN", "sys_region": "CN", "runtime": "web",
    "runtime_version": "3.37.4", "samantha_web": "1",
    "use-olympus-account": "1", "version_code": "20800",
    "web_platform": "desktop", "chromium_version": "147.0.7727.149",
}


class DoubaoCheckin:
    """豆包每日签到领额度：detail 查状态 → landing/checkin 领取。

    协议参照 CreditDaddy#32 / doubao2api / 快灵实操记录（2026-10~11 实测互证）：
    - 鉴权纯 doubao.com 会话 Cookie，无 a_bogus / msToken 签名，device_id 空可过；
    - detail 路径尾斜杠不能丢、必须 POST（GET 或缺斜杠 404）；
    - 服务端幂等（idempotent:true = 当天已签，奖励不重复发放）；
    - 710010702 = 账号未命中活动（活动下线/飞书企业号被排除）；
    - 710012001 = 会话失效，需重新扫码。
    额度每 200/天、周一翻倍，累计 500/2000/5000 里程碑再加码，30 天共 8800。
    """

    def __init__(self, doc, log=print):
        self.doc = doc

    def _url(self, path: str) -> str:
        query = "&".join(f"{k}={v}" for k, v in _DOUBAO_QUERY.items())
        return f"{_DOUBAO_BASE}{path}?{query}"

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Referer": "https://www.doubao.com/",
                "User-Agent": _DOUBAO_UA, "Cookie": self.doc.token}

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(self._url(path), json=body, headers=self._headers(),
                             timeout=TIMEOUT)
        return _json_or(resp)

    @staticmethod
    def _unwrap(doc: dict) -> dict:
        """code:0 正常回包；已知业务码转人类可读错误，未知码如实透出。"""
        code = doc.get("code")
        if code in (0, "0", None):
            return doc.get("data") if isinstance(doc.get("data"), dict) else {}
        msg = str(doc.get("message") or doc.get("msg") or "")[:150]
        if code in (710012001, "710012001") or "登录" in msg or "会话" in msg:
            raise UpstreamError(401, "upstream_auth_expired",
                                f"豆包会话已失效（{code}），请重新扫码登录。")
        if code in (710010702, "710010702"):
            raise UpstreamError(502, "no_campaign", "当前无可参与的签到活动（活动可能已下线，或飞书企业号不参与）。")
        raise UpstreamError(502, "service_rejected",
                            f"豆包拒绝（code={code}）{msg}".strip())

    def _daily_task(self) -> dict:
        """detail 任务面板里的 daily_signin 任务结构（无则返回空 dict）。"""
        data = self._unwrap(self._post("/alice/ug/campaign/v3/detail/",
                                       {"campaign_group_code": _DOUBAO_SCENE}))
        tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
        return next((t for t in tasks if t.get("task_kind") == "daily_signin"), {})

    @staticmethod
    def _fmt_amount(value) -> str:
        """额度字段可能带千分位（"1,000"）——剔除逗号再格式化。"""
        text = str(value or "").replace(",", "").strip()
        return text if text.isdigit() else ""

    def _quota_text(self) -> str:
        """剩余额度一句话（失败不阻塞签到，只静默跳过）。

        响应形状 data.*.window_limits[].{total_amount, used_amount}（多套餐），
        total_amount 是带千分位的字符串（如 "1,000"）。
        """
        try:
            data = self._unwrap(self._post(
                "/alice/commerce/sale/subscription/quota/summary/", {}))
            found: list[dict] = []

            def _walk(node) -> None:
                if isinstance(node, dict):
                    if node.get("total_amount") is not None:
                        found.append(node)
                    for v in node.values():
                        _walk(v)
                elif isinstance(node, list):
                    for v in node:
                        _walk(v)

            _walk(data)
            for w in found:
                total = self._fmt_amount(w.get("total_amount"))
                used = self._fmt_amount(w.get("used_amount"))
                if total.isdigit():
                    left = max(0, int(total) - int(used or 0))
                    return f"，余额 {left}/{total}"
        except Exception:
            pass
        return ""

    def run(self) -> dict:
        task = self._daily_task()
        check = task.get("daily_checkin") if isinstance(task, dict) else None
        if task and isinstance(check, dict) and check.get("checked_in_today"):
            days = check.get("continuous_days")
            reward = (check.get("reward") or {}).get("reward_amount")
            return {"checkin": "今日已签"
                    + (f"（连签 {days} 天）" if days else "")
                    + self._quota_text()}
        if task and not check:
            return {"skip": "活动面板里没有签到任务（可能已下线）"}

        claim = self._unwrap(self._post("/alice/ug/campaign/landing/checkin",
                                        {"scene": _DOUBAO_SCENE}))
        reward, days = claim.get("reward_amount"), claim.get("continuous_days")
        if claim.get("idempotent"):
            return {"checkin": "今日已签（幂等命中）" + self._quota_text()}
        if claim.get("checked_in"):
            text = "签到成功"
            if reward:
                text += f" +{self._fmt_amount(reward) or reward} 额度"
            if days:
                text += f"（连签 {days} 天）"
            return {"checkin": text + self._quota_text()}
        return {"checkin": "领取未确认，下轮复查"}


# -- 分派 ----------------------------------------------------------------------

_OTHER = {"trae": TraeCheckin, "qoder": QoderCheckin, "qodercn": QoderCheckin,
          "minimax": MiniMaxCheckin, "lobsterai": LobsterCheckin,
          "codearts": CodeartsCheckin, "loomy": LoomyCheckin, "zcode": ZCodeCheckin,
          "doubao": DoubaoCheckin}


def checkin_other(doc, log=print) -> dict:
    """非 WorkBuddy 渠道的签到入口；渠道未接入返回 skip 说明。"""
    cls = _OTHER.get(doc.channel_id or "")
    if cls is None:
        return {"skip": f"渠道 {doc.channel_name} 暂无签到托管"}
    return cls(doc, log=log).run()


def sweep_other(doc, log=print) -> dict:
    """签到兜底轮询入口：与 run 相同（各渠道实现本身幂等）。"""
    return checkin_other(doc, log=log)
