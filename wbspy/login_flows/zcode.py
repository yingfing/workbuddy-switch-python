"""ZCode（智谱）：CLI OAuth 轮询（无回调）。

    参考：dsh-xdpool vendor/channel-pack pack.js zcode-oauth.ts 段。

    流程：POST /api/v1/oauth/cli/init（Bearer 为自生成 64hex 会话密钥）→
    得 flow_id + authorize_url → 用户浏览器授权 →
    GET /api/v1/oauth/cli/poll/{flow_id} 到 status=ready 收 JWT 与第三方 token。
"""

from __future__ import annotations

import secrets
import time
import uuid

import requests

from ..document import parse_auth
from .core import Ticket, qr_svg

ORIGIN = "https://zcode.z.ai"
INIT_URL = f"{ORIGIN}/api/v1/oauth/cli/init"
POLL_URL = f"{ORIGIN}/api/v1/oauth/cli/poll"
APP_VERSION = "3.14.3"
PROVIDER = "bigmodel"


def _headers(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": f"ZCode/{APP_VERSION}",
            "HTTP-Referer": ORIGIN,
            "X-ZCode-App-Version": APP_VERSION,
            "X-Platform": "win32"}


def start(channel, ticket: Ticket) -> None:
    secret = secrets.token_hex(32)          # 自生成会话密钥（对齐官方 CLI 做法）
    resp = requests.post(INIT_URL, json={"provider": PROVIDER},
                         headers=_headers(secret), timeout=30)
    text = resp.text[:200]
    if not resp.ok:
        raise RuntimeError(f"ZCode 授权初始化失败（HTTP {resp.status_code}）：{text}")
    data = (resp.json().get("data") or {}) if "json" in resp.headers.get(
        "content-type", "") else {}
    flow_id = data.get("flow_id")
    url = data.get("authorize_url") or ""
    if not isinstance(flow_id, str) or not flow_id or not url.startswith("https://"):
        raise RuntimeError("ZCode 授权初始化响应缺少 flow_id / authorize_url。")
    interval = data.get("poll_interval_sec")
    ticket.kind = "device"
    ticket.origin = ORIGIN
    ticket.state = flow_id
    ticket.secret = secret
    ticket.auth_url = url
    ticket.qr_content = url
    ticket.qr_svg = qr_svg(url)
    ticket.message = ("在新窗口打开 ZCode 授权页完成登录；"
                      "本流程自动轮询，无需回调")
    try:
        ticket.raw_poll_interval = max(1, int(interval))  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        pass


def poll(ticket: Ticket) -> "Document | None":
    if not ticket.secret:
        raise RuntimeError("ZCode 登录会话状态损坏，请重新发起。")
    try:
        resp = requests.get(f"{POLL_URL}/{ticket.state}",
                            headers=_headers(ticket.secret), timeout=20)
    except requests.RequestException:
        return None
    if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
        raise RuntimeError(f"ZCode 轮询被拒（HTTP {resp.status_code}）")
    if not resp.ok:
        return None
    body = resp.json() if "json" in resp.headers.get("content-type", "") else {}
    if body.get("code") != 0:
        return None
    data = body.get("data") or {}
    status = data.get("status")
    if status == "pending":
        return None
    if status == "failed":
        raise RuntimeError("用户拒绝了授权或授权失败。")
    if status != "ready":
        raise RuntimeError(f"ZCode 轮询状态无法识别：{status}")
    jwt = data.get("token")
    user = data.get("user") or {}
    uid = str(user.get("user_id") or user.get("id") or "")
    if not isinstance(jwt, str) or not jwt or not uid:
        raise RuntimeError("ZCode 轮询响应缺少 token / user_id。")
    third = data.get("zai") or data.get("bigmodel") or {}
    third_token = (third.get("access_token") or third.get("accessToken") or "")
    if not third_token:
        raise RuntimeError("ZCode 轮询响应缺少第三方 access_token。")
    refresh = (third.get("refresh_token") or third.get("refreshToken") or "")
    name = str(user.get("name") or user.get("email") or uid)

    doc = parse_auth({
        "account": {"uid": uid, "nickname": name},
        "auth": {"accessToken": jwt, "refreshToken": refresh,
                 "domain": "zcode.z.ai"},
    })
    doc.raw.setdefault("channel", "zcode")
    doc.raw["zcode"] = {"device_mid": str(uuid.uuid4()),      # 自生成且需稳定
                        "app_version": APP_VERSION, "source": "plugin",
                        "third_party_access_token": third_token,
                        "third_party_kind": ("zai" if data.get("zai") else "bigmodel")}
    return doc
