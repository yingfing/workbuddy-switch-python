"""MiniMax：设备码 + PKCE 轮询。"""

from __future__ import annotations

import requests

from ..document import parse_auth
from ..login import MINIMAX_ACCOUNT, _decode_jwt_exp_ms, _pkce_pair
from .core import Ticket, qr_svg


def parse_minimax_token(payload: dict) -> "Document | None":
    """MiniMax token 轮询响应 → Document；未批准返回 None。"""
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        return None
    exp = _decode_jwt_exp_ms(token)
    doc = parse_auth({
        "account": {"uid": str(payload.get("user_id") or "minimax-user"),
                    "nickname": "MiniMax 账号"},
        "auth": {"accessToken": token, "refreshToken": payload.get("refresh_token", ""),
                 "domain": "account.minimax.cn", "expiresAt": exp},
    })
    doc.raw.setdefault("channel", "minimax")
    return doc


def start(channel, ticket: Ticket) -> None:
    verifier, challenge = _pkce_pair()
    resp = requests.post(f"{MINIMAX_ACCOUNT}/oauth2/device/code", data={
        "client_id": "mcode-public", "scope": "agent.default",
        "audience": "agent-backend", "code_challenge": challenge,
        "code_challenge_method": "S256"}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"MiniMax 设备码服务暂不可用（HTTP {resp.status_code}）")
    data = resp.json()
    ticket.kind = "device"
    ticket.platform = "minimax"
    ticket.origin = MINIMAX_ACCOUNT
    ticket.state = data.get("device_code", "")
    ticket.code = verifier                       # 借 code 字段存 verifier
    ticket.auth_url = (data.get("verification_uri_complete")
                       or data.get("verification_uri", ""))
    ticket.message = f"打开验证链接并输入设备码：{data.get('user_code', '')}"
    ticket.qr_content = ticket.auth_url
    ticket.qr_svg = qr_svg(ticket.auth_url)


def poll(ticket: Ticket) -> "Document | None":
    try:
        resp = requests.post(f"{ticket.origin}/oauth2/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": ticket.state, "client_id": "mcode-public",
            "code_verifier": ticket.code}, timeout=15).json()
    except (requests.RequestException, ValueError):
        return None
    return parse_minimax_token(resp)                 # None = 还没批准
