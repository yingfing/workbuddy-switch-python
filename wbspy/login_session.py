"""异步登录会话：为 Web UI 提供 start/poll 两段式登录。

把 login.py 的阻塞流程改造成状态机：

    POST /api/login/start  {channel, method?}
      → {ticket, kind: qr|browser|manual|done, qr_svg, auth_url, message}
    GET  /api/login/poll/{ticket}
      → {status: pending|success|failed|expired, document?}

Raccoon 扫码与会话票据管理在本文件实现；WorkBuddy state 轮询复用 login.py
的协议常量。完成的凭据直接入库（store.save_account）并回传给前端展示。
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests

from .channels import Channel, get_channel, methods_of
from .document import Document, GLOBAL_DOMAINS, parse_auth
from .login import (
    LOGIN_HOSTS,
    LOGIN_TIMEOUT_SEC,
    POLL_INTERVAL_SEC,
    RACCOON_AUTH_PREFIX,
    _decode_jwt_exp_ms,
    _render_qr_in_terminal,
    _validate_login_url,
    local_login,
)
from .store import save_account


# Gemini 本地回调收到的 code：state → code（由 webui 的回调端点写入）
GEMINI_CALLBACK_CODES: dict[str, str] = {}


def parse_minimax_token(payload: dict) -> Document | None:
    """MiniMax token 轮询响应 → Document；未批准返回 None。"""
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    from .login import _decode_jwt_exp_ms
    exp = _decode_jwt_exp_ms(token)
    doc = parse_auth({
        "account": {"uid": str(payload.get("user_id") or "minimax-user"),
                    "nickname": "MiniMax 账号"},
        "auth": {"accessToken": token, "refreshToken": payload.get("refresh_token", ""),
                 "domain": "account.minimax.cn", "expiresAt": exp},
    })
    doc.raw.setdefault("channel", "minimax")
    return doc


def qr_svg(content: str) -> str:
    """内容 → SVG 二维码字符串（qrcode 库缺失时返回空串）。"""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(content, image_factory=factory, border=1)
    return img.to_string().decode()


@dataclass
class Ticket:
    id: str
    channel_id: str
    method: str
    kind: str = ""                  # qr / browser / manual / done
    qr_content: str = ""
    qr_svg: str = ""
    auth_url: str = ""
    message: str = ""
    # 会话状态
    state: str = ""                 # workbuddy state
    code: str = ""                  # raccoon qrcode_code
    origin: str = ""
    platform: str = ""
    expires_at: float = 0.0
    document: Document | None = None
    error: str = ""

    def public(self) -> dict:
        return {
            "ticket": self.id, "channel": self.channel_id, "method": self.method,
            "kind": self.kind, "qr_svg": self.qr_svg, "auth_url": self.auth_url,
            "message": self.message, "error": self.error,
        }


class LoginSessionManager:
    """进程内票据表 + 轮询状态机。线程安全（FastAPI 多 worker 也在单进程内）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    # -- 启动 -----------------------------------------------------------------

    def start(self, channel_id: str, method: str | None = None) -> dict:
        channel = get_channel(channel_id)
        chosen = method or methods_of(channel)[0]
        errors: list[str] = []
        for m in ([chosen] if method else methods_of(channel)):
            try:
                return self._start_one(channel, m)
            except Exception as exc:
                errors.append(f"{m}: {exc}")
        raise RuntimeError(f"渠道 {channel.name} 暂无可用登录方式（{'；'.join(errors)}）")

    def _start_one(self, channel: Channel, method: str) -> dict:
        ticket = Ticket(id=uuid.uuid4().hex[:16], channel_id=channel.id,
                        method=method, expires_at=time.time() + LOGIN_TIMEOUT_SEC)
        if method == "qr" and channel.login_kind == "raccoon-qr":
            code = secrets.token_hex(16)
            ticket.kind = "qr"
            ticket.code = code
            ticket.origin = channel.origin
            ticket.qr_content = f"{channel.origin}/login/mp?code={code}&appname={quote_plus('商汤小浣熊官网')}"
            ticket.qr_svg = qr_svg(ticket.qr_content)
            ticket.message = "请用「商汤小浣熊」App 扫码"
        elif method == "browser" and channel.login_kind == "workbuddy-state":
            global_login = channel.id == "workbuddy"
            origin = "https://www.workbuddy.ai" if global_login else "https://www.codebuddy.cn"
            platform = "workbuddy-ai" if global_login else "workbuddy"
            resp = requests.post(
                f"{origin}/v2/plugin/auth/state?platform={platform}",
                json={}, timeout=15,
                headers={"Content-Type": "application/json", "X-Client-Platform": "web"})
            if not resp.ok:
                raise RuntimeError(f"网页登录服务暂不可用（HTTP {resp.status_code}）")
            data = resp.json().get("data") or {}
            state = data.get("state")
            if not isinstance(state, str) or not state or len(state) > 500:
                raise RuntimeError("登录服务未返回有效授权请求。")
            url = _validate_login_url(
                data.get("authUrl") or data.get("auth_url") or data.get("url") or
                f"{origin}/login?state={quote_plus(state)}", platform)
            ticket.kind = "browser"
            ticket.origin = origin
            ticket.platform = platform
            ticket.state = state
            ticket.auth_url = url
            ticket.qr_content = url
            ticket.qr_svg = qr_svg(url)
            ticket.message = "手机扫码打开官方登录页，或在本机浏览器打开；页面内可扫码/短信/密码登录"
        elif method == "browser" and channel.login_kind == "codearts-ticket":
            # 华为云 CodeArts ticket 登录（对齐 channel-pack runLoginFlow）：
            # 本地 ticketId + secret，浏览器完成华为云登录后，凭据可从
            # snap-manager 轮询端点取回，无需本地回调服务器。
            ticket_no = uuid.uuid4()
            secret = secrets.token_hex(32)
            callback = "http://127.0.0.1:0/authentication"
            redirect_url = ("https://devcloud.cn-north-4.huaweicloud.com/doer/redirect"
                            "?IdeaType=jetbrains"
                            f"&auth_callback_url={quote_plus(callback)}"
                            "&plugin-name=snap_jetbrains&plugin-version=26.3.3"
                            f"&ticket_id={quote_plus(str(ticket_no))}")
            url = ("https://auth.huaweicloud.com/authui/login.html?service="
                   + quote_plus(redirect_url))
            ticket.kind = "browser"
            ticket.platform = "codearts"
            ticket.origin = "https://snap-access.cn-north-4.myhuaweicloud.com"
            ticket.state = str(ticket_no)
            ticket.code = secret
            ticket.auth_url = url
            ticket.qr_content = url
            ticket.qr_svg = qr_svg(url)
            ticket.message = ("在新窗口打开华为云登录页完成登录；登录成功后的跳转"
                              "页面可无视，此处会自动检测到凭据（DeepSeek/GLM/"
                              "openPangu 每日免费额度）")
        elif method == "browser" and channel.login_kind == "cline-device":
            # Cline = WorkOS 设备码（对齐 channel-pack requestClineDeviceAuthorization）
            resp = requests.post(
                "https://api.workos.com/user_management/authorize/device",
                data={"client_id": "client_01K3A541FN8TA3EPPHTD2325AR"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15)
            data = resp.json() if resp.ok else {}
            device_code = data.get("device_code") or ""
            user_code = data.get("user_code") or ""
            url = (data.get("verification_uri_complete")
                   or data.get("verification_uri") or "")
            if not (device_code and user_code and url):
                raise RuntimeError(f"WorkOS 设备码授权失败（HTTP {resp.status_code}）。")
            ticket.kind = "device"
            ticket.platform = "cline"
            ticket.origin = "https://api.workos.com"
            ticket.state = device_code
            ticket.code = user_code
            ticket.auth_url = url
            ticket.message = (f"在新窗口打开 Cline 授权页并确认设备码 "
                              f"{user_code}；此处自动等待")
        elif method == "qr" and channel.login_kind == "loomy-wechat":
            # Loomy = 微信开放平台扫码（对齐 channel-pack loomy-wechat.ts）
            state = uuid.uuid4().hex
            auth_url = ("https://open.weixin.qq.com/connect/qrconnect"
                        "?appid=wx18d60be432287cf8"
                        f"&redirect_uri={quote_plus('https://loomy.xunfei.cn/oauth/wechat/callback')}"
                        f"&response_type=code&scope=snsapi_login&state={state}#wechat_redirect")
            resp = requests.get(auth_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://open.weixin.qq.com/"}, timeout=20)
            m = (re.search(r"/connect/qrcode/([A-Za-z0-9_\-=+/]+)", resp.text)
                 or re.search(r"l/qrconnect\?uuid=([A-Za-z0-9_\-=+/]+)", resp.text))
            if not resp.ok or not m:
                raise RuntimeError("微信授权页拉取失败或页面结构已变化。")
            wuuid = m.group(1)
            ticket.kind = "qr"
            ticket.platform = "loomy-wechat"
            ticket.origin = "https://account.xfinfr.com"
            ticket.state = state
            ticket.code = wuuid
            ticket.auth_url = auth_url
            ticket.qr_content = f"https://open.weixin.qq.com/connect/qrcode/{wuuid}"
            ticket.qr_svg = f'<img src="{ticket.qr_content}" alt="微信二维码" width="220"/>'
            ticket.message = "请用微信扫码并确认登录（讯飞星火 Loomy）"
        elif method == "qr" and channel.login_kind == "minimax-device":
            from .login import MINIMAX_ACCOUNT, _pkce_pair
            import requests as _rq
            verifier, challenge = _pkce_pair()
            resp = _rq.post(f"{MINIMAX_ACCOUNT}/oauth2/device/code", data={
                "client_id": "mcode-public", "scope": "agent.default",
                "audience": "agent-backend", "code_challenge": challenge,
                "code_challenge_method": "S256"}, timeout=15)
            if not resp.ok:
                raise RuntimeError(f"MiniMax 设备码服务暂不可用（HTTP {resp.status_code}）")
            data = resp.json()
            ticket.kind = "device"
            ticket.origin = MINIMAX_ACCOUNT
            ticket.state = data.get("device_code", "")
            ticket.code = verifier                       # 借 code 字段存 verifier
            ticket.auth_url = data.get("verification_uri_complete") or data.get("verification_uri", "")
            ticket.message = f"打开验证链接并输入设备码：{data.get('user_code', '')}"
            ticket.qr_content = ticket.auth_url
            ticket.qr_svg = qr_svg(ticket.auth_url)
        elif method == "browser" and channel.login_kind == "gemini-oauth":
            from .login import GEMINI_AUTH, GEMINI_SCOPES, gemini_client
            import secrets as _secrets
            ticket.kind = "browser"
            ticket.platform = "gemini"
            ticket.state = _secrets.token_hex(12)
            ticket.origin = f"http://localhost:8199/oauth-callback"
            ticket.auth_url = (f"{GEMINI_AUTH}?client_id={gemini_client()[0]}&response_type=code"
                               f"&redirect_uri={quote_plus(ticket.origin)}"
                               f"&scope={quote_plus(' '.join(GEMINI_SCOPES))}"
                               f"&state={ticket.state}&access_type=offline&prompt=consent")
            ticket.qr_content = ticket.auth_url
            ticket.qr_svg = qr_svg(ticket.auth_url)
            ticket.message = "在本机浏览器打开授权链接，完成后自动回调本机 8199 端口"
        elif method == "local":
            doc = local_login(channel.id)
            save_account(doc)
            ticket.kind = "done"
            ticket.document = doc
            ticket.message = f"已读取本机凭据并入库：{doc.label()}"
        else:
            # 其余渠道暂无协议级实现：诚实降级，给出厂商登录页链接
            ticket.kind = "manual"
            ticket.auth_url = channel.origin
            ticket.message = (f"{channel.name} 的登录协议尚未在本版实现。"
                              f"可打开厂商登录页（{channel.origin}）手动登录；"
                              "若本机装有该桌面客户端，改用 local 方式读取凭据。")
        with self._lock:
            self._tickets[ticket.id] = ticket
        return ticket.public()

    # -- 轮询 -----------------------------------------------------------------

    def poll(self, ticket_id: str) -> dict:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return {"status": "expired", "error": "会话不存在或已清理"}
        if ticket.document is not None:
            return {"status": "success", "document": _doc_public(ticket.document)}
        if ticket.error:
            return {"status": "failed", "error": ticket.error}
        if time.time() > ticket.expires_at:
            self.drop(ticket_id)
            return {"status": "expired", "error": "登录会话已超时（5 分钟），请重新发起"}
        try:
            if ticket.kind == "qr" and ticket.code and ticket.channel_id == "raccoon":
                doc = self._poll_raccoon(ticket)
            elif ticket.kind == "device" and ticket.state:
                doc = self._poll_minimax(ticket)
            elif ticket.kind == "device" and ticket.platform == "cline":
                doc = self._poll_cline(ticket)
            elif ticket.kind == "qr" and ticket.platform == "loomy-wechat":
                doc = self._poll_loomy(ticket)
            elif ticket.kind == "browser" and ticket.platform == "codearts":
                doc = self._poll_codearts(ticket)
            elif ticket.kind == "browser" and ticket.platform == "gemini":
                doc = self._poll_gemini(ticket)
            elif ticket.kind == "browser" and ticket.state:
                doc = self._poll_workbuddy(ticket)
            else:
                doc = None  # manual / done：无轮询
        except Exception as exc:
            ticket.error = str(exc)
            return {"status": "failed", "error": str(exc)}
        if doc is not None:
            ticket.document = doc
            save_account(doc)
            return {"status": "success", "document": _doc_public(doc)}
        return {"status": "pending", "message": ticket.message}

    def _poll_raccoon(self, ticket: Ticket) -> Document | None:
        try:
            resp = requests.post(
                f"{ticket.origin}{RACCOON_AUTH_PREFIX}/login_with_qrcode_code",
                json={"qrcode_code": ticket.code}, timeout=15)
            envelope = resp.json() if resp.ok else {}
        except (requests.RequestException, ValueError):
            return None
        data = envelope.get("data") if envelope.get("code") == 0 else None
        status = (data or {}).get("status", "")
        if status == "canceled":
            raise RuntimeError("扫码登录已被取消。")
        if status == "success" and (data or {}).get("access_token"):
            exp = _decode_jwt_exp_ms(data["access_token"])
            doc = parse_auth({
                "account": {"uid": str(data.get("uid") or data.get("user_id") or "raccoon-user"),
                            "nickname": data.get("nickname") or "Raccoon 账号"},
                "auth": {"accessToken": data["access_token"],
                         "refreshToken": data.get("refresh_token", ""),
                         "domain": urlparse(ticket.origin).hostname,
                         "expiresAt": exp},
            })
            doc.raw.setdefault("channel", "raccoon")
            return doc
        return None

    def _poll_workbuddy(self, ticket: Ticket) -> Document | None:
        try:
            resp = requests.get(
                f"{ticket.origin}/v2/plugin/auth/token?state={quote_plus(ticket.state)}",
                timeout=15, headers={"X-Client-Platform": "web"})
            payload = resp.json() if resp.ok else {}
        except (requests.RequestException, ValueError):
            return None
        inner = payload.get("data") or {}
        token = inner.get("accessToken") or inner.get("access_token")
        if not isinstance(token, str) or not token:
            return None

        domain = inner.get("domain") or urlparse(ticket.origin).hostname
        global_login = ticket.platform == "workbuddy-ai"
        allowed = GLOBAL_DOMAINS if global_login else \
            {"codebuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "www.workbuddy.cn"}
        if domain not in allowed:
            raise ValueError("登录凭据的服务域名不匹配，已停止保存。")
        profile = requests.get(
            f"{ticket.origin}/v2/plugin/login/account?state={quote_plus(ticket.state)}",
            headers={"Authorization": f"Bearer {token}", "X-Domain": domain,
                     "X-Client-Platform": "web"}, timeout=15)
        if not profile.ok or not (profile.json().get("data") or {}).get("uid"):
            raise RuntimeError("网页登录未返回有效账号信息。")
        account = profile.json()["data"]
        expires_at = inner.get("expiresAt") or (
            int(time.time() * 1000 + float(inner["expiresIn"]) * 1000)
            if inner.get("expiresIn") else None)
        doc = parse_auth({
            "account": account,
            "auth": {**inner, "accessToken": token, "domain": domain, "expiresAt": expires_at},
            "allAccounts": [account],
        })
        doc.raw.setdefault("channel", "workbuddy-global" if global_login else "buddy")
        return doc

    def _poll_minimax(self, ticket: Ticket) -> Document | None:
        from .login import parse_minimax_token
        try:
            resp = requests.post(f"{ticket.origin}/oauth2/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": ticket.state, "client_id": "mcode-public",
                "code_verifier": ticket.code}, timeout=15).json()
        except (requests.RequestException, ValueError):
            return None
        doc = parse_minimax_token(resp)
        return doc                                       # None = 还没批准

    def _poll_cline(self, ticket: Ticket) -> Document | None:
        """WorkOS 设备码轮询 + Cline token 注册（对齐 pollClineWorkOsTokens）。"""
        try:
            resp = requests.post(
                "https://api.workos.com/user_management/authenticate",
                data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                      "device_code": ticket.state,
                      "client_id": "client_01K3A541FN8TA3EPPHTD2325AR"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15)
            payload = resp.json() if resp.text else {}
        except (requests.RequestException, ValueError):
            return None
        err = payload.get("error")
        if err in ("authorization_pending", "slow_down"):
            return None
        if err:
            raise RuntimeError(f"WorkOS 授权失败：{payload.get('error_description') or err}")
        access = payload.get("access_token") or ""
        refresh = payload.get("refresh_token") or ""
        if not access:
            return None
        # 注册到 Cline 换取正式凭据
        reg = requests.post(
            "https://api.cline.bot/api/v1/auth/register",
            json={"accessToken": access, "refreshToken": refresh},
            headers={"Content-Type": "application/json",
                     "HTTP-Referer": "https://cline.bot", "X-Title": "Cline",
                     "X-IS-MULTIROOT": "false", "X-CLIENT-TYPE": "cline-sdk"},
            timeout=20)
        reg_data = reg.json() if reg.ok else {}
        inner = reg_data.get("data") if isinstance(reg_data.get("data"), dict) else reg_data
        token = inner.get("accessToken") or inner.get("access_token") or access
        exp_ms = _decode_jwt_exp_ms(token) if isinstance(token, str) else None
        account = (inner.get("userInfo") or {}) if isinstance(inner, dict) else {}
        doc = parse_auth({
            "account": {"uid": str(account.get("clineUserId") or account.get("accountId")
                                   or inner.get("accountId") or "cline-user"),
                        "nickname": (f"{account.get('firstName', '')} {account.get('lastName', '')}".strip()
                                     or account.get("email") or "Cline 账号")},
            "auth": {"accessToken": token, "refreshToken": refresh,
                     "domain": "api.cline.bot", "expiresAt": exp_ms},
        })
        doc.raw.setdefault("channel", "cline")
        return doc

    def _poll_loomy(self, ticket: Ticket) -> Document | None:
        """微信扫码长轮询 + 讯飞账号绑定（对齐 pollLoomyWechatOnce）。"""
        try:
            resp = requests.get(
                "https://long.open.weixin.qq.com/connect/l/qrconnect",
                params={"uuid": ticket.code, "_": int(time.time() * 1000)},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                         "Referer": "https://open.weixin.qq.com/"}, timeout=40)
            body = resp.text
        except requests.RequestException:
            return None
        errcode = (re.search(r"wx_errcode\s*=\s*(\d+)", body) or [None, ""])[1] if             re.search(r"wx_errcode\s*=\s*(\d+)", body) else ""
        wx_code = (re.search(r"wx_code\s*=\s*'([^']*)'", body) or [None, ""])[1] if             re.search(r"wx_code\s*=\s*'([^']*)'", body) else ""
        if errcode in ("", "408", "404"):
            return None  # 等待扫码 / 已扫码待确认
        if errcode == "402":
            raise RuntimeError("二维码已失效，请重新发起登录。")
        if errcode == "403":
            raise RuntimeError("你取消了微信授权。")
        if errcode != "405" or not wx_code:
            return None
        # 确认成功：绑定微信 → 换 session
        def loomy_body(param: dict) -> dict:
            return {"base": {"appid": "GM3LOOMY", "modelid": "Web", "version": "1.0.0",
                             "devid": "web", "ua": "Loomy|Desktop|Electron|macOS",
                             "traceid": uuid.uuid4().hex}, "param": param}

        def loomy_headers(body_json: str, path: str) -> dict:
            date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
            nonce = uuid.uuid4()
            md5 = base64.b64encode(hashlib.md5(body_json.encode()).digest()).decode()
            sts = "\n".join(["POST", path, "", md5, "application/json", date,
                              str(nonce), "", ""])
            sig = base64.b64encode(hmac.new(b"zsak6eadrbawz683wf5r3m2snrwj868r",
                                            sts.encode(), hashlib.sha1).digest()).decode()
            return {"Authorization": f"account 2thryby66wxi53sk:{sig}",
                    "Date": date, "Nonce": str(nonce),
                    "Content-Type": "application/json", "Content-MD5": md5}

        bj = json.dumps(loomy_body({"tcode": {"code": wx_code}, "type": "wx"}))
        r1 = requests.post("https://account.xfinfr.com/login/thirdAccount/bind/auth",
                           data=bj, headers=loomy_headers(bj, "/login/thirdAccount/bind/auth"),
                           timeout=15)
        rcode = (r1.json() or {}).get("rcode") if r1.ok else ""
        if not rcode:
            raise RuntimeError("微信授权绑定失败。")
        bj2 = json.dumps(loomy_body({"rcode": rcode, "expire": 1209600}))
        r2 = requests.post("https://account.xfinfr.com/login/thirdAccount/bind/skip",
                           data=bj2, headers=loomy_headers(bj2, "/login/thirdAccount/bind/skip"),
                           timeout=15)
        rec = (r2.json() or {}) if r2.ok else {}
        session, userid = rec.get("session") or "", rec.get("userid") or ""
        if not (session and userid):
            raise RuntimeError("Loomy 登录未返回有效会话。")
        doc = parse_auth({
            "account": {"uid": str(userid), "nickname": rec.get("nickname") or "Loomy 账号"},
            "auth": {"accessToken": session, "domain": "account.xfinfr.com",
                     "expiresAt": int(time.time() * 1000 + 1209600 * 1000)},
        })
        doc.raw.setdefault("channel", "loomy")
        return doc

    def _poll_codearts(self, ticket: Ticket) -> Document | None:
        """轮询华为云 snap-manager 取回 CodeArts STS 临时凭据。"""
        try:
            resp = requests.get(
                f"{ticket.origin}/snap-manager/v1/login/ticket",
                params={"ticket_id": ticket.state, "secret": ticket.code},
                headers={"Content-Type": "application/json;charset=UTF-8",
                         "plugin-name": "snap_AIIDE", "plugin-version": "5.2.0"},
                timeout=15)
            data = resp.json() if resp.ok else {}
        except (requests.RequestException, ValueError):
            return None
        cred = (data or {}).get("credential") or {}
        access = cred.get("access") or ""
        st = cred.get("securitytoken") or cred.get("securityToken") or ""
        if not (access and st):
            return None
        expires_raw = str(cred.get("expires_at") or cred.get("expiresAt") or "")
        exp_ms = None
        if expires_raw:
            try:
                exp_ms = int(time.mktime(time.strptime(
                    expires_raw[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
            except ValueError:
                exp_ms = None
        doc = parse_auth({
            "account": {"uid": str(cred.get("user_id") or cred.get("domain_id")
                                   or "codearts-user"),
                        "nickname": cred.get("user_name") or "CodeArts 账号"},
            "auth": {"accessToken": st, "domain": "codearts.huaweicloud.com",
                     "expiresAt": exp_ms},
        })
        doc.raw.setdefault("channel", "codearts")
        doc.raw["codeartsCredential"] = {
            "access_key_id": access,
            "secret_access_key": cred.get("secret", ""),
            "security_token": st,
            "expires_at": expires_raw,
            "domain_id": cred.get("domain_id", ""),
            "user_id": cred.get("user_id", ""),
            "user_name": cred.get("user_name", ""),
        }
        return doc

    def _poll_gemini(self, ticket: Ticket) -> Document | None:
        """等待本机 8199 回调收到 code（由 /api/gemini-callback 注入）。"""
        from .login import GEMINI_TOKEN, gemini_client
        _gc = gemini_client()
        code = GEMINI_CALLBACK_CODES.pop(ticket.state, None)
        if not code:
            return None
        tok = requests.post(GEMINI_TOKEN, data={
            "code": code,
            "client_id": _gc[0], "client_secret": _gc[1],
            "redirect_uri": ticket.origin, "grant_type": "authorization_code"}, timeout=15).json()
        token = tok.get("access_token")
        if not token:
            ticket.error = f"Google token 交换失败：{str(tok)[:140]}"
            return None
        email = ""
        try:
            email = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                 headers={"Authorization": f"Bearer {token}"},
                                 timeout=10).json().get("email", "")
        except Exception:
            pass
        doc = parse_auth({
            "account": {"uid": email or "gemini-user", "nickname": f"Gemini ({email or 'google'})"},
            "auth": {"accessToken": token, "refreshToken": tok.get("refresh_token", ""),
                     "domain": "googleapis.com",
                     "expiresAt": int(time.time() * 1000 + float(tok.get("expires_in", 3600)) * 1000)},
        })
        doc.raw.setdefault("channel", "gemini")
        return doc

    def drop(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for tid, ticket in list(self._tickets.items()):
                if ticket.expires_at < now and ticket.document is None:
                    del self._tickets[tid]


def _doc_public(doc: Document) -> dict:
    return {"uid": doc.uid, "label": doc.label(), "region": doc.region,
            "api_ready": doc.api_ready, "protected": doc.protected_token}
