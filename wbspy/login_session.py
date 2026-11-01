"""异步登录会话（兼容 facade）。

实现已按渠道拆分至 :mod:`wbspy.login_flows` 包；本文件仅保留旧导入路径。
Gemini 本地回调 code 注入口（GEMINI_CALLBACK_CODES）随 gemini flow 移动，
此处 re-export 供 webui 继续使用。
"""

from .login_flows import LoginSessionManager, Ticket, doc_public, qr_svg
from .login_flows.gemini import GEMINI_CALLBACK_CODES
from .login_flows.minimax import parse_minimax_token

__all__ = ["LoginSessionManager", "Ticket", "doc_public", "qr_svg",
           "GEMINI_CALLBACK_CODES", "parse_minimax_token"]
