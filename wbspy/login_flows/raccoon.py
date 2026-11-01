"""Raccoon（商汤小浣熊）：App 扫码轮询。"""

from __future__ import annotations

import secrets
from urllib.parse import quote_plus, urlparse

import requests

from ..document import parse_auth
from ..login import RACCOON_AUTH_PREFIX, _decode_jwt_exp_ms
from .core import Ticket, qr_svg


def start(channel, ticket: Ticket) -> None:
    code = secrets.token_hex(16)
    ticket.kind = "qr"
    ticket.code = code
    ticket.origin = channel.origin
    ticket.qr_content = f"{channel.origin}/login/mp?code={code}&appname={quote_plus('商汤小浣熊官网')}"
    ticket.qr_svg = qr_svg(ticket.qr_content)
    ticket.message = "请用「商汤小浣熊」App 扫码"


def poll(ticket: Ticket) -> "Document | None":
    from ..document import Document  # noqa: F401  (类型引用)
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
