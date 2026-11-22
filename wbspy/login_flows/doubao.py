"""豆包（豆包工作 / 豆包电脑版）：passport 协议级扫码登录。

协议来源（2026-11，参照 doubao2api 逆向实现互证）：
- 出码  GET https://accounts.doubao.com/passport/web/get_qrcode/
        query: aid=582478（豆包工作 PC 客户端 aid，客户端 JS 常量表实锤
        isInSamanthaDoubaoWorkClientPhysically() ? 582478 : 482431）+ jssdk 参数；
        带 real_aid 会被判「非法应用」——绝不能带。
        响应 data.qrcode 即 data:image/png;base64 图片，data.token 供轮询。
- 轮询 GET https://accounts.doubao.com/passport/web/check_qrconnect/
        data.status: new / scanning / confirmed / expired（也兼容数字 1/2/3/4）；
        confirmed 时响应的 Set-Cookie 就是完整会话（sessionid/sid_tt/ttwid…）。
- 已知限制：手机豆包 App 扫码后的「确认」环节偶有失败（社区实测），
  失败如实报错，重试即可；额度仅个人账号可领（飞书企业号被排除）。
"""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket

_GET_QRCODE = "https://accounts.doubao.com/passport/web/get_qrcode/"
_CHECK_URL = "https://accounts.doubao.com/passport/web/check_qrconnect/"
_DOUBAO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 "
              "SamanthaDoubao/2.36.14")
# doubao2api 实测组合：带 real_aid 会「非法应用」，千万别加
_QR_QUERY = {
    "aid": "582478",
    "language": "zh",
    "next": "https://www.doubao.com/",
    "is_from_ttaccountsdk": "1",
    "passport_jssdk_version": "4.4.6",
    "passport_jssdk_type": "normal",
    "sdk_version": "2.2.11-doubao.0",
    "pkg_type": "release_version",
    "version_code": "20800",
    "device_platform": "desktop",
}
_STATUS_MAP = {"new": "waiting", "1": "waiting", "0": "waiting",
               "scanning": "scanned", "scanned": "scanned", "2": "scanned", "5": "scanned",
               "confirmed": "confirmed", "success": "confirmed", "3": "confirmed", "6": "confirmed",
               "expired": "expired", "4": "expired", "7": "expired",
               "canceled": "canceled", "cancelled": "canceled", "8": "canceled"}


def _headers() -> dict:
    return {"User-Agent": _DOUBAO_UA, "Referer": "https://www.doubao.com/",
            "Accept": "application/json"}


def _uid_from_cookie(cookie: str) -> str:
    """sessionid 是稳定身份（一个账号一个 sessionid 值域）→ md5 前 12 位。"""
    m = re.search(r"(?:^|;\s*)sessionid=([^;]+)", cookie)
    seed = m.group(1) if m else cookie
    return "doubao-" + hashlib.md5(seed.encode()).hexdigest()[:12]


def _collect_cookie(resp: requests.Response) -> str:
    parts = []
    for c in resp.cookies:
        if c.name:
            parts.append(f"{c.name}={c.value}")
    return "; ".join(parts)


def start(channel, ticket: Ticket) -> None:
    params = "&".join(f"{k}={quote_plus(v)}" for k, v in _QR_QUERY.items())
    resp = requests.get(f"{_GET_QRCODE}?{params}", headers=_headers(), timeout=20)
    doc = resp.json() if resp.ok else {}
    data = doc.get("data") or {}
    token = str(data.get("token") or "")
    image = str(data.get("base64_image") or data.get("qrcode") or "")
    if data.get("error_code") not in (0, "0", None) or not token:
        detail = str(data.get("description") or doc.get("message")
                     or f"HTTP {resp.status_code}")[:160]
        raise RuntimeError(f"豆包二维码获取失败：{detail}")
    ticket.kind = "qr"
    ticket.platform = "doubao-qr"
    ticket.origin = "accounts.doubao.com"
    ticket.state = token
    if image.startswith("data:image"):
        # 服务端直接给 PNG data URI → 用 <img> 渲染。绝不塞进 ticket.code：
        # 那是前端「复制码」栏（点击复制），几万字符的 data URI 会原样展示
        ticket.qr_svg = f'<img src="{image}" alt="豆包扫码" width="220"/>'
    elif image:
        ticket.qr_content = image            # 非 data URI 时交通用二维码渲染器
    ticket.message = "请用手机「豆包」App 扫一扫并确认登录（豆包工作/豆包电脑版）"


def poll(ticket: Ticket) -> "Document | None":
    params = _QR_QUERY.copy()
    params["token"] = ticket.state
    qs = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
    resp = requests.get(f"{_CHECK_URL}?{qs}", headers=_headers(), timeout=20)
    if not resp.ok:
        return None
    data = (resp.json() or {}).get("data") or {}
    status = _STATUS_MAP.get(str(data.get("status") or "").lower().strip())
    if status == "scanned":
        ticket.message = "已扫码，请在手机上确认登录"
        return None
    if status in ("expired", "canceled"):
        raise RuntimeError("二维码已失效或被取消，请重新发起登录。")
    if status != "confirmed":
        return None
    cookie = _collect_cookie(resp)
    if not re.search(r"(?:^|;\s*)sessionid=", cookie):
        # 确认了但会话没下来（App 端偶发）——如实报错让用户重试
        detail = str(data.get("description") or "")[:120]
        raise RuntimeError("登录确认未返回会话 Cookie" + (f"：{detail}" if detail else "，请重试。"))
    ticket.message = f"登录成功（{time.strftime('%H:%M:%S')}）"
    return parse_auth({
        "account": {"uid": _uid_from_cookie(cookie), "nickname": "豆包账号"},
        "auth": {"accessToken": cookie, "domain": "www.doubao.com"},
        "channel": "doubao",
    })
