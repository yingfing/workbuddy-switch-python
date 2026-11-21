"""手机连接网关：复刻 WorkBuddy 桌面端 edge-sync 协议的最小子集。

让手机 App 的「连接电脑端」把 wbspy 当成一台桌面端直连使用。

协议四步（详见 docs/mobile-pairing-protocol.md，凭据来自桌面端 5.6.2 逆向）：

1. PUT  /console/as/conversation-sync/devices/remote-view   {deviceId, flag:1}
2. POST /edge-cloud/credentials                             → connectToken 等
3. WSS  wss://<host>/cf-connect/websocket?untrusted_uid=<uid>
        Centrifuge JSON 协议 connect(token) → subscribe user:<uid>:devices
4. 每 30s（服务端下发 heartbeatIntervalMs）publish device.online 心跳

手机下行指令经云端通道 push 到本端：
    {"type": "...", "args": [...], "requestId": "...",
     "replyChannel": "reply:<requestId>#<uid>"}
handler 结果直接 publish 到 replyChannel（该通道含 "#"，无需订阅 token）。

线程模型：单线程事件循环（recv 1s 超时驱动心跳计时），无共享竞态。
断线自动重连（指数退避），credentials 过期由重连时重取覆盖。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .cdp import MiniWs, WsError, WsTimeout

# ---------------------------------------------------------------------------
# 协议常量（逆向自 edge-sync/server/index.cjs · ChSdk 2.3.7）
# ---------------------------------------------------------------------------

API_PATH_CREDENTIALS = "/edge-cloud/credentials"
API_PATH_REMOTE_VIEW = "/console/as/conversation-sync/devices/remote-view"

CH_SDK_VERSION = "2.3.7"
APP_VERSION = "5.6.2"

EVENT_DEVICE_ONLINE = "device.online"
EVENT_DEVICE_OFFLINE = "device.offline"

DEFAULT_HEARTBEAT_S = 30.0
MIN_HEARTBEAT_S = 10.0                       # 服务端下发值兜底防抖
CONNECT_TIMEOUT_S = 10.0
PUMP_TICK_S = 1.0                            # recv 空转步长（驱动心跳/开关）
RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0


class MobileGatewayError(Exception):
    """手机连接网关协议层失败（credentials/WS/订阅）。"""


# ---------------------------------------------------------------------------
# HTTP 两步（第 1、2 步）
# ---------------------------------------------------------------------------

def auth_headers(doc) -> dict:
    """WorkBuddy API 鉴权头（对齐桌面端 axios 实例：Bearer + X-User-Id）。"""
    return {"Authorization": f"Bearer {doc.token}", "X-User-Id": doc.uid}


def fetch_credentials(doc, http, device_id: str, device_name: str,
                      app_version: str = APP_VERSION) -> dict:
    """第 2 步：POST /edge-cloud/credentials 换 channel 凭据。

    doc: Document（workbuddy 账号，须明文 token）
    http: requests.Session（由调用方复用连接池）
    """
    body = {
        "userId": doc.uid,
        "deviceId": device_id,
        "clientType": "desktop",
        "deviceName": device_name,
        "useDefaultScopes": True,
        "chSdkVersion": CH_SDK_VERSION,
        "appVersion": app_version,
        "capabilities": {"decompress": []},
    }
    resp = http.post(_origin(doc) + API_PATH_CREDENTIALS, json=body,
                     headers={**auth_headers(doc), "Content-Type": "application/json"},
                     timeout=15)
    if resp.status_code == 401:
        raise MobileGatewayError("credentials 401：账号 token 失效")
    if resp.status_code == 403:
        raise MobileGatewayError("credentials 403：拒绝（账号权限或风控）")
    if resp.status_code != 200:
        raise MobileGatewayError(f"credentials HTTP {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    missing = [k for k in ("connectToken", "cfWsUrl") if not inner.get(k)]
    if missing:
        raise MobileGatewayError(f"credentials 响应缺字段：{','.join(missing)}")
    return inner


def set_remote_view(doc, http, device_id: str, flag: int = 1) -> None:
    """第 1 步：注册/注销「允许移动端连接」（1=允许，2=拒绝）。"""
    resp = http.put(_origin(doc) + API_PATH_REMOTE_VIEW,
                    json={"deviceId": device_id, "flag": flag},
                    headers={**auth_headers(doc), "Content-Type": "application/json"},
                    timeout=15)
    if resp.status_code not in (200, 201, 204):
        raise MobileGatewayError(
            f"remote-view HTTP {resp.status_code}：{resp.text[:200]}")


def _origin(doc) -> str:
    """账号对应的 API origin（buddy 渠道按区域走 cn/global 域）。"""
    api = getattr(doc, "api", None)
    return api.rstrip("/") if api else "https://www.workbuddy.cn"


# ---------------------------------------------------------------------------
# Centrifuge JSON 协议最小客户端（第 3、4 步）
# ---------------------------------------------------------------------------

class CentrifugeWs:
    """单连接 Centrifuge JSON 客户端：connect / subscribe / publish / ping。

    push（服务端主动消息）由 request 调用顺带缓存，pump 主循环经
    next_message 统一消费——单线程所有权，无锁。
    """

    def __init__(self, url: str, timeout: float = CONNECT_TIMEOUT_S):
        p = urlparse(url)
        if p.scheme not in ("ws", "wss") or not p.hostname:
            raise MobileGatewayError(f"非法 cfWsUrl：{url}")
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        self._ws = MiniWs(p.hostname, p.port or (443 if p.scheme == "wss" else 80),
                          path, timeout, use_tls=(p.scheme == "wss"))
        self._id = 0
        self._pushes: list[dict] = []

    def close(self) -> None:
        self._ws.close()

    # -- 帧收发 ---------------------------------------------------------------

    @staticmethod
    def _parse_lines(text: str) -> list[dict]:
        """一帧可能含多行 JSON（Centrifuge 以 \n 分隔多消息）→ 逐行解析。"""
        out = []
        for line in text.split("\n"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def _request(self, method: str, params: dict | None = None,
                 timeout: float = 10.0) -> dict:
        """发请求帧 → 等同名响应；途中收到的 push 缓存、服务端 ping 自动回。

        Centrifuge 客户端命令帧形状：{"id": N, "<method>": <params>}
        （方法名是键，不是 {"method":..,"params":..}——与服务端回复帧同构，
        这也是 3501 bad request 的根源）。
        """
        self._id += 1
        mid = self._id
        self._ws.send_text(json.dumps({**({"id": mid}), method: params or {}}))
        deadline = time.time() + timeout
        while True:
            for obj in self._parse_lines(self._ws.recv_message(deadline)):
                if obj.get("id") == mid:
                    err = obj.get("error")
                    if err:
                        raise MobileGatewayError(
                            f"{method} 被拒绝：code={err.get('code')} {err.get('message', '')[:160]}")
                    return obj
                if "disconnect" in obj:
                    d = obj["disconnect"]
                    raise MobileGatewayError(
                        f"服务端断开：code={d.get('code')} reason={d.get('reason')}")
                if "push" in obj:
                    self._pushes.append(obj["push"])
                elif "id" not in obj:
                    self.pong()                   # 服务端心跳帧（空 {}）→ 回空 pong

    def connect(self, token: str, name: str = "js", version: str = "") -> dict:
        """第 3 步 a：连接鉴权。返回服务端 connect 信息（含 ping 间隔）。"""
        params = {"token": token}
        if name:
            params["name"] = name
        if version:
            params["version"] = version
        obj = self._request("connect", params)
        info = obj.get("connect") or {}
        if info.get("expires") and not info.get("refresh_required"):
            pass                                  # expires 由调用方凭续期兜底
        return info

    def subscribe(self, channel: str, token: str | None = None) -> dict:
        obj = self._request("subscribe", {k: v for k, v in {
            "channel": channel, "token": token or None, "recoverable": False,
        }.items() if v is not None})
        return obj.get("subscribe") or {}

    def publish(self, channel: str, data: Any) -> dict:
        obj = self._request("publish", {"channel": channel, "data": data})
        return obj.get("publish") or {}

    def pong(self) -> None:
        """回应服务端心跳：空命令帧 {}（对齐 js-centrifuge 的 _sendPong）。

        注意不能回 {"id":N,"ping":{}}——服务端会视作非法指令直接断连。
        """
        self._ws.send_text("{}")

    # -- 消息泵 ---------------------------------------------------------------

    def next_message(self, timeout: float) -> dict | None:
        """取一条待处理消息（push 优先，其次底层新帧）；超时返回 None。

        服务端 ping 在此自动应答；disconnect 抛 MobileGatewayError。
        """
        if self._pushes:
            return {"push": self._pushes.pop(0)}
        deadline = time.time() + timeout
        while True:
            for obj in self._parse_lines(self._ws.recv_message(deadline)):
                if "push" in obj:
                    return {"push": obj["push"]}
                if "disconnect" in obj:
                    d = obj["disconnect"]
                    raise MobileGatewayError(
                        f"服务端断开：code={d.get('code')} reason={d.get('reason')}")
                if "id" not in obj:
                    self.pong()                   # 服务端心跳帧（空 {}）→ 回空 pong
                    continue
                # 带 id 但没有对应请求在等（异常时序）→ 忽略，继续读
            if time.time() > deadline:
                return None


# ---------------------------------------------------------------------------
# 指令路由（可插拔 handler，模仿 edge-sync 的 ChannelCommandRouter）
# ---------------------------------------------------------------------------

class CommandRouter:
    """type → handler 注册表；未注册指令回 success=false，不炸连接。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict], Any]] = {}

    def on(self, cmd_type: str, handler: Callable[[dict], Any]) -> None:
        self._handlers[cmd_type] = handler

    def off(self, cmd_type: str) -> None:
        self._handlers.pop(cmd_type, None)

    def types(self) -> list[str]:
        return sorted(self._handlers)

    def dispatch(self, cmd: dict) -> dict:
        """执行 handler 并归一化回执形状：{success, ...}。"""
        handler = self._handlers.get(cmd.get("type") or "")
        if handler is None:
            return {"success": False,
                    "error": f"unsupported command: {cmd.get('type')}"}
        try:
            result = handler(cmd)
        except Exception as exc:                  # handler 挂了也要给手机一个明确回执
            return {"success": False, "error": str(exc)}
        if result is None:
            return {"success": True}
        if isinstance(result, dict):
            return {"success": True, **result}
        return {"success": True, "result": result}


def make_default_handlers(pool, log: Callable[[str], None] = print) -> dict:
    """内置最小指令面：能力探测 / 数据同步 / 会话消息（云端桥接）。

    handler 签名 fn(cmd) -> dict（含 success），由 CommandRouter 归一化。
    conversations:send 走 pool.chat_failover（账号池 OpenAI 兼容链路），
    消息不落云端会话——手机端以回执为准（第一版口径，handler 可插拔替换）。
    """
    def capabilities(cmd: dict) -> dict:
        return {"success": True, "capabilities": [],
                "supported": False}              # 无头服务：远程桌面画面不启用

    def models_list(cmd: dict) -> dict:
        try:
            rows = pool.models() or []
        except Exception as exc:
            log(f"[mobile] wb:models:list 失败：{exc}")
            rows = []
        return {"success": True, "models": rows}

    def connectors_list(cmd: dict) -> dict:
        return {"success": True, "connectors": []}

    def connectors_states(cmd: dict) -> dict:
        return {"success": True, "states": {}}

    def automations_list(cmd: dict) -> dict:
        return {"success": True, "automations": []}

    def conversations_send(cmd: dict) -> dict:
        args = (cmd.get("args") or [{}])[0] if cmd.get("args") else {}
        if isinstance(args, dict):
            text = str(args.get("text") or args.get("message")
                       or args.get("prompt") or "").strip()
        else:
            text = str(args or "").strip()
        if not text:
            return {"success": False, "error": "empty message"}
        model = None
        if isinstance(args, dict) and args.get("model"):
            model = str(args["model"])
        payload = {"messages": [{"role": "user", "content": text}],
                   "stream": False}
        if model:
            payload["model"] = model
        doc, resp = pool.chat_failover(payload, session=f"mobile:{cmd.get('requestId', '')}")
        body = resp.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = str(body)[:500]
        log(f"[mobile] 会话消息：「{doc.label()}」{len(content)} 字")
        return {"success": True, "content": content, "uid": doc.uid}

    return {
        "remoteDesktop.capabilities": capabilities,
        "wb:models:list": models_list,
        "wb:connectors:list": connectors_list,
        "wb:connectors:states": connectors_states,
        "wb:automations:list": automations_list,
        "wb:conversations:send": conversations_send,
    }


# ---------------------------------------------------------------------------
# 网关总控（线程生命周期 + 协议主循环）
# ---------------------------------------------------------------------------

class MobileGateway:
    """手机连接网关：后台线程跑协议四步 + 指令泵。

    pool: AccountPool（取 workbuddy 泳道账号发 credentials/心跳/指令回执）
    state_dir: 设备身份持久化目录（默认与账号库同根 data/）
    """

    def __init__(self, pool, log: Callable[[str], None] = print,
                 state_dir: Path | None = None):
        self.pool = pool
        self.log = log
        self._state_dir = Path(state_dir) if state_dir else None
        self._device: dict | None = None
        self._router = CommandRouter()
        for cmd_type, handler in make_default_handlers(pool, log).items():
            self._router.on(cmd_type, handler)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = {"running": False, "connected": False, "uid": "",
                        "device_id": "", "device_name": "", "last_error": "",
                        "connected_at": 0.0}
        self._reconnect_delay = RECONNECT_BASE_S

    # -- 设备身份 -------------------------------------------------------------

    def device(self) -> dict:
        """设备身份（uuid4 持久化：同一台 wbspy 对手机始终是同一台桌面端）。"""
        with self._lock:
            if self._device:
                return self._device
            state_file = (self._state_dir / "mobile-device.json") if self._state_dir else None
            if state_file and state_file.exists():
                try:
                    self._device = json.loads(state_file.read_text(encoding="utf-8"))
                    return self._device
                except (json.JSONDecodeError, OSError):
                    pass
            device = {"device_id": str(uuid_mod.uuid4()),
                      "device_name": socket.gethostname() or "wbspy"}
            if state_file:
                try:
                    state_file.parent.mkdir(parents=True, exist_ok=True)
                    state_file.write_text(json.dumps(device, ensure_ascii=False),
                                          encoding="utf-8")
                except OSError:
                    pass
            self._device = device
            return device

    # -- 生命周期 -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
        dev = self.device()                          # 锁外：device() 内部另取锁
        with self._lock:
            self._status.update(running=True, device_id=dev["device_id"],
                                device_name=dev["device_name"], last_error="")
            self._thread = threading.Thread(target=self._run_loop,
                                            name="mobile-gateway", daemon=True)
            thread = self._thread
        thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            self._status.update(running=False, connected=False, connected_at=0.0)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def router(self) -> CommandRouter:
        return self._router

    # -- 协议主循环 -----------------------------------------------------------

    def _pick_doc(self):
        """选一个 workbuddy 泳道明文 token 账号；没有则返回 None。"""
        docs = self.pool.healthy(lane="workbuddy")
        for doc in docs:
            if doc.token and not doc.protected_token:
                return doc
        return None

    def _run_loop(self) -> None:
        """外层重连循环：指数退避，stop 立即退出。"""
        while not self._stop.is_set():
            doc = self._pick_doc()
            if doc is None:
                self._set_status(last_error="无可用 workbuddy 账号（需明文 token）")
                if self._stop.wait(timeout=30.0):
                    return
                continue
            try:
                self._session(doc)
                self._reconnect_delay = RECONNECT_BASE_S
            except (MobileGatewayError, WsError, OSError, ValueError) as exc:
                self._set_status(connected=False, last_error=str(exc)[:300])
                self.log(f"[mobile] 连接中断：{exc}")
            except Exception as exc:              # 框架级意外：保命，退避重试
                self._set_status(connected=False, last_error=str(exc)[:300])
                self.log(f"[mobile] 意外错误：{exc!r}")
            if self._stop.is_set():
                return
            delay = min(self._reconnect_delay, RECONNECT_MAX_S)
            self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_S)
            self._stop.wait(timeout=delay)

    def _set_status(self, **kwargs) -> None:
        with self._lock:
            self._status.update(kwargs)

    def _session(self, doc) -> None:
        """一次完整的「注册 → 凭据 → WS → 心跳泵」会话。"""
        import requests

        dev = self.device()
        devices_channel = f"user:{doc.uid}:devices"
        http = requests.Session()
        http.headers.update({"User-Agent": "wbspy"})

        set_remote_view(doc, http, dev["device_id"], flag=1)
        cred = fetch_credentials(doc, http, dev["device_id"], dev["device_name"])
        heartbeat_s = max(float(cred.get("heartbeatIntervalMs") or 0) / 1000.0,
                          MIN_HEARTBEAT_S) if cred.get("heartbeatIntervalMs") \
            else DEFAULT_HEARTBEAT_S

        ws = CentrifugeWs(cred["cfWsUrl"])
        try:
            ws.connect(cred["connectToken"])
            sub_token = (cred.get("subscriptionTokens") or {}).get(devices_channel)
            if sub_token is None:
                wildcard = devices_channel.rsplit(":", 1)[0] + ":*"
                sub_token = (cred.get("subscriptionTokens") or {}).get(wildcard)
            ws.subscribe(devices_channel, token=sub_token or "")
            ws.publish(devices_channel, self._heartbeat_payload(dev))
            self._set_status(connected=True, uid=doc.uid,
                             connected_at=time.time(), last_error="")
            self.log(f"[mobile] 已上线：device={dev['device_name']} "
                     f"uid={doc.uid[:8]}… 心跳 {heartbeat_s:.0f}s")
            self._pump(ws, doc, dev, devices_channel, heartbeat_s)
        finally:
            ws.close()
            self._set_status(connected=False)

    @staticmethod
    def _heartbeat_payload(dev: dict, event: str = EVENT_DEVICE_ONLINE) -> dict:
        return {"type": event, "deviceId": dev["device_id"],
                "clientType": "desktop", "ts": int(time.time() * 1000)}

    def _pump(self, ws: CentrifugeWs, doc, dev: dict,
              devices_channel: str, heartbeat_s: float) -> None:
        """单线程事件循环：心跳计时 + 指令分发 + 回执，stop/断线即退。"""
        next_beat = time.time() + heartbeat_s
        while not self._stop.is_set():
            remaining = next_beat - time.time()
            if remaining <= 0:
                ws.publish(devices_channel, self._heartbeat_payload(dev))
                next_beat = time.time() + heartbeat_s
                remaining = heartbeat_s
            try:
                msg = ws.next_message(timeout=min(remaining, PUMP_TICK_S))
            except WsTimeout:
                continue                          # 空转：回循环查心跳/停止
            if msg is None:
                continue
            push = msg.get("push") or {}
            channel = push.get("channel") or ""
            pub = push.get("pub") or {}
            data = pub.get("data")
            if not isinstance(data, dict):
                continue
            if channel == devices_channel and data.get("type") in (
                    EVENT_DEVICE_ONLINE, EVENT_DEVICE_OFFLINE):
                continue                          # 其他设备的心跳回响，不处理
            self._handle_command(ws, doc, data)

    def _handle_command(self, ws: CentrifugeWs, doc, cmd: dict) -> None:
        cmd_type = str(cmd.get("type") or "")
        result = self._router.dispatch(cmd)
        reply_channel = str(cmd.get("replyChannel") or "")
        if not reply_channel:
            self.log(f"[mobile] 指令 {cmd_type or '?'}（无 replyChannel）→ 忽略回执")
            return
        try:
            ws.publish(reply_channel, result)
            state = "ok" if result.get("success") else "err"
            self.log(f"[mobile] 指令 {cmd_type} → {state}")
        except MobileGatewayError as exc:
            raise                                 # 发布失败视为连接级故障 → 重连
        except (WsError, OSError) as exc:
            raise MobileGatewayError(f"回执发布失败：{exc}") from exc
