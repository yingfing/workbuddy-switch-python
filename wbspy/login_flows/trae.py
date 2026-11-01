"""TRAE：授权页回调 + RefreshToken/PKCE 双路 ExchangeToken。

    参考：WorkBuddy-Switch desktop/editor-oauth.mjs（PKCE 路）+
    dsh-xdpool vendor/channel-pack pack.js trae-oauth 段（refreshToken 直换路，
    端点与版本常量以其为准 —— pack.js 更新且带实测注释）。

    流程：拼授权 URL（含 auth_callback_url 指回本服务 /authorize）→
    用户在 TRAE 授权页登录 → 上游 302 回调带 refreshToken/userJwt/PKCE code →
    /authorize 端点投递进 ticket.callback → poll() 换取正式凭据。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from urllib.parse import quote_plus

import requests

from ..document import parse_auth
from .core import Ticket, qr_svg

CONSOLE_HOST = "https://www.trae.cn"          # 授权页
OAUTH_HOST = "https://api.trae.com.cn"        # 换票 / 用户信息
ACCOUNT_ORIGIN = "https://api.trae.cn"        # PKCE 路 ExchangeToken（WBS 源）
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.52"
IDE_VERSION_CODE = "20260811"
PLUGIN_VERSION = "2.3.62834"
USER_AGENT = "Trae/0.1.52"
EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
USER_INFO_PATH = "/cloudide/api/v3/trae/GetUserInfo"
PKCE_EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"


def _public_base() -> str:
    """回调基地址：环境变量优先（公网部署），否则本机 web 端口。"""
    return (os.environ.get("WBSPY_PUBLIC_ORIGIN") or
            "http://127.0.0.1:8178").rstrip("/")


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def start(channel, ticket: Ticket) -> None:
    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    trace_id = (machine_id + device_id)[-16:]
    verifier, challenge = _pkce()

    sharecode = os.environ.get("WBSPY_PUBLIC_SHARECODE", "")
    callback = f"{_public_base()}/authorize"
    if sharecode:
        callback += f"?sharecode={quote_plus(sharecode)}"

    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": CLIENT_ID,
        "redirect": "0",
        "login_trace_id": trace_id,
        "auth_callback_url": callback,
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": "PC",
        "x_device_type": "PC",
        "x_os_version": "1.0",
        "x_app_version": IDE_VERSION,
        "x_app_type": "stable",
        # PKCE：上游若走授权码流程，凭 code+verifier 换票
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "code_verifier_session": "1",
    }
    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    url = f"{CONSOLE_HOST}/authorization?{query}"

    ticket.kind = "device"
    ticket.origin = OAUTH_HOST
    ticket.state = trace_id
    ticket.secret = verifier
    ticket.auth_url = url
    ticket.qr_content = url
    ticket.qr_svg = qr_svg(url)
    ticket.message = ("在新窗口打开 TRAE 授权页完成登录；"
                      "登录后浏览器会自动跳回本服务，无需手动操作")


def _parse_callback(cb: dict) -> dict:
    """解析 /authorize 投递的查询参数（对齐 parseTraeCallbackDetailed）。"""

    def json_param(raw: str | None):
        if not raw:
            return None
        for candidate in (raw,):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
        return None

    def text(src: dict | None, key: str) -> str:
        if not src:
            return ""
        v = src.get(key)
        return v if isinstance(v, str) else ""

    user_info = json_param(cb.get("userInfo"))
    user_jwt = json_param(cb.get("userJwt"))
    auth_code_info = json_param(cb.get("authCodeInfo"))

    refresh = cb.get("refreshToken") or text(user_jwt, "RefreshToken")
    jwt_token = text(user_jwt, "Token")
    uid = text(user_info, "UserID")
    nickname = text(user_info, "ScreenName")

    code = (cb.get("code") or cb.get("authCode") or
            text(auth_code_info, "code") or text(auth_code_info, "authCode") or
            (cb.get("authCodeInfo") or "").strip())
    return {"refresh": refresh, "jwt": jwt_token, "uid": uid,
            "nickname": nickname, "code": code}


def _exchange_refresh(refresh: str) -> dict:
    resp = requests.post(
        f"{OAUTH_HOST}{EXCHANGE_PATH}",
        json={"ClientID": CLIENT_ID, "RefreshToken": refresh,
              "ClientSecret": "-", "UserID": ""},
        headers={"Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=20)
    if not resp.ok:
        raise RuntimeError(f"TRAE ExchangeToken 失败（HTTP {resp.status_code}）")
    return resp.json()


def _exchange_pkce(code: str, verifier: str) -> dict:
    device = {"DeviceID": secrets.token_hex(16), "MachineID": secrets.token_hex(16),
              "PlatformCode": "IDE_PC", "DeviceType": "PC", "DeviceName": "wbspy",
              "DeviceModel": "x86_64", "ClientVersion": IDE_VERSION,
              "DevicePublicKey": "", "DeviceBrand": "", "DeviceCPU": "",
              "OSInfo": "linux", "OSVersion": "1.0"}
    resp = requests.post(
        f"{ACCOUNT_ORIGIN}{PKCE_EXCHANGE_PATH}",
        json={"ClientID": CLIENT_ID, "AuthCode": code,
              "CodeVerifier": verifier, "DeviceInfo": device,
              "IDEVersion": IDE_VERSION},
        headers={"Content-Type": "application/json",
                 "x-cloudide-token": "", "x-device-id": device["DeviceID"],
                 "x-app-id": APP_ID, "x-platform-code": "IDE_PC"},
        timeout=20)
    if not resp.ok:
        raise RuntimeError(f"TRAE PKCE ExchangeToken 失败（HTTP {resp.status_code}）")
    return resp.json()


def _extract_tokens(data: dict) -> tuple[str, str, int]:
    raw = data.get("Result") or data.get("result") or data or {}
    token = (raw.get("Token") or raw.get("token") or
             raw.get("AccessToken") or raw.get("access_token") or "")
    refresh = raw.get("RefreshToken") or raw.get("refreshToken") or ""
    exp = raw.get("TokenExpireAt") or raw.get("tokenExpireAt") or 0
    try:
        exp_ms = int(exp) if exp else 0
    except (TypeError, ValueError):
        exp_ms = 0
    if exp_ms and exp_ms < 10**12:
        exp_ms *= 1000
    if not token:
        raise RuntimeError("TRAE 换票响应缺少 Token。")
    return token, refresh, exp_ms


def _user_info(token: str) -> dict:
    try:
        resp = requests.post(
            f"{OAUTH_HOST}{USER_INFO_PATH}",
            json={"ReqSource": "IDE", "IDEVersion": IDE_VERSION},
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": USER_AGENT, "X-Cloudide-Token": token},
            timeout=20)
        if resp.ok:
            return resp.json().get("Result") or resp.json().get("result") or {}
    except requests.RequestException:
        pass
    return {}


def poll(ticket: Ticket) -> "Document | None":
    if not ticket.callback:
        return None
    cb = _parse_callback(ticket.callback)
    if cb["refresh"]:
        data = _exchange_refresh(cb["refresh"])
    elif cb["code"] and ticket.secret:
        data = _exchange_pkce(cb["code"], ticket.secret)
    elif cb["jwt"]:
        data = {"Result": {"Token": cb["jwt"], "RefreshToken": ""}}
    else:
        raise RuntimeError("TRAE 回调未携带 refreshToken / userJwt / 授权码。")
    token, refresh, exp_ms = _extract_tokens(data)
    info = _user_info(token) if cb["refresh"] or cb["code"] else {}
    uid = (cb["uid"] or info.get("UserID") or info.get("userId") or
           info.get("uid") or "trae-user")
    nickname = (cb["nickname"] or info.get("ScreenName") or
                info.get("screenName") or "")
    if isinstance(nickname, str) and not nickname.strip():
        nickname = f"TRAE 用户 {uid[-4:]}"
    doc = parse_auth({
        "account": {"uid": str(uid), "nickname": str(nickname)},
        "auth": {"accessToken": token, "refreshToken": refresh,
                 "domain": "api.trae.com.cn",
                 "expiresAt": exp_ms or int(time.time() * 1000 + 14 * 86400 * 1000)},
    })
    doc.raw.setdefault("channel", "trae")
    return doc
