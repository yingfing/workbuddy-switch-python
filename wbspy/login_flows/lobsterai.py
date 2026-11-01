"""LobsterAI（有道）：portal 登录回调 + authCode 换凭据。

    参考：dsh-xdpool vendor/channel-pack pack.js lobsterai-oauth.ts 段。

    流程：拼 {portal}/portal#/login?source=electron&redirect_uri=…&state=… →
    用户在官方页面登录 → 302 回调 redirect_uri 带 code&state →
    /authorize 端点投递 → poll() POST /api/auth/exchange 换 token。
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket, qr_svg

API_BASE = "https://lobsterai-server.youdao.com"
PORTAL_BASE = "https://lobsterai.youdao.com"
VERSION_API = ("https://api-overmind.youdao.com"
               "/openapi/get/luna/hardware/lobsterai/prod/update")
FALLBACK_VERSION = "2026.9.4"
USER_AGENT = "LobsterAI/0.1.0"
EXCHANGE_PATH = "/api/auth/exchange"


def _public_base() -> str:
    return (os.environ.get("WBSPY_PUBLIC_ORIGIN") or
            "http://127.0.0.1:8178").rstrip("/")


def _client_version() -> str:
    """客户端版本：上游更新接口优先，失败回退内置常量（不抛错）。"""
    try:
        resp = requests.get(VERSION_API, headers={"Accept": "application/json",
                                                  "User-Agent": USER_AGENT},
                            timeout=15)
        if resp.ok:
            value = ((resp.json().get("data") or {}).get("value") or {})
            v = str(value.get("version") or "").strip()
            if v and v.replace(".", "").isdigit():
                return v
    except (requests.RequestException, ValueError):
        pass
    return FALLBACK_VERSION


def start(channel, ticket: Ticket) -> None:
    session_uuid = str(uuid.uuid4())
    state = str(uuid.uuid4())
    first_keyfrom = str(int(time.time() * 1000))

    sharecode = os.environ.get("WBSPY_PUBLIC_SHARECODE", "")
    callback = f"{_public_base()}/authorize"
    if sharecode:
        callback += f"?sharecode={quote_plus(sharecode)}"

    url = (f"{PORTAL_BASE}/portal#/login?source=electron"
           f"&redirect_uri={quote_plus(callback)}&state={quote_plus(state)}")

    ticket.kind = "device"
    ticket.origin = API_BASE
    ticket.state = state
    ticket.code = ""                      # code 由回调投递
    ticket.secret = f"{session_uuid}|{first_keyfrom}"   # 会话私有，不进 public()
    ticket.auth_url = url
    ticket.qr_content = url
    ticket.qr_svg = qr_svg(url)
    ticket.message = ("在新窗口打开 LobsterAI 官方登录页完成登录；"
                      "登录后浏览器会自动跳回本服务，无需手动操作")


def poll(ticket: Ticket) -> "Document | None":
    cb = ticket.callback
    if not cb:
        return None
    code = str(cb.get("code") or "")
    if not code:
        raise RuntimeError("LobsterAI 回调缺少 code 参数。")
    if "|" not in ticket.secret:
        raise RuntimeError("LobsterAI 登录会话状态损坏，请重新发起。")
    session_uuid, first_keyfrom = ticket.secret.split("|", 1)

    resp = requests.post(
        f"{API_BASE}{EXCHANGE_PATH}",
        json={"authCode": code, "firstKeyfrom": first_keyfrom,
              "latestKeyfrom": str(int(time.time() * 1000)),
              "uuid": session_uuid, "version": _client_version()},
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
        timeout=30)
    body = resp.json() if resp.ok else {}
    if body.get("code") != 0:
        msg = body.get("msg") or body.get("message") or f"HTTP {resp.status_code}"
        raise RuntimeError(f"LobsterAI 换取凭据失败：{msg}")
    data = body.get("data") or {}
    token = str(data.get("accessToken") or "")
    if not token:
        raise RuntimeError("LobsterAI 换取凭据失败：响应缺少 accessToken。")
    refresh = str(data.get("refreshToken") or "")
    user = data.get("user") or {}
    expires_in = data.get("expiresIn")
    exp_ms = (int(time.time() * 1000 + int(expires_in) * 1000)
              if isinstance(expires_in, (int, float)) and expires_in > 0 else 0)
    uid = (str(user.get("id") or user.get("userId") or user.get("yid") or ""))[:64] \
        or "lobsterai-user"
    nickname = str(user.get("nickname") or "") or f"LobsterAI {uid[-4:]}"

    doc = parse_auth({
        "account": {"uid": uid, "nickname": nickname},
        "auth": {"accessToken": token, "refreshToken": refresh,
                 "domain": "lobsterai-server.youdao.com", "expiresAt": exp_ms},
    })
    doc.raw.setdefault("channel", "lobsterai")
    doc.raw["lobsterai"] = {"uuid": session_uuid, "firstKeyfrom": first_keyfrom,
                            "latestKeyfrom": str(int(time.time() * 1000))}
    return doc
