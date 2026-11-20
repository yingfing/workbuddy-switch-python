"""最小 WebSocket + Chrome DevTools Protocol 客户端（零第三方依赖）。

为码道 CodeArts 网页签到而写：驱动系统自带 Chrome/Chromium/Edge 的
headless 模式打开码道页面，在页面内调活动福利接口。不引入 Playwright，
不下载浏览器二进制——只探系统已有的浏览器可执行文件。

WebSocket 仅实现本场景需要的子集（RFC 6455）：
- 文本帧收发（client 强制掩码）
- 分片消息重组（CDP 响应常达数十 KB，必然分片）
- ping 自动回 pong、close 视为断开
"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import struct
import time
from urllib.parse import urlparse


class WsError(Exception):
    """WebSocket 层失败（握手/中断/超时）。"""


class MiniWs:
    """单连接文本帧 WebSocket 客户端（client → server 方向掩码）。"""

    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("握手失败：连接被对端关闭")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise WsError(f"握手失败：{status[:120]!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("连接中断")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_message(self, deadline: float | None = None) -> str:
        """读一条完整消息；自动回 ping，遇 close 抛 WsError。"""
        if deadline is None:
            deadline = time.time() + self.timeout
        frags: list[bytes] = []
        while True:
            if time.time() > deadline:
                raise WsError("读消息超时")
            b1, b2 = self._read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:                       # ping → pong
                self._send_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise WsError("服务端关闭了 WebSocket 连接")
            if opcode not in (0x1, 0x2, 0x0):
                continue                            # 忽略其他控制帧
            frags.append(data)
            if b1 & 0x80:                           # FIN：消息收全
                return b"".join(frags).decode(errors="replace")


class CdpError(Exception):
    """CDP 调用失败（协议错误 / evaluate 异常 / 超时）。"""


class CdpClient:
    """极简 CDP：请求-响应轮询，事件消息直接丢弃。"""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        p = urlparse(ws_url)
        if not p.hostname or not p.port:
            raise CdpError(f"CDP 地址不完整：{ws_url}")
        self.ws = MiniWs(p.hostname, p.port, p.path or "/", timeout)
        self._id = 0

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise CdpError(f"CDP 调用超时：{method}")
            try:
                msg = json.loads(self.ws.recv_message(deadline=deadline))
            except (WsError, ValueError) as exc:
                raise CdpError(f"CDP {method}：{exc}") from exc
            if msg.get("id") != mid:
                continue                            # 事件/其他请求的响应，忽略
            if "error" in msg:
                raise CdpError(f"CDP {method}：{msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}

    def evaluate(self, expression: str, timeout: float = 60.0) -> object:
        """执行 JS 并返回反序列化后的值。"""
        result = self.call("Runtime.evaluate",
                           {"expression": expression, "awaitPromise": True,
                            "returnByValue": True}, timeout=timeout)
        details = result.get("exceptionDetails")
        if details:
            text = (details.get("exception") or {}).get("description") or str(details)[:300]
            raise CdpError(f"页面脚本异常：{text[:300]}")
        return (result.get("result") or {}).get("value")
