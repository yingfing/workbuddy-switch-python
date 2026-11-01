"""登录流程管理器：注册表分发的两段式状态机。

    POST /api/login/start  {channel, method?}  → manager.start()
    GET  /api/login/poll/{ticket}              → manager.poll()

每个渠道一个 flow 模块（本包内同名文件），暴露 ``start(channel, ticket)``
与 ``poll(ticket) -> Document | None``（None = 继续等待）。新增渠道只需
实现这两个函数并在 ``_FLOWS`` 注册。
"""

from __future__ import annotations

import threading
import time
import uuid

from ..channels import Channel, get_channel, methods_of
from ..login import LOGIN_TIMEOUT_SEC, local_login
from ..store import save_account
from . import cline, codearts, gemini, loomy, minimax, qoder, raccoon
from . import trae, workbuddy, lobsterai, zcode
from .core import Ticket, doc_public

# (method, channel.login_kind) → flow 模块
_FLOWS: dict[tuple[str, str], object] = {
    ("qr", "raccoon-qr"): raccoon,
    ("browser", "workbuddy-state"): workbuddy,
    ("qr", "minimax-device"): minimax,
    ("browser", "gemini-oauth"): gemini,
    ("browser", "codearts-ticket"): codearts,
    ("browser", "cline-device"): cline,
    ("qr", "loomy-wechat"): loomy,
    ("browser", "qoder-device"): qoder,
    ("qr", "qoder-device"): qoder,
    ("browser", "trae-callback"): trae,
    ("browser", "lobsterai-callback"): lobsterai,
    ("qr", "zcode-oauth"): zcode,
}


class LoginSessionManager:
    """进程内票据表 + 轮询状态机。线程安全（FastAPI 多 worker 也在单进程内）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    # -- 启动 -----------------------------------------------------------------

    def start(self, channel_id: str, method: str | None = None) -> dict:
        channel = get_channel(channel_id)
        chosen = method or methods_of(channel)[0]
        errors: list[str] = []
        for m in ([chosen] if method else methods_of(channel)):
            try:
                return self._start_one(channel, m)
            except Exception as exc:
                errors.append(f"{m}: {exc}")
        raise RuntimeError(f"渠道 {channel.name} 暂无可用登录方式（{'；'.join(errors)}）")

    def _start_one(self, channel: Channel, method: str) -> dict:
        ticket = Ticket(id=uuid.uuid4().hex[:16], channel_id=channel.id,
                        method=method, expires_at=time.time() + LOGIN_TIMEOUT_SEC)
        if method == "local":
            doc = local_login(channel.id)
            save_account(doc)
            ticket.kind = "done"
            ticket.document = doc
            ticket.message = f"已读取本机凭据并入库：{doc.label()}"
        else:
            flow = _FLOWS.get((method, channel.login_kind))
            if flow is None:
                # 无协议级实现：诚实降级，给出厂商登录页链接
                ticket.kind = "manual"
                ticket.auth_url = channel.origin
                ticket.message = (
                    f"{channel.name} 的登录协议尚未在本版实现。"
                    f"可打开厂商登录页（{channel.origin}）手动登录；"
                    "若本机装有该桌面客户端，改用 local 方式读取凭据。")
            else:
                flow.start(channel, ticket)
        with self._lock:
            self._tickets[ticket.id] = ticket
        return ticket.public()

    # -- 轮询 -----------------------------------------------------------------

    def poll(self, ticket_id: str) -> dict:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return {"status": "expired", "error": "会话不存在或已清理"}
        if ticket.document is not None:
            return {"status": "success", "document": doc_public(ticket.document)}
        if ticket.error:
            return {"status": "failed", "error": ticket.error}
        if time.time() > ticket.expires_at:
            self.drop(ticket_id)
            return {"status": "expired", "error": "登录会话已超时（5 分钟），请重新发起"}
        flow = _FLOWS.get((ticket.method, _login_kind(ticket.channel_id)))
        if flow is None:
            return {"status": "pending", "message": ticket.message}
        try:
            doc = flow.poll(ticket)
        except Exception as exc:
            ticket.error = str(exc)
            return {"status": "failed", "error": str(exc)}
        if doc is not None:
            ticket.document = doc
            save_account(doc)
            return {"status": "success", "document": doc_public(doc)}
        return {"status": "pending", "message": ticket.message}

    def drop(self, ticket_id: str) -> None:
        with self._lock:
            self._tickets.pop(ticket_id, None)

    # -- 回调投递（/authorize 端点 → 等待中的 flow）---------------------------

    def deliver_callback(self, params: dict) -> str:
        """按 state/loginTraceID 找到等待中的票据并投递回调参数。

        返回给浏览器的提示文案；找不到票据或已过期返回对应错误提示。
        """
        state = str(params.get("loginTraceID") or params.get("state") or "")
        if not state or len(state) > 200:
            return "回调参数无效，请返回应用重试。"
        with self._lock:
            ticket = next((t for t in self._tickets.values()
                           if t.state == state), None)
        if ticket is None:
            return "授权会话不存在或已完成，请关闭此页。"
        if ticket.document is not None:
            return "该授权已完成，请返回应用查看。"
        if time.time() > ticket.expires_at:
            return "授权会话已超时，请重新发起登录。"
        with self._lock:
            ticket.callback = dict(params)
        return "授权信息已接收，正在换取凭据；请返回应用查看结果。"

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            for tid, ticket in list(self._tickets.items()):
                if ticket.expires_at < now and ticket.document is None:
                    del self._tickets[tid]


def _login_kind(channel_id: str) -> str:
    try:
        return get_channel(channel_id).login_kind
    except Exception:
        return ""
