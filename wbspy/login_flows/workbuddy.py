"""WorkBuddy 国内版/国际版：state 两段式轮询授权。"""

from __future__ import annotations

import time
from urllib.parse import quote_plus, urlparse

import requests

from ..document import GLOBAL_DOMAINS, parse_auth
from ..login import _validate_login_url
from .core import Ticket, qr_svg


def start(channel, ticket: Ticket) -> None:
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


def poll(ticket: Ticket) -> "Document | None":
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
