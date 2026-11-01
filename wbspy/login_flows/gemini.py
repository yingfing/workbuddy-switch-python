"""Gemini：Google OAuth 本机回调（8199 端口由 webui 监听注入 code）。"""

from __future__ import annotations

import secrets
import time
from urllib.parse import quote_plus

import requests

from ..login import GEMINI_SCOPES, GEMINI_TOKEN, gemini_client
from .core import GEMINI_CALLBACK_CODES, Ticket, qr_svg


def start(channel, ticket: Ticket) -> None:
    client_id, _ = gemini_client()
    ticket.kind = "browser"
    ticket.platform = "gemini"
    ticket.state = secrets.token_hex(12)
    ticket.origin = "http://localhost:8199/oauth-callback"
    from ..login import GEMINI_AUTH
    ticket.auth_url = (f"{GEMINI_AUTH}?client_id={client_id}&response_type=code"
                       f"&redirect_uri={quote_plus(ticket.origin)}"
                       f"&scope={quote_plus(' '.join(GEMINI_SCOPES))}"
                       f"&state={ticket.state}&access_type=offline&prompt=consent")
    ticket.qr_content = ticket.auth_url
    ticket.qr_svg = qr_svg(ticket.auth_url)
    ticket.message = "点击「打开授权页」完成 Google 登录授权（需要本机已配置公开常量）"


def poll(ticket: Ticket) -> "Document | None":
    from ..document import parse_auth
    client_id, client_secret = gemini_client()
    code = GEMINI_CALLBACK_CODES.pop(ticket.state, None)
    if not code:
        return None
    tok = requests.post(GEMINI_TOKEN, data={
        "code": code,
        "client_id": client_id, "client_secret": client_secret,
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
