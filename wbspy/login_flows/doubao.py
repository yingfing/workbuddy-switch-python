"""豆包（豆包工作 / 豆包电脑版）：passport 协议级扫码登录。

协议来源（2026-11 修订，参照 wangchuxiaoji-oss/doubao2api qr_login.py，
其移植自 LiveSub QRLogin.cpp，实测可稳定拿到 sessionid）：

关键修复——旧实现裸调 get_qrcode（aid=582478 桌面客户端协议），缺
ttwid/csrf 前置会话，App 端「确认」后服务端不下发会话 Cookie：
1. 会话暖身：先 GET www.doubao.com 主页收集 ttwid 等基础 Cookie；
2. GET /passport/safe/csrf_token/ 拿 passport_csrf_token；
3. 出码 GET /passport/web/get_qrcode/?aid=497858（Web 端 aid），
   请求头带 x-tt-passport-csrf-token；
4. 轮询 GET /passport/web/check_qrconnect/（同样带 csrf 头 + 全量 Cookie），
   data.status: new / scanning / confirmed / expired；
5. confirmed 后手动跟随 redirect_url 整条重定向链，
   逐跳收集 Set-Cookie（sessionid 常挂在中间跳）。

全程同一 Cookie 罐累积，任何一跳的会话 Cookie 都不丢。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket

_BASE = "https://www.doubao.com"
_GET_QRCODE = f"{_BASE}/passport/web/get_qrcode/"
_CHECK_URL = f"{_BASE}/passport/web/check_qrconnect/"
_CSRF_URL = f"{_BASE}/passport/safe/csrf_token/"
_AID = "497858"                     # 豆包 Web 端 aid（doubao2api 实测可用）
_NEXT = quote_plus("https://www.doubao.com", safe="")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_BASE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.doubao.com/chat/login",
    "Origin": "https://www.doubao.com",
}
_STATUS_MAP = {"new": "waiting", "1": "waiting", "0": "waiting",
               "scanning": "scanned", "scanned": "scanned", "2": "scanned", "5": "scanned",
               "confirmed": "confirmed", "success": "confirmed", "3": "confirmed", "6": "confirmed",
               "expired": "expired", "4": "expired", "7": "expired",
               "canceled": "canceled", "cancelled": "canceled", "8": "canceled"}


def _uid_from_cookie(cookie: str) -> str:
    """sessionid 是稳定身份（一个账号一个 sessionid 值域）→ md5 前 12 位。"""
    m = re.search(r"(?:^|;\s*)sessionid=([^;]+)", cookie)
    seed = m.group(1) if m else cookie
    return "doubao-" + hashlib.md5(seed.encode()).hexdigest()[:12]


def _cookie_header(session: requests.Session) -> str:
    """会话全程累积的 Cookie —— sessionid 常分散在重定向链多跳响应里。"""
    parts = []
    seen = set()
    for c in session.cookies:
        if c.name and c.name not in seen:
            seen.add(c.name)
            parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def _csrf_token(session: requests.Session) -> str:
    """主页暖身 + csrf 端点，返回 passport_csrf_token。"""
    try:
        session.get(f"{_BASE}/", headers=_BASE_HEADERS, timeout=20)
    except requests.RequestException:
        pass
    for name in ("passport_csrf_token", "passport_csrf_token_default"):
        v = session.cookies.get(name)
        if v:
            return v
    try:
        resp = session.get(f"{_CSRF_URL}?aid={_AID}", headers=_BASE_HEADERS, timeout=20)
        data = resp.json() if resp.ok else {}
        v = str(((data.get("data") or {}).get("passport_csrf_token")) or "")
        if v:
            return v
    except (requests.RequestException, ValueError):
        pass
    return session.cookies.get("passport_csrf_token", "")


def _state_dump(token: str, csrf: str, session: requests.Session) -> str:
    return json.dumps({"token": token, "csrf": csrf, "cookies": _cookie_header(session)})


def start(channel, ticket: Ticket) -> None:
    session = requests.Session()
    session.headers.update(_BASE_HEADERS)
    csrf = _csrf_token(session)
    headers = dict(_BASE_HEADERS)
    if csrf:
        headers["x-tt-passport-csrf-token"] = csrf
    params = f"next={_NEXT}&aid={_AID}"
    resp = session.get(f"{_GET_QRCODE}?{params}", headers=headers, timeout=20)
    doc = resp.json() if resp.ok else {}
    data = doc.get("data") or {}
    token = str(data.get("token") or "")
    image = str(data.get("base64_image") or data.get("qrcode") or "")
    err = data.get("error_code", doc.get("error_code", 0))
    if err not in (0, "0", None) or not token:
        detail = str(data.get("description") or doc.get("message")
                     or f"HTTP {resp.status_code}")[:160]
        raise RuntimeError(f"豆包二维码获取失败：{detail}")
    ticket.kind = "qr"
    ticket.platform = "doubao-qr"
    ticket.origin = "accounts.doubao.com"
    ticket.state = _state_dump(token, csrf, session)
    if image.startswith("data:image"):
        # 服务端直接给 PNG data URI → 用 <img> 渲染。绝不塞进 ticket.code：
        # 那是前端「复制码」栏（点击复制），几万字符的 data URI 会原样展示
        ticket.qr_svg = f'<img src="{image}" alt="豆包扫码" width="220"/>'
    elif image:
        ticket.qr_content = image            # 非 data URI 时交通用二维码渲染器
    ticket.message = "请用手机「豆包」App 扫一扫并确认登录（豆包工作/豆包电脑版）"


def poll(ticket: Ticket) -> "Document | None":
    try:
        st = json.loads(ticket.state)
    except ValueError:
        raise RuntimeError("登录会话状态损坏，请重新发起登录。")
    token, csrf, jar = st.get("token", ""), st.get("csrf", ""), st.get("cookies", "")
    session = requests.Session()
    for part in jar.split("; "):
        if "=" in part:
            k, _, v = part.partition("=")
            session.cookies.set(k, v)
    headers = dict(_BASE_HEADERS)
    if csrf:
        headers["x-tt-passport-csrf-token"] = csrf
    params = f"next={_NEXT}&token={quote_plus(token)}&aid={_AID}"
    resp = session.get(f"{_CHECK_URL}?{params}", headers=headers, timeout=20)
    if not resp.ok:
        ticket.state = _state_dump(token, csrf, session)
        return None
    data = (resp.json() or {}).get("data") or {}
    status = _STATUS_MAP.get(str(data.get("status") or "").lower().strip())
    if status == "scanned":
        ticket.message = "已扫码，请在手机上确认登录"
        ticket.state = _state_dump(token, csrf, session)
        return None
    if status in ("expired", "canceled"):
        raise RuntimeError("二维码已失效或被取消，请重新发起登录。")
    if status != "confirmed":
        ticket.state = _state_dump(token, csrf, session)
        return None
    # confirmed 后服务端下发 redirect_url（会话 Cookie 挂在跳转链上）——手动逐跳跟
    redirect_url = str(data.get("redirect_url") or data.get("next_url") or "")
    if redirect_url:
        for _ in range(10):
            try:
                r = session.get(redirect_url, headers=_BASE_HEADERS,
                                timeout=20, allow_redirects=False)
            except requests.RequestException:
                break
            loc = r.headers.get("Location", "")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                redirect_url = loc
                continue
            break
    cookie = _cookie_header(session)
    if not re.search(r"(?:^|;\s*)sessionid=", cookie):
        # 确认了但会话没下来——如实报错让用户重试
        detail = str(data.get("description") or "")[:120]
        raise RuntimeError("登录确认未返回会话 Cookie" + (f"：{detail}" if detail else "，请重试。"))
    ticket.message = f"登录成功（{time.strftime('%H:%M:%S')}）"
    return parse_auth({
        "account": {"uid": _uid_from_cookie(cookie), "nickname": "豆包账号"},
        "auth": {"accessToken": cookie, "domain": "www.doubao.com"},
        "channel": "doubao",
    })
