"""华为云 CodeArts：ticket 轮询（对齐 channel-pack runLoginFlow）。"""

from __future__ import annotations

import secrets
import time
import uuid
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket, qr_svg

_CREDENTIAL_ENDPOINT = "https://snap-access.cn-north-4.myhuaweicloud.com"


def start(channel, ticket: Ticket) -> None:
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
    ticket.origin = _CREDENTIAL_ENDPOINT
    ticket.state = str(ticket_no)
    ticket.code = secret
    ticket.auth_url = url
    ticket.qr_content = url
    ticket.qr_svg = qr_svg(url)
    ticket.message = ("在新窗口打开华为云登录页完成登录；登录成功后的跳转"
                      "页面可无视，此处会自动检测到凭据（DeepSeek/GLM/"
                      "openPangu 每日免费额度）")


def poll(ticket: Ticket) -> "Document | None":
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
