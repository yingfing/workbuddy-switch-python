"""Qoder 国际版/国内版：设备选择页 + PKCE 轮询（对齐 qoder-oauth.ts）。

说明：Qoder 的**对话推理**走 wasm 加密（qoder-auth-wasm，生成
``Bearer COSY.<载荷>.<签名>``），与登录无关——登录本身是标准的
设备会话 + PKCE 轮询，无需 wasm。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from urllib.parse import quote_plus, urlparse

import requests

from ..document import parse_auth
from .core import Ticket, qr_svg

_PRODUCTS = {
    "qoder": {                                   # 国际版
        "auth": "https://qoder.com",
        "openapi": "https://openapi.qoder.sh",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "domain": "qoder.com",
    },
    "qodercn": {                                 # 国内版
        "auth": "https://qoder.cn",
        "openapi": "https://openapi.qoder.com.cn",
        "client_id": "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa",
        "domain": "qoder.cn",
    },
}
_SELECT_PATH = "/device/selectAccounts"
_POLL_PATH = "/api/v1/deviceToken/poll"
_PKCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def _pkce() -> tuple[str, str]:
    length = 43 + secrets.randbelow(87)          # 43–129，对齐原实现
    verifier = "".join(_PKCE_ALPHABET[b % len(_PKCE_ALPHABET)]
                       for b in secrets.token_bytes(length))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64_url(digest)
    return verifier, challenge


def base64_url(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode().replace("+", "-").replace("/", "_").rstrip("=")


def start(channel, ticket: Ticket) -> None:
    prod = _PRODUCTS.get(channel.id)
    if prod is None:
        raise RuntimeError(f"未知的 Qoder 产品：{channel.id}")
    verifier, challenge = _pkce()
    nonce, machine_id = str(uuid.uuid4()), str(uuid.uuid4())
    url = (f"{prod['auth']}{_SELECT_PATH}?challenge={quote_plus(challenge)}"
           "&challenge_method=S256"
           f"&nonce={quote_plus(nonce)}"
           f"&machine_id={quote_plus(machine_id)}"
           f"&client_id={quote_plus(prod['client_id'])}")
    ticket.kind = "device"
    ticket.platform = "qoder"
    ticket.origin = prod["openapi"]
    ticket.state = nonce
    ticket.code = verifier
    ticket.auth_url = url
    ticket.qr_content = url
    ticket.qr_svg = qr_svg(url)
    ticket.message = ("在新窗口打开 Qoder 授权页选择账号完成登录；"
                      "此处自动等待凭据")


def poll(ticket: Ticket) -> "Document | None":
    channel_id = "qodercn" if "qoder.cn" in (urlparse(ticket.auth_url).hostname or "") \
        else "qoder"
    prod = _PRODUCTS[channel_id]
    try:
        resp = requests.get(
            f"{ticket.origin}{_POLL_PATH}",
            params={"nonce": ticket.state, "verifier": ticket.code,
                    "challenge_method": "S256"},
            headers={"Accept": "application/json"}, timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code == 404:
        return None                              # 等待用户授权
    if not resp.ok:
        raise RuntimeError(f"Qoder 登录服务异常（HTTP {resp.status_code}）。")
    payload = resp.json() if resp.text else {}
    token = (payload.get("token") or payload.get("device_token")
             or payload.get("access_token") or "")
    if not token:
        return None
    uid = payload.get("user_id") or payload.get("userId") or "qoder-user"
    nickname = payload.get("user_name") or payload.get("userName") or "Qoder 账号"
    expires_at = payload.get("expires_at") or payload.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        expires_at = int(expires_at * (1 if expires_at > 1e12 else 1000))
    else:
        expires_at = None
    doc = parse_auth({
        "account": {"uid": str(uid), "nickname": str(nickname)},
        "auth": {"accessToken": token,
                 "refreshToken": payload.get("refresh_token") or "",
                 "domain": prod["domain"], "expiresAt": expires_at},
    })
    doc.raw.setdefault("channel", channel_id)
    # machine_id 是后续 wasm 加密推理的必要输入，随凭据一并保存
    doc.raw["qoderMachineId"] = _ticket_machine_id(ticket)
    return doc


def _ticket_machine_id(ticket: Ticket) -> str:
    # start 时 machine_id 已编码进 auth_url 的 machine_id 参数
    from urllib.parse import parse_qs, urlsplit
    q = parse_qs(urlsplit(ticket.auth_url).query)
    return (q.get("machine_id") or [""])[0]
