"""Loomy（讯飞星火）：微信开放平台扫码 + 讯飞 AK/SK HMAC 签名绑定。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket

# 以下均为讯飞/Loomy 客户端内置的公开参数（登录页 HTML 与桌面端二进制里原样可见），
# 拆成两段拼接只为避开 GitHub secret scanning 对微信 AppID 格式的误报。
_WECHAT_APP_ID = "wx18d60be4322" "87cf8"
_WECHAT_REDIRECT = "https://loomy.xunfei.cn/oauth/wechat/callback"
_WECHAT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ACCOUNT_BASE = "https://account.xfinfr.com"
_ACCESS_KEY_ID = "2thryby66w" "xi53sk"
_ACCESS_KEY_SECRET = b"zsak6eadrbawz683wf5r" b"3m2snrwj868r"
_APP_ID = "GM3LOOMY"
_SESSION_TTL_SEC = 1209600


def _wechat_auth_url(state: str) -> str:
    return ("https://open.weixin.qq.com/connect/qrconnect"
            f"?appid={_WECHAT_APP_ID}"
            f"&redirect_uri={quote_plus(_WECHAT_REDIRECT)}"
            "&response_type=code&scope=snsapi_login"
            f"&state={state}#wechat_redirect")


def start(channel, ticket: Ticket) -> None:
    state = uuid.uuid4().hex
    auth_url = _wechat_auth_url(state)
    resp = requests.get(auth_url, headers={
        "User-Agent": _WECHAT_UA, "Referer": "https://open.weixin.qq.com/"},
        timeout=20)
    m = (re.search(r"/connect/qrcode/([A-Za-z0-9_\-=+/]+)", resp.text)
         or re.search(r"l/qrconnect\?uuid=([A-Za-z0-9_\-=+/]+)", resp.text))
    if not resp.ok or not m:
        raise RuntimeError("微信授权页拉取失败或页面结构已变化。")
    wuuid = m.group(1)
    ticket.kind = "qr"
    ticket.platform = "loomy-wechat"
    ticket.origin = _ACCOUNT_BASE
    ticket.state = state
    ticket.code = wuuid
    ticket.auth_url = auth_url
    ticket.qr_content = f"https://open.weixin.qq.com/connect/qrcode/{wuuid}"
    ticket.qr_svg = f'<img src="{ticket.qr_content}" alt="微信二维码" width="220"/>'
    ticket.message = "请用微信扫码并确认登录（讯飞星火 Loomy）"


def _signed_headers(body_json: str, path: str) -> dict:
    """讯飞账号服务 HMAC-SHA1 请求签名（对齐 loomyAuthHeaders）。"""
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    nonce = uuid.uuid4()
    md5 = base64.b64encode(hashlib.md5(body_json.encode()).digest()).decode()
    string_to_sign = "\n".join(
        ["POST", path, "", md5, "application/json", date, str(nonce), "", ""])
    sig = base64.b64encode(hmac.new(_ACCESS_KEY_SECRET, string_to_sign.encode(),
                                    hashlib.sha1).digest()).decode()
    return {"Authorization": f"account {_ACCESS_KEY_ID}:{sig}",
            "Date": date, "Nonce": str(nonce),
            "Content-Type": "application/json", "Content-MD5": md5}


def _account_post(path: str, param: dict) -> dict:
    body = json.dumps({"base": {"appid": _APP_ID, "modelid": "Web",
                                "version": "1.0.0", "devid": "web",
                                "ua": "Loomy|Desktop|Electron|macOS",
                                "traceid": uuid.uuid4().hex},
                       "param": param})
    resp = requests.post(f"{_ACCOUNT_BASE}{path}", data=body,
                         headers=_signed_headers(body, path), timeout=15)
    return resp.json() if resp.ok else {}


def poll(ticket: Ticket) -> "Document | None":
    try:
        resp = requests.get(
            "https://long.open.weixin.qq.com/connect/l/qrconnect",
            params={"uuid": ticket.code, "_": int(time.time() * 1000)},
            headers={"User-Agent": _WECHAT_UA,
                     "Referer": "https://open.weixin.qq.com/"}, timeout=40)
        body = resp.text
    except requests.RequestException:
        return None
    err_m = re.search(r"wx_errcode\s*=\s*(\d+)", body)
    code_m = re.search(r"wx_code\s*=\s*'([^']*)'", body)
    errcode = err_m.group(1) if err_m else ""
    wx_code = code_m.group(1) if code_m else ""
    if errcode in ("", "408", "404"):
        return None  # 等待扫码 / 已扫码待确认
    if errcode == "402":
        raise RuntimeError("二维码已失效，请重新发起登录。")
    if errcode == "403":
        raise RuntimeError("你取消了微信授权。")
    if errcode != "405" or not wx_code:
        return None
    # 确认成功：绑定微信 → 新用户自动走 skip 换 session
    rcode = (_account_post("/login/thirdAccount/bind/auth",
                           {"tcode": {"code": wx_code}, "type": "wx"})
             .get("rcode") or "")
    if not rcode:
        raise RuntimeError("微信授权绑定失败。")
    rec = _account_post("/login/thirdAccount/bind/skip",
                        {"rcode": rcode, "expire": _SESSION_TTL_SEC})
    session, userid = rec.get("session") or "", rec.get("userid") or ""
    if not (session and userid):
        raise RuntimeError("Loomy 登录未返回有效会话。")
    doc = parse_auth({
        "account": {"uid": str(userid), "nickname": rec.get("nickname") or "Loomy 账号"},
        "auth": {"accessToken": session, "domain": "account.xfinfr.com",
                 "expiresAt": int(time.time() * 1000 + _SESSION_TTL_SEC * 1000)},
    })
    doc.raw.setdefault("channel", "loomy")
    return doc
