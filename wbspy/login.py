"""统一登录入口：能扫码直接扫码，不能扫码再读本机。

三层回退（与用户要求一致）：

1. **qr** —— 协议级扫码。Raccoon（商汤小浣熊）协议完整移植自 dsh-xdpool
   channel pack 的 raccoon-oauth.ts：本地生成 32 位 hex code，二维码内容
   ``{apiBase}/login/mp?code=...&appname=商汤小浣熊官网``，轮询
   ``POST {authApiPrefix}/login_with_qrcode_code`` 直到 success 拿 token。
   终端用 ``qrcode`` 库渲染 ASCII 二维码（未安装则打印链接）。
2. **browser** —— 网页授权。WorkBuddy 国内/国际版：POST
   ``/v2/plugin/auth/state?platform=...`` 拿 state 与 authUrl → authUrl 同时
   渲染为二维码（手机扫码打开官方登录页，页面上可再扫码/短信）并用
   ``webbrowser.open`` 打开本机浏览器 → 轮询 ``GET /v2/plugin/auth/token``
   → 拿 accessToken 后 ``GET /v2/plugin/login/account`` 取档案组装文档。
3. **local** —— 读本机桌面客户端登录文件（WorkBuddy CN/Global、Trae、
   Qoder），零交互兜底。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import webbrowser
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse

import requests

from .channels import Channel, get_channel, methods_of
from .document import Document, GLOBAL_DOMAINS, parse_auth

LOGIN_TIMEOUT_SEC = 300
POLL_INTERVAL_SEC = 3

# 登录 URL 的白名单校验（对应原版 officialLoginURL）
LOGIN_HOSTS = {
    "workbuddy": {"www.codebuddy.cn", "www.workbuddy.cn"},
    "workbuddy-global": {"www.workbuddy.ai", "workbuddy.ai"},
}


def _render_qr_in_terminal(content: str) -> None:
    """终端 ASCII 二维码；qrcode 库缺失时退化为打印链接。"""
    try:
        import qrcode  # type: ignore
    except ImportError:
        print(f"  二维码内容（安装 qrcode 库可显示图形）：{content}")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def _validate_login_url(url: str, provider: str) -> str:
    parsed = urlparse(url)
    hosts = LOGIN_HOSTS[provider]
    if parsed.scheme != "https" or parsed.hostname not in hosts or \
            parsed.username or parsed.password or parsed.port:
        raise ValueError("登录服务返回了无法信任的授权地址。")
    return url


# ---------------------------------------------------------------------------
# 1. Raccoon 协议级扫码
# ---------------------------------------------------------------------------

RACCOON_AUTH_PREFIX = "/passport/api/v1"
RACCOON_QR_STATUS = {"canceled": "canceled", "logging": "logging", "success": "success"}


def _decode_jwt_exp_ms(token: str) -> int | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = int(claims.get("exp", 0))
        return exp * 1000 if exp > 0 else None
    except Exception:
        return None


def raccoon_qr_login(product_api_base: str = "https://api.raccoon.sensetime.com",
                     poll: Callable[[], bool] | None = None) -> Document:
    """Raccoon 扫码直登：出二维码 → 轮询 → 组装文档。"""
    http = requests.Session()
    code = secrets.token_hex(16)
    qr_url = f"{product_api_base}/login/mp?code={code}&appname={quote_plus('商汤小浣熊官网')}"

    print("请用「商汤小浣熊」App 扫描二维码登录：\n")
    _render_qr_in_terminal(qr_url)
    print()

    deadline = time.time() + LOGIN_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            resp = http.post(
                f"{product_api_base}{RACCOON_AUTH_PREFIX}/login_with_qrcode_code",
                json={"qrcode_code": code}, timeout=15)
            envelope = resp.json() if resp.ok else {}
        except (requests.RequestException, ValueError):
            envelope = {}
        data = envelope.get("data") if envelope.get("code") == 0 else None
        status = (data or {}).get("status", "")
        if status == "success" and (data or {}).get("access_token"):
            exp = _decode_jwt_exp_ms(data["access_token"])
            account = {"uid": str(data.get("uid") or data.get("user_id") or "raccoon-user"),
                       "nickname": data.get("nickname") or "Raccoon 账号"}
            auth = {"accessToken": data["access_token"],
                    "refreshToken": data.get("refresh_token", ""),
                    "domain": urlparse(product_api_base).hostname,
                    "expiresAt": exp}
            doc = parse_auth({"account": account, "auth": auth})
            doc.raw.setdefault("channel", "raccoon")
            print("✓ 扫码登录成功")
            return doc
        if status == "canceled":
            raise RuntimeError("扫码登录已被取消。")
        if poll and not poll():
            raise RuntimeError("扫码登录已中止。")
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError("扫码登录超时（二维码 5 分钟内有效，请重试）。")


# ---------------------------------------------------------------------------
# 2. WorkBuddy 网页授权（state 轮询）
# ---------------------------------------------------------------------------


def workbuddy_web_login(provider: str = "workbuddy",
                        open_browser: bool = True) -> Document:
    """WorkBuddy 官方网页授权：state → authUrl（二维码 + 浏览器）→ 轮询。"""
    global_login = provider == "workbuddy-global"
    origin = "https://www.workbuddy.ai" if global_login else "https://www.codebuddy.cn"
    platform = "workbuddy-ai" if global_login else "workbuddy"
    http = requests.Session()

    resp = http.post(f"{origin}/v2/plugin/auth/state?platform={platform}",
                     json={}, timeout=15,
                     headers={"Content-Type": "application/json", "X-Client-Platform": "web"})
    if not resp.ok:
        raise RuntimeError(f"网页登录服务暂不可用（HTTP {resp.status_code}）。")
    data = resp.json().get("data") or {}
    state = data.get("state")
    if not isinstance(state, str) or not state or len(state) > 500:
        raise RuntimeError("登录服务未返回有效授权请求。")
    url = _validate_login_url(
        data.get("authUrl") or data.get("auth_url") or data.get("url") or
        f"{origin}/login?state={quote_plus(state)}", provider)

    print("请完成官方网页登录（页面内可扫码 / 短信 / 密码，任选）：\n")
    _render_qr_in_terminal(url)
    print()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"  登录地址：{url}")
    print(f"  等待授权完成（{LOGIN_TIMEOUT_SEC // 60} 分钟内）…\n")

    deadline = time.time() + LOGIN_TIMEOUT_SEC
    token_payload = None
    while time.time() < deadline:
        try:
            poll_resp = http.get(
                f"{origin}/v2/plugin/auth/token?state={quote_plus(state)}",
                timeout=15, headers={"X-Client-Platform": "web"})
            payload = poll_resp.json() if poll_resp.ok else {}
        except (requests.RequestException, ValueError):
            payload = {}
        inner = payload.get("data") or {}
        token = inner.get("accessToken") or inner.get("access_token")
        if isinstance(token, str) and token:
            token_payload = inner
            break
        time.sleep(POLL_INTERVAL_SEC)
    if token_payload is None:
        raise TimeoutError("网页授权超时，请重试。")

    domain = token_payload.get("domain") or urlparse(origin).hostname
    allowed = GLOBAL_DOMAINS if global_login else \
        {"codebuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "www.workbuddy.cn"}
    if domain not in allowed:
        raise ValueError("登录凭据的服务域名不匹配，已停止保存。")

    profile = http.get(f"{origin}/v2/plugin/login/account?state={quote_plus(state)}",
                       headers={"Authorization": f"Bearer {token}",
                                "X-Domain": domain, "X-Client-Platform": "web"},
                       timeout=15)
    if not profile.ok or not (profile.json().get("data") or {}).get("uid"):
        raise RuntimeError("网页登录未返回有效账号信息。")
    account = profile.json()["data"]
    expires_at = token_payload.get("expiresAt") or (
        int(time.time() * 1000 + float(token_payload["expiresIn"]) * 1000)
        if token_payload.get("expiresIn") else None)
    doc = parse_auth({
        "account": account,
        "auth": {**token_payload, "accessToken": token, "domain": domain,
                 "expiresAt": expires_at},
        "allAccounts": [account],
    })
    doc.raw.setdefault("channel", "workbuddy-global" if global_login else "buddy")
    print(f"✓ 网页授权成功：{doc.label()}")
    return doc


# ---------------------------------------------------------------------------
# 3. 本机凭据读取（兜底）
# ---------------------------------------------------------------------------

LOCAL_SOURCES = {
    "buddy": "workbuddy-desktop.info",
    "workbuddy": "workbuddy-desktop-ai.info",
    "trae": "workbuddy-desktop.info",
    "qodercn": "workbuddy-desktop.info",
    "qoder": "workbuddy-desktop.info",
}


def local_login(channel_id: str) -> Document:
    """读本机桌面客户端登录文件（Windows / macOS / Linux 平台路径解析）。"""
    from pathlib import Path

    from .platform_paths import find_local_auth
    if channel_id not in LOCAL_SOURCES:
        raise RuntimeError(f"渠道 {channel_id} 暂无本机凭据来源。") from None
    path = find_local_auth(channel_id)
    doc = parse_auth(Path(path).read_text(encoding="utf-8"))
    doc.raw.setdefault("channel", channel_id)
    print(f"✓ 已读取本机凭据：{doc.label()}")
    return doc


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def login(channel_id: str, method: str | None = None, **kwargs) -> Document:
    """按渠道登录，method 缺省时按 qr > browser > local 顺序回退。"""
    channel = get_channel(channel_id)
    methods = [method] if method else methods_of(channel)
    errors: list[str] = []
    for m in methods:
        try:
            if m == "qr" and channel.login_kind == "raccoon-qr":
                return raccoon_qr_login()
            if m == "browser":
                if channel.login_kind == "workbuddy-state":
                    return workbuddy_web_login(
                        "workbuddy-global" if channel_id == "workbuddy" else "workbuddy",
                        open_browser=kwargs.get("open_browser", True))
                raise RuntimeError(f"渠道 {channel.id} 的浏览器登录需按厂商协议接入（loginUrl: {channel.origin}）。")
            if m == "local":
                return local_login(channel.id)
        except Exception as exc:  # 逐级回退
            errors.append(f"{m}: {exc}")
            print(f"· {m} 登录不可用 → 尝试下一方式（{exc}）")
    raise RuntimeError(f"渠道 {channel_id} 所有登录方式均失败：{'；'.join(errors)}")


# ---------------------------------------------------------------------------
# 4. MiniMax 设备码 + PKCE（协议提炼自 dsh-xdpool channel pack minimax-oauth.ts）
# ---------------------------------------------------------------------------

MINIMAX_ACCOUNT = "https://account.minimax.cn"
MINIMAX_CLIENT_ID = "mcode-public"
MINIMAX_SCOPE = "agent.default"
MINIMAX_AUDIENCE = "agent-backend"


def _pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge S256)。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    import hashlib
    verifier = "".join(secrets.choice(alphabet) for _ in range(64))
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def minimax_device_login(poll: Callable[[], bool] | None = None) -> Document:
    """MiniMax 设备码登录：产出 user_code + 验证链接，轮询换 token。"""
    import hashlib
    verifier, challenge = _pkce_pair()
    http = requests.Session()
    resp = http.post(f"{MINIMAX_ACCOUNT}/oauth2/device/code", data={
        "client_id": MINIMAX_CLIENT_ID, "scope": MINIMAX_SCOPE,
        "audience": MINIMAX_AUDIENCE, "code_challenge": challenge,
        "code_challenge_method": "S256"}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"MiniMax 设备码服务暂不可用（HTTP {resp.status_code}）。")
    data = resp.json()
    device_code, user_code = data.get("device_code"), data.get("user_code")
    verify_url = data.get("verification_uri_complete") or data.get("verification_uri")
    interval = int(data.get("interval") or 5)
    if not device_code or not user_code or not verify_url:
        raise RuntimeError("MiniMax 未返回有效设备码请求。")
    print(f"请打开 {verify_url} 并输入设备码 {user_code} 完成登录（网页里会展示成二维码/按钮）。")

    deadline = time.time() + LOGIN_TIMEOUT_SEC
    while time.time() < deadline:
        if poll and not poll():
            raise RuntimeError("登录已中止。")
        time.sleep(interval)
        tok = http.post(f"{MINIMAX_ACCOUNT}/oauth2/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code, "client_id": MINIMAX_CLIENT_ID,
            "code_verifier": verifier}, timeout=15).json()
        token = tok.get("access_token")
        if isinstance(token, str) and token:
            doc = parse_auth({
                "account": {"uid": tok.get("user_id") or user_code,
                            "nickname": f"MiniMax ({user_code})"},
                "auth": {"accessToken": token,
                         "refreshToken": tok.get("refresh_token", ""),
                         "domain": "account.minimax.cn",
                         "expiresAt": int(time.time() * 1000 + float(tok.get("expires_in", 3600)) * 1000)},
            })
            doc.raw.setdefault("channel", "minimax")
            print("✓ MiniMax 登录成功")
            return doc
        if tok.get("error") == "access_denied":
            raise RuntimeError("MiniMax 授权被拒绝。")
    raise TimeoutError("MiniMax 登录超时，请重试。")


# ---------------------------------------------------------------------------
# 5. Gemini 本地回调 OAuth（协议提炼自 gemini-oauth.ts）
# ---------------------------------------------------------------------------

GEMINI_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GEMINI_TOKEN = "https://oauth2.googleapis.com/token"
GEMINI_USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
GEMINI_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",
                 "https://www.googleapis.com/auth/userinfo.email",
                 "https://www.googleapis.com/auth/userinfo.profile")
# Gemini 使用 Google "installed app" 类型 OAuth 客户端。client_id/secret 属公开内置
# 常量（与 google-gemini/gemini-cli 一致），但为避免 GitHub push protection 对公开
# 常量的误报，本仓库不内置其值；按以下顺序解析：
#   1) 环境变量 GEMINI_CLIENT_ID / GEMINI_CLIENT_SECRET
#   2) 配置文件 ~/.wbspy/gemini-client.json  {"client_id": "...", "client_secret": "..."}
import json as _json
import os as _os
from pathlib import Path as _Path


def gemini_client() -> tuple[str, str]:
    """解析 Gemini OAuth 客户端公开常量，找不到时给出获取指引。"""
    cid, csec = _os.environ.get("GEMINI_CLIENT_ID"), _os.environ.get("GEMINI_CLIENT_SECRET")
    if cid and csec:
        return cid, csec
    cfg = _Path.home() / ".wbspy" / "gemini-client.json"
    if cfg.exists():
        data = _json.loads(cfg.read_text())
        cid, csec = data.get("client_id"), data.get("client_secret")
        if cid and csec:
            return cid, csec
    raise RuntimeError(
        "缺少 Gemini OAuth 客户端公开常量（可在 google-gemini/gemini-cli 开源仓库获取），"
        "然后：export GEMINI_CLIENT_ID=... GEMINI_CLIENT_SECRET=...，"
        "或写入 ~/.wbspy/gemini-client.json")


def gemini_oauth_login(port: int = 8199, poll: Callable[[], bool] | None = None) -> Document:
    """Gemini 登录：本机起回调服务器，打开 Google 授权页，等 code 换 token。"""
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    client_id, client_secret = gemini_client()
    state = secrets.token_hex(12)
    holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if "code=" in self.path:
                holder["code"] = self.path.split("code=")[1].split("&")[0]
                holder["state"] = self.path.split("state=")[1].split("&")[0] if "state=" in self.path else ""
                self.send_response(200)
                self.end_headers()
                self.wfile.write("✓ 授权成功，可关闭此页面".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):  # 静默
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    redirect_uri = f"http://localhost:{port}/oauth-callback"
    auth_url = (f"{GEMINI_AUTH}?client_id={client_id}&response_type=code"
                f"&redirect_uri={quote_plus(redirect_uri)}&scope={quote_plus(' '.join(GEMINI_SCOPES))}"
                f"&state={state}&access_type=offline&prompt=consent")
    print("请在浏览器完成 Google 授权（本机已启动回调监听）：\n")
    _render_qr_in_terminal(auth_url)
    print(f"\n  {auth_url}")

    verifier, challenge = _pkce_pair()
    deadline = time.time() + LOGIN_TIMEOUT_SEC
    while time.time() < deadline and "code" not in holder:
        if poll and not poll():
            server.shutdown()
            raise RuntimeError("登录已中止。")
        time.sleep(1)
    server.shutdown()
    code = holder.get("code")
    if not code:
        raise TimeoutError("Google 授权超时。")
    if holder.get("state") != state:
        raise ValueError("OAuth state 校验失败，已中止。")

    tok = requests.post(GEMINI_TOKEN, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        "code_verifier": verifier}, timeout=15).json()
    token = tok.get("access_token")
    if not token:
        raise RuntimeError(f"Google token 交换失败：{str(tok)[:160]}")
    email = ""
    try:
        email = requests.get(GEMINI_USERINFO, headers={"Authorization": f"Bearer {token}"},
                             timeout=10).json().get("email", "")
    except Exception:
        pass
    doc = parse_auth({
        "account": {"uid": email or "gemini-user", "nickname": f"Gemini ({email or 'google'})"},
        "auth": {"accessToken": token, "refreshToken": tok.get("refresh_token", ""),
                 "domain": "googleapis.com",
                 "expiresAt": int(time.time() * 1000 + float(tok.get("expires_in", 3600)) * 1000)},
    })
    doc.raw.setdefault("channel", "gemini")
    print(f"✓ Gemini 授权成功：{email}")
    return doc
