"""Cline：WorkOS 设备码 + token 注册（对齐 pollClineWorkOsTokens）。"""

from __future__ import annotations

import requests

from ..document import parse_auth
from ..login import _decode_jwt_exp_ms
from .core import Ticket

_WORKOS = "https://api.workos.com"
_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
_CLIENT_HEADERS = {"Content-Type": "application/json",
                   "HTTP-Referer": "https://cline.bot", "X-Title": "Cline",
                   "X-IS-MULTIROOT": "false", "X-CLIENT-TYPE": "cline-sdk"}


def start(channel, ticket: Ticket) -> None:
    resp = requests.post(
        f"{_WORKOS}/user_management/authorize/device",
        data={"client_id": _CLIENT_ID},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15)
    data = resp.json() if resp.ok else {}
    device_code = data.get("device_code") or ""
    user_code = data.get("user_code") or ""
    url = data.get("verification_uri_complete") or data.get("verification_uri") or ""
    if not (device_code and user_code and url):
        raise RuntimeError(f"WorkOS 设备码授权失败（HTTP {resp.status_code}）。")
    ticket.kind = "device"
    ticket.platform = "cline"
    ticket.origin = _WORKOS
    ticket.state = device_code
    ticket.code = user_code
    ticket.auth_url = url
    ticket.message = (f"在新窗口打开 Cline 授权页并确认设备码 "
                      f"{user_code}；此处自动等待")


def poll(ticket: Ticket) -> "Document | None":
    try:
        resp = requests.post(
            f"{_WORKOS}/user_management/authenticate",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                  "device_code": ticket.state, "client_id": _CLIENT_ID},
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
    reg = requests.post("https://api.cline.bot/api/v1/auth/register",
                        json={"accessToken": access, "refreshToken": refresh},
                        headers=_CLIENT_HEADERS, timeout=20)
    reg_data = reg.json() if reg.ok else {}
    inner = reg_data.get("data") if isinstance(reg_data.get("data"), dict) else reg_data
    token = inner.get("accessToken") or inner.get("access_token") or access
    exp_ms = _decode_jwt_exp_ms(token) if isinstance(token, str) else None
    account = (inner.get("userInfo") or {}) if isinstance(inner, dict) else {}
    doc = parse_auth({
        "account": {"uid": str(account.get("clineUserId") or account.get("accountId")
                               or inner.get("accountId") or "cline-user"),
                    "nickname": (f"{account.get('firstName', '')} "
                                 f"{account.get('lastName', '')}".strip()
                                 or account.get("email") or "Cline 账号")},
        "auth": {"accessToken": token, "refreshToken": refresh,
                 "domain": "api.cline.bot", "expiresAt": exp_ms},
    })
    doc.raw.setdefault("channel", "cline")
    return doc
