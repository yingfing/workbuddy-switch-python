"""登录凭据文档解析与区域判定。

WorkBuddy 桌面客户端把登录信息写在本地 JSON 文件：

    国内版:  ~/.workbuddy/workbuddy-desktop.info
    国际版:  ~/.workbuddy-ai/workbuddy-desktop-ai.info

结构::

    {
      "account": { "uid": "...", "nickname": "...", "phoneNumber": "..." },
      "auth":    { "accessToken": "...", "refreshToken": "...",
                   "domain": "www.codebuddy.cn", "expiresAt": 1730000000000 },
      ...其余为机器本地字段，切换时原样保留...
    }

token 可能是 ``{"$wbEncrypted": 1, "envelope": "..."}``（客户端 5.6+ 的本机
加密凭据）：这种凭据只能用于切换，API 能力需要官方网页登录授权的明文 token。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 区域 / 端点判定（对应原版 quota.mjs 的 apiOrigin / workBuddyOrigin）
# ---------------------------------------------------------------------------

GLOBAL_ORIGIN = "https://www.workbuddy.ai"
CN_PRODUCT_ORIGIN = "https://www.workbuddy.cn"
CN_LEGACY_DOMAINS = {
    "workbuddy.cn", "www.workbuddy.cn",
    "codebuddy.cn", "www.codebuddy.cn",
    "copilot.tencent.com",
}

GLOBAL_DOMAINS = {"workbuddy.ai", "www.workbuddy.ai"}


def api_origin(domain: str | None) -> str:
    """凭据所属账号服务的 API origin。无法识别的域名直接拒绝，绝不外发凭据。"""
    if domain in GLOBAL_DOMAINS:
        return GLOBAL_ORIGIN
    if domain and domain not in CN_LEGACY_DOMAINS:
        raise ValueError("账号服务地区或域名无法识别，已停止发送凭据。")
    if domain in ("workbuddy.cn", "www.workbuddy.cn"):
        return CN_PRODUCT_ORIGIN
    return "https://www.codebuddy.cn"


def workbuddy_origin(domain: str | None) -> str:
    """产品功能（对话、积分活动）统一走当前产品域；国际版维持自身。"""
    origin = api_origin(domain)
    return CN_PRODUCT_ORIGIN if origin != GLOBAL_ORIGIN else origin


def region_of(domain: str | None) -> str:
    return "global" if domain in GLOBAL_DOMAINS else "cn"


# ---------------------------------------------------------------------------
# 文档解析（对应原版 auth.mjs 的 parseAuth）
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _token_expiry(auth: dict[str, Any]) -> int | None:
    """expiresAt / expireTime / accessTokenExpiresAt，秒则换毫秒。"""
    raw = auth.get("expiresAt") or auth.get("expireTime") or auth.get("accessTokenExpiresAt")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value * 1000) if value < 1e11 else int(value)


@dataclass
class Document:
    """一个登录凭据文档（已解析）。"""

    raw: dict[str, Any]                  # 登录文件完整 JSON（切换时保留机器本地字段）
    uid: str                             # 计费身份
    token: str = ""                      # 明文 accessToken；空 = 仅本机加密凭据
    protected_token: bool = False        # 是否 $wbEncrypted 包装
    refresh_token: str = ""
    nickname: str = ""
    account_type: str = ""
    phone_hint: str = ""
    token_expires_at: int | None = None
    region: str = "cn"
    domain: str = ""

    @property
    def api(self) -> str:
        return api_origin(self.domain)

    @property
    def origin(self) -> str:
        return workbuddy_origin(self.domain)

    @property
    def token_expired(self) -> bool:
        return bool(self.token_expires_at and self.token_expires_at <= time.time() * 1000)

    @property
    def api_ready(self) -> bool:
        """能否直接调 API（有明文 token 且未过期）。"""
        return bool(self.token) and not self.token_expired

    def to_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False)

    def label(self) -> str:
        base = self.nickname or "WorkBuddy 账号"
        suffix = " [加密凭据,仅切换]" if self.protected_token else ""
        region = "国际版" if self.region == "global" else "国内版"
        return f"{base}（{region}, {self.uid[:12]}…）{suffix}"


def parse_auth(raw: str | bytes | dict) -> Document:
    """解析登录文件 JSON → Document。缺少账号或凭据即抛错。"""
    try:
        root = raw if isinstance(raw, dict) else json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("WorkBuddy 登录信息不是有效的 JSON。") from exc
    if not isinstance(root, dict):
        raise ValueError("WorkBuddy 登录信息不是有效的 JSON 对象。")

    account = root.get("account") or {}
    auth = root.get("auth") or {}
    uid = _text(account.get("uid"))
    wrapper = auth.get("accessToken")
    token = _text(wrapper)
    protected = (
        isinstance(wrapper, dict)
        and wrapper.get("$wbEncrypted") == 1
        and bool(wrapper.get("envelope"))
    )
    if not uid or (not token and not protected) or len(uid) > 200:
        raise ValueError("登录信息缺少账号或访问凭据，请先在 WorkBuddy 中登录。")

    phone = _text(account.get("phoneNumber"))
    domain = _text(auth.get("domain") or (account.get("domain")) or root.get("domain"))
    refresh = _text(auth.get("refreshToken") or auth.get("refresh_token"))
    return Document(
        raw=root,
        uid=uid,
        token=token,
        protected_token=protected,
        refresh_token=refresh,
        nickname=_text(account.get("nickname")) or "WorkBuddy 账号",
        account_type=_text(account.get("accountType") or account.get("type")),
        phone_hint=f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "",
        token_expires_at=_token_expiry(auth),
        region=region_of(domain or None),
        domain=domain,
    )


def load_live_document(region: str = "cn", home: Path | None = None) -> Document:
    """读取桌面客户端当前登录（只读，不写客户端文件）。"""
    home = home or Path.home()
    if region == "global":
        path = home / ".workbuddy-ai" / "workbuddy-desktop-ai.info"
    else:
        path = home / ".workbuddy" / "workbuddy-desktop.info"
    if not path.exists():
        raise FileNotFoundError(f"未找到桌面客户端登录文件：{path}")
    return parse_auth(path.read_text(encoding="utf-8"))
