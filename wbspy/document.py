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

from .channels import CHANNELS


def _qoder_id(domain: str) -> str:
    return "qodercn" if ".cn" in domain else "qoder"


# domain 子串 → 渠道注册表 id（顺序敏感：具体域名先于宽泛子串）
_CHANNEL_DOMAIN_NEEDLES: tuple[tuple[str, Any], ...] = (
    ("codebuddy.cn", "buddy"),
    ("workbuddy.ai", "workbuddy"),
    ("trae", "trae"),
    ("qoder", _qoder_id),
    ("lobsterai", "lobsterai"),
    ("youdao.com", "lobsterai"),
    ("zcode", "zcode"),
    ("z.ai", "zcode"),
    ("cline", "cline"),
    ("huaweicloud", "codearts"),
    ("minimax", "minimax"),
    ("xfinfr", "loomy"),
    ("googleapis", "gemini"),
    ("raccoon", "raccoon"),
    ("sensetime", "raccoon"),
)

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


# 非 WorkBuddy 渠道账号的 API origin 白名单（仍为封闭集合，绝不外发到未知域）。
CHANNEL_API_ORIGINS = {
    "account.minimax.cn": "https://api.minimaxi.com",
    "googleapis.com": "https://oauth2.googleapis.com",
    "codearts.huaweicloud.com": "https://snap-access.cn-north-4.myhuaweicloud.com",
    "api.cline.bot": "https://api.cline.bot",
    "account.xfinfr.com": "https://loomyad.xunfei.cn/api/v1",
}


def _channel_origin(domain: str) -> str | None:
    for suffix, origin in CHANNEL_API_ORIGINS.items():
        if domain == suffix or domain.endswith("." + suffix):
            return origin
    return None


def api_origin(domain: str | None) -> str:
    """凭据所属账号服务的 API origin。无法识别的域名直接拒绝，绝不外发凭据。"""
    if domain in GLOBAL_DOMAINS:
        return GLOBAL_ORIGIN
    if domain and _channel_origin(domain):
        return _channel_origin(domain)
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
    def storage_scope(self) -> str:
        """存储与路由的命名空间：凭据实际服务的 API origin。

        不同渠道的 uid 命名空间互相独立（TRAE 的 "1" 和 Qoder 的 "1" 是两个
        人），因此凡是以「账号」为键的场景（磁盘文件名、账号池索引、冷却表）
        都必须用 scope+uid 复合键，绝不能裸用 uid。域名无法识别时退回原始
        domain，保证键仍然稳定。
        """
        try:
            return api_origin(self.domain)
        except ValueError:
            return self.domain or "unknown"

    @property
    def api(self) -> str:
        return api_origin(self.domain)

    @property
    def origin(self) -> str:
        return workbuddy_origin(self.domain)

    @property
    def is_workbuddy(self) -> bool:
        """是否 WorkBuddy 系凭据（国内/国际版）——只有它们能调 WorkBuddy 上游。"""
        return self.channel_id in ("buddy", "workbuddy")

    @property
    def channel_id(self) -> str | None:
        """凭据所属渠道的注册表 id（domain 子串识别）。"""
        d = (self.domain or "").lower()
        if not d:
            return "buddy"                       # 无域名的老快照视为 WorkBuddy 国内版
        for needle, cid in _CHANNEL_DOMAIN_NEEDLES:
            if needle in d:
                return cid(d) if callable(cid) else cid
        return None                              # 未知域名：能力视为空，展示用域名本身

    @property
    def capabilities(self) -> frozenset[str]:
        """运行期能力声明（chat/models/refresh/checkin/credits），来自渠道注册表。

        pool / 网关 / 自动化路由前必查——渠道不声明的能力绝不调用，
        从结构上杜绝「拿 TRAE 凭据打 WorkBuddy 接口」这类误用。
        """
        ch = CHANNELS.get(self.channel_id or "")
        return frozenset(ch.capabilities) if ch else frozenset()

    @property
    def channel_name(self) -> str:
        """凭据所属渠道的友好名（由 channel_id 查注册表，未知域名原样展示）。"""
        ch = CHANNELS.get(self.channel_id or "")
        if ch:
            return ch.name
        d = (self.domain or "").lower()
        return d or "WorkBuddy 账号"

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
