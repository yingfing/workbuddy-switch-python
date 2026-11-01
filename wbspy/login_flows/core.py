"""登录流程公共件：会话票据、二维码渲染、脱敏视图。"""

from __future__ import annotations

from dataclasses import dataclass

from ..document import Document


# Gemini 本地回调收到的 code：state → code（由 webui 的回调端点写入）
GEMINI_CALLBACK_CODES: dict[str, str] = {}


def qr_svg(content: str) -> str:
    """内容 → SVG 二维码字符串（qrcode 库缺失时返回空串）。"""
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return ""
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(content, image_factory=factory, border=1)
    return img.to_string().decode()


@dataclass
class Ticket:
    """一次登录会话的状态机票据（Web 两段式 start/poll 共用）。"""

    id: str
    channel_id: str
    method: str
    kind: str = ""                  # qr / browser / device / manual / done
    qr_content: str = ""
    qr_svg: str = ""
    auth_url: str = ""
    message: str = ""
    # 会话状态（各流程自行约定语义）
    state: str = ""
    code: str = ""
    origin: str = ""
    platform: str = ""
    expires_at: float = 0.0
    document: Document | None = None
    error: str = ""

    def public(self) -> dict:
        return {
            "ticket": self.id, "channel": self.channel_id, "method": self.method,
            "kind": self.kind, "qr_svg": self.qr_svg, "auth_url": self.auth_url,
            "message": self.message, "error": self.error,
        }


def doc_public(doc: Document) -> dict:
    """凭据脱敏视图（回传前端展示用）。"""
    return {"uid": doc.uid, "label": doc.label(), "region": doc.region,
            "api_ready": doc.api_ready, "protected": doc.protected_token}
