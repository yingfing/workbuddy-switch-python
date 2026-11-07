"""Web 交互界面：与网关同进程，一个端口跑全部。

    python -m wbspy web --port 8178
      → http://127.0.0.1:8178/          单页界面（Raycast Dark 风格）
      → http://127.0.0.1:8178/v1/...    OpenAI 兼容网关（复用 gateway.py）

管理 API（/api/*）：

    GET    /api/channels                渠道列表与登录方式
    POST   /api/login/start             发起登录（返回二维码/授权页）
    GET    /api/login/poll/{ticket}     轮询登录结果
    GET    /api/accounts                账号库
    DELETE /api/accounts/{uid}          移除账号
    POST   /api/automation/run          后台跑一轮签到/旅行/任务
    GET    /api/automation/status       自动化日志与运行状态
    POST   /api/automation/toggle       自动签到开关（持久化）
    POST   /api/automation/config       托管节奏配置（保存即自动生效）
    POST   /api/system/restart          重启服务进程
    GET    /api/snapshots               快照列表
    POST   /api/switch/save             保存当前桌面登录
    POST   /api/switch/switch           切换到快照
    POST   /api/switch/restore          恢复切换前登录
    GET    /api/pool                    网关账号池实时状态
    GET    /api/models                  聚合模型目录
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .automation import run_all, poll_worker
from .channels import CHANNELS, get_channel, methods_of
from .document import parse_auth
from .gateway import create_app
from .login_session import GEMINI_CALLBACK_CODES, LoginSessionManager
from .pool import AccountPool
from .usage import LEDGER
from .store import (load_accounts, remove_account, list_snapshots,
                    load_config, save_config)
from .switch import restore_previous, save_current_account, switch_account

STATIC_DIR = Path(__file__).parent / "static"


def _ok(data=None, **extra) -> dict:
    return {"ok": True, "data": data, **extra}


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": str(message)})


def rebuild_restart_argv(argv: list[str], current_port: int | None,
                         config: dict) -> list[str]:
    """重建重启命令行参数：把 --port 替换为 config web.port（缺省=当前端口）。

    网页改端口后点重启即生效——即使 systemd unit 里写死了 --port，
    重启时会用 config 里最近保存的值覆盖它（网页是最近一次的用户意图）。
    其余参数（--host、--allow-insecure 等）原样保留。
    """
    try:
        new_port = int((config.get("web") or {}).get("port") or current_port or 0)
    except (TypeError, ValueError):
        new_port = current_port or 0
    out: list[str] = []
    skip = False
    for a in argv:
        if skip:                                 # 旧 "--port <值>" 的值
            skip = False
            continue
        if a == "--port":
            skip = True
            continue
        if a.startswith("--port="):
            continue
        out.append(a)
    if new_port:
        out += ["--port", str(new_port)]
    return out


def port_bind_error(port: int) -> str | None:
    """试绑 0.0.0.0:port，可绑返回 None，否则返回人话错误信息。"""
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
        return None
    except PermissionError:
        return f"端口 {port} 是特权端口（<1024），需要管理员权限"
    except OSError:
        return f"端口 {port} 已被其他程序占用"


def create_web_app(pool: AccountPool, api_key: str = "", port: int | None = None) -> FastAPI:
    app = create_app(pool, api_key=api_key)     # 网关路由（/v1/*、/healthz）
    app.state.web_port = port                   # 当前监听端口（重启 argv 重建用）
    sessions = LoginSessionManager()
    try:
        pool.start_auto_refresh()               # token 临期自动续期（后台线程）
    except Exception:
        pass

    # -- Gemini 本地回调（localhost:8199 → 注入 code）--------------------------

    _gemini_listener = {"started": False}

    def _ensure_gemini_listener():
        if _gemini_listener["started"]:
            return
        _gemini_listener["started"] = True

        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class CB(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if "code=" in self.path and "state=" in self.path:
                    code = self.path.split("code=")[1].split("&")[0]
                    state = self.path.split("state=")[1].split("&")[0]
                    GEMINI_CALLBACK_CODES[state] = code
                    self.send_response(200)
                else:
                    self.send_response(404)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        try:
            srv = HTTPServer(("127.0.0.1", 8199), CB)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError:
            pass                                    # 端口占用（已有监听）

    # -- 页面 ---------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/update/check")
    def api_update_check():
        """检查新版本：只返回对比结果与发布页链接，绝不自动升级。"""
        from .update import check_update
        return _ok(check_update())

    if (STATIC_DIR / "index.html").exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- 登录 ---------------------------------------------------------------

    @app.get("/api/channels")
    def api_channels():
        return _ok([{
            "id": ch.id, "name": ch.name, "org": ch.org, "note": ch.note,
            "methods": methods_of(ch), "origin": ch.origin,
            "checkin": ch.credits_claim,
        } for ch in CHANNELS.values()])

    @app.post("/api/login/start")
    async def login_start(request: Request):
        body = await request.json()
        try:
            result = sessions.start(body.get("channel", ""), body.get("method"))
            if result.get("kind") == "browser" and "accounts.google.com" in (result.get("auth_url") or ""):
                _ensure_gemini_listener()
            return _ok(result)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _err(exc)

    @app.get("/api/login/poll/{ticket}")
    def login_poll(ticket: str):
        result = sessions.poll(ticket)
        if result.get("status") == "success":
            pool.refresh_documents(load_accounts())   # 新账号即时进池
        return _ok(result)

    @app.get("/authorize")
    def authorize_callback(request: Request):
        """TRAE / LobsterAI 授权回调：把查询参数投递给等待中的登录票据。"""
        from fastapi.responses import HTMLResponse
        message = sessions.deliver_callback(dict(request.query_params))
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><title>wbspy 授权</title>'
            '<body style="font:16px system-ui;padding:48px;text-align:center">'
            f'<p>{message}</p></body>')

    # -- 账号 ---------------------------------------------------------------

    @app.get("/api/accounts")
    def api_accounts():
        return _ok([{
            "uid": d.uid, "label": d.label(), "region": d.region,
            "api_ready": d.api_ready, "protected": d.protected_token,
            "expires_at": d.token_expires_at, "domain": d.domain,
            "channel": d.channel_name,
        } for d in load_accounts()])

    @app.delete("/api/accounts/{uid}")
    def api_account_remove(uid: str, domain: str = ""):
        if not remove_account(uid, domain=domain):
            raise HTTPException(404, "账号不存在")
        pool.refresh_documents(load_accounts())
        return _ok()

    # -- 自动化 ---------------------------------------------------------------

    app.state.auto_running = False
    # auto_log / auto_total 已在 gateway.create_app 初始化（网关日志与自动化共用）
    app.state.auto_thread = None
    app.state.auto_stop = threading.Event()

    def _log(line: str) -> None:
        app.state.auto_log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        del app.state.auto_log[:-200]
        app.state.auto_total += 1

    pool.event_log = _log    # 调度事件（换号/凭据失效/token 续期）进网页日志

    def _start_poll() -> None:
        """按 config.automation.enabled 启动自动签到轮询线程。"""
        if app.state.auto_thread and app.state.auto_thread.is_alive():
            return
        app.state.auto_stop = threading.Event()
        app.state.auto_thread = threading.Thread(
            target=poll_worker, args=(_log, app.state.auto_stop), daemon=True)
        app.state.auto_thread.start()

    if (load_config().get("automation") or {}).get("enabled"):
        _start_poll()                                    # 服务重启后恢复之前的开关状态

    @app.post("/api/automation/run")
    def automation_run():
        if app.state.auto_running:
            return _err("上一轮自动化仍在运行")
        accounts = load_accounts()
        if not accounts:
            return _err("账号库为空，请先登录", 400)
        app.state.auto_running = True
        app.state.auto_log = []

        def worker():
            try:
                run_all(accounts, _log)
            except Exception as exc:
                _log(f"异常：{exc}")
            finally:
                app.state.auto_running = False

        threading.Thread(target=worker, daemon=True).start()
        return _ok({"started": True})

    def _restart_poll() -> None:
        """重启托管线程：旧线程 set 唤醒退出，起新线程立即按新配置跑。"""
        app.state.auto_stop.set()
        old = app.state.auto_thread
        if old and old.is_alive():
            old.join(timeout=3)
        app.state.auto_thread = None
        _start_poll()

    @app.post("/api/automation/toggle")
    async def automation_toggle(request: Request):
        """网页自动签到开关（状态写 config，服务重启自动恢复）。"""
        body = await request.json()
        enabled = bool(body.get("enabled"))
        cfg = load_config()
        cfg.setdefault("automation", {})["enabled"] = enabled
        save_config(cfg)
        auto = cfg.get("automation") or {}
        if enabled:
            _start_poll()
            _log("自动签到托管：已开启（" + _mode_desc(auto) + "）")
        else:
            app.state.auto_stop.set()
            app.state.auto_thread = None
            _log("自动签到托管：已关闭")
        return _ok({"enabled": enabled})

    @app.post("/api/automation/config")
    async def automation_config(request: Request):
        """保存托管节奏并自动重启线程——保存即生效，无需手动重启服务。"""
        import re
        body = await request.json()
        mode = body.get("mode")
        if mode not in ("interval", "daily"):
            raise HTTPException(400, "mode 仅支持 interval（固定间隔）/ daily（每天定点）")
        cfg = load_config()
        auto = cfg.setdefault("automation", {})
        auto["mode"] = mode
        if mode == "interval":
            try:
                minutes = int(body.get("interval_minutes"))
            except (TypeError, ValueError):
                raise HTTPException(400, "interval_minutes 需为整数分钟")
            if not 5 <= minutes <= 1440:
                raise HTTPException(400, "间隔需在 5 ~ 1440 分钟之间")
            auto["interval_minutes"] = minutes
        else:
            daily_at = str(body.get("daily_at") or "")
            if not re.fullmatch(r"\d{1,2}:\d{1,2}", daily_at):
                raise HTTPException(400, "时间格式应为 HH:MM")
            h, m = (int(x) for x in daily_at.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise HTTPException(400, "时间超出 0:00-23:59 范围")
            auto["daily_at"] = f"{h:02d}:{m:02d}"
        if "notify_url" in body:                     # webhook 推送地址一并保存（空=清除）
            nu = str(body.get("notify_url") or "").strip()
            if nu:
                auto["notify_url"] = nu
            else:
                auto.pop("notify_url", None)
        save_config(cfg)
        if auto.get("enabled"):
            _restart_poll()
            _log(f"托管节奏已保存并生效：{_mode_desc(auto)}")
        return _ok({"saved": True, "mode": auto["mode"],
                    "interval_minutes": auto.get("interval_minutes"),
                    "daily_at": auto.get("daily_at"),
                    "notify_url": auto.get("notify_url") or ""})

    def _mode_desc(auto: dict) -> str:
        if (auto.get("mode") or "interval") == "daily":
            return f"每天 {str(auto.get('daily_at') or '09:00')} 定点托管"
        try:
            n = int(auto.get("interval_minutes") or 120)
        except (TypeError, ValueError):
            n = 120
        return f"每 {n} 分钟轮询签到" + ("（每天首轮全量托管）" if n else "")

    @app.post("/api/system/restart")
    def system_restart():
        """重启服务进程：os.execv 自替换；端口取 config web.port（网页改端口重启即生效）。"""
        import os
        import sys

        def _do():
            time.sleep(1.5)                            # 先把响应发回前端
            try:
                rest = rebuild_restart_argv(
                    sys.argv[1:], getattr(app.state, "web_port", None), load_config())
                if getattr(sys, "frozen", False):      # PyInstaller exe
                    os.execv(sys.executable, [sys.executable] + rest)
                os.execv(sys.executable, [sys.executable, "-m", "wbspy"] + rest)
            except Exception as exc:                   # pragma: no cover
                _log(f"服务重启失败：{exc}")

        threading.Thread(target=_do, daemon=True).start()
        return _ok({"restarting": True})

    @app.get("/api/logs")
    def api_logs():
        """全局运行日志（悬浮面板用）：最近日志行 + 累计行数（未读徽标依据）。"""
        return _ok({"log": app.state.auto_log[-200:], "total": app.state.auto_total})

    @app.get("/api/automation/status")
    def automation_status():
        auto = load_config().get("automation") or {}
        enabled = bool(auto.get("enabled"))
        history: list = []
        try:
            from .automation import HISTORY_PATH
            import json as _json
            if HISTORY_PATH.exists():
                raw = _json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                for entry in raw[:10]:
                    ok = sum(1 for r in entry.get("results", [])
                             if r.get("summary") and not r["summary"].get("skip"))
                    history.append({
                        "at": entry.get("at"),
                        "accounts": len(entry.get("results", [])),
                        "ok": ok,
                        "results": [{"label": r.get("label"), "summary": r.get("summary")}
                                    for r in entry.get("results", [])],
                    })
        except Exception:
            history = []
        return _ok({"running": app.state.auto_running,
                    "log": app.state.auto_log[-60:], "history": history,
                    "enabled": enabled,
                    "mode": (auto.get("mode") or "interval"),
                    "interval_minutes": (auto.get("interval_minutes") or 120),
                    "daily_at": (auto.get("daily_at") or "09:00"),
                    "notify_url": (auto.get("notify_url") or "")})

    # -- 桌面切换 ---------------------------------------------------------------

    @app.get("/api/snapshots")
    def api_snapshots():
        return _ok([{"name": name, "label": doc.label(), "uid": doc.uid}
                    for name, doc in list_snapshots()])

    @app.post("/api/switch/save")
    async def switch_save(request: Request):
        body = await request.json()
        try:
            doc = save_current_account(body.get("name") or None,
                                       region=body.get("region", "cn"))
            return _ok({"label": doc.label()})
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            return _err(exc)

    @app.post("/api/switch/switch")
    async def switch_do(request: Request):
        body = await request.json()
        try:
            switch_account(body["name"], region=body.get("region", "cn"))
            return _ok()
        except (KeyError, ValueError, FileNotFoundError, RuntimeError) as exc:
            return _err(exc)

    @app.post("/api/switch/restore")
    async def switch_restore(request: Request):
        body = await request.json()
        try:
            restore_previous(body.get("region", "cn"))
            return _ok()
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            return _err(exc)

    # -- 网关/池 ---------------------------------------------------------------

    @app.get("/api/pool")
    def api_pool():
        return _ok(pool.status())

    @app.get("/api/models")
    def api_models():
        return _ok(pool.models())

    # -- 我的模型（已入库账号实际可调用的索引）------------------------------------

    @app.get("/api/my-models")
    def api_my_models():
        """逐账号的可用模型索引（缓存 5 分钟）；前端合并出统一目录。"""
        return _ok(pool.models_by_account())

    # -- 用量账本 ----------------------------------------------------------------

    @app.get("/api/credits")
    def api_credits():
        """逐账号查询 WorkBuddy 余额（只读，不消耗积分；单账号失败不拦整体）。"""
        from .store import load_accounts
        from .upstream import WorkBuddyClient
        out = []
        for d in load_accounts():
            row = {"uid": d.uid, "label": d.label(), "channel": d.channel_name,
                   "region": d.region, "api_ready": d.api_ready}
            if "credits" not in d.capabilities:
                row.update(error="该渠道未声明余额查询能力")
            elif not d.token:
                row.update(error="加密凭据，请重新网页授权后查询")
            else:
                try:
                    row.update(credits=WorkBuddyClient(d).credits())
                except Exception as exc:
                    row.update(error=str(exc)[:120])
            out.append(row)
        return _ok(out)

    @app.get("/api/usage")
    def api_usage():
        return _ok(LEDGER.summary())

    @app.get("/api/usage/export")
    def api_usage_export():
        from fastapi.responses import Response
        csv_text = LEDGER.to_csv()
        return Response(csv_text, media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=wbspy-usage.csv"})

    # -- 聚合渠道 Key 配置（统一穿透 /v1 用）-------------------------------------

    @app.get("/api/keys")
    def api_keys():
        from . import aggregate
        return _ok(aggregate.key_status())

    @app.post("/api/keys")
    async def api_keys_save(request: Request):
        from . import aggregate
        from .providers import load_providers
        body = await request.json()
        pid = str(body.get("provider", "")).strip().lower()
        if not pid:
            return _err("缺少 provider")
        known = {p.get("id") for p in load_providers()}
        if pid not in known:                     # 防手误存成永不生效的孤儿 key
            import difflib
            near = difflib.get_close_matches(pid, [x for x in known if x], n=3)
            return _err(f"渠道 ID「{pid}」不存在"
                        + (f"，是不是想填：{'、'.join(near)}？" if near else ""))
        aggregate.save_key(pid, str(body.get("key") or ""),
                           {"account_id": body.get("account_id")} if body.get("account_id") else None)
        app.state.model_cached_at = 0.0     # 目录即时刷新
        return _ok({"provider": pid})

    @app.delete("/api/keys/{provider}")
    def api_keys_remove(provider: str):
        from . import aggregate
        removed = aggregate.remove_key(provider)
        app.state.model_cached_at = 0.0
        return _ok({"removed": removed})

    # -- 网关访问密钥（网页控制台可改，热生效）-----------------------------------

    @app.get("/api/settings/gateway-key")
    def gateway_key_get():
        from .cli import DEFAULT_API_KEY
        key = app.state.gw_key
        return _ok({"key": key, "is_default": bool(key) and key == DEFAULT_API_KEY,
                    "default": DEFAULT_API_KEY})

    @app.post("/api/settings/gateway-key")
    async def gateway_key_set(request: Request):
        """修改 /v1 网关访问密钥：空值=恢复内置默认；落盘并立即生效。"""
        from .cli import DEFAULT_API_KEY
        body = await request.json()
        new_key = str(body.get("api_key") or "").strip()
        cfg = load_config()
        if new_key == DEFAULT_API_KEY or not new_key:
            cfg.pop("api_key", None)                 # 恢复默认：从 config 移除自定义值
            effective = DEFAULT_API_KEY
        else:
            if len(new_key) < 4:
                return _err("密钥至少 4 个字符")
            cfg["api_key"] = new_key
            effective = new_key
        save_config(cfg)
        app.state.gw_key = effective                 # /v1 校验热生效，无需重启
        _log(f"网关访问密钥已更新（{'内置默认' if effective == DEFAULT_API_KEY else '自定义'}），立即生效。")
        return _ok({"key": effective, "is_default": effective == DEFAULT_API_KEY})

    # -- 网关开关 + 服务端口（开关立即生效；端口保存前试绑校验，重启生效）--------

    @app.get("/api/settings/gateway")
    def gateway_settings_get():
        cfg = load_config()
        cur = getattr(app.state, "web_port", None)
        return _ok({
            "enabled": bool(getattr(app.state, "gw_enabled", True)),
            "aggregate": bool(getattr(app.state, "gw_aggregate", True)),
            "vision": bool(getattr(app.state, "gw_vision", True)),
            "port": (cfg.get("web") or {}).get("port") or "",
            "current_port": cur,
        })

    @app.post("/api/settings/gateway")
    async def gateway_settings_set(request: Request):
        """body: {enabled?: bool, aggregate?: bool, port?: str|int}

        enabled  → 写 config gateway.enabled 并热生效（/v1 立即 404 或恢复）；
        aggregate → 写 config gateway.aggregate 并热生效（/v1/models 是否注入聚合渠道模型）；
        port     → 校验 + 试绑检测（自己占着的当前端口除外）+ 落盘 config web.port，
                   重启服务后生效（restart_required=true 提示前端引导重启）。
        """
        body = await request.json()
        cfg = load_config()
        cur = getattr(app.state, "web_port", None)
        result = {"enabled": bool(getattr(app.state, "gw_enabled", True)),
                  "aggregate": bool(getattr(app.state, "gw_aggregate", True)),
                  "vision": bool(getattr(app.state, "gw_vision", True)),
                  "port": (cfg.get("web") or {}).get("port") or "",
                  "restart_required": False}

        if "enabled" in body:
            enabled = bool(body.get("enabled"))
            cfg.setdefault("gateway", {})["enabled"] = enabled
            app.state.gw_enabled = enabled
            result["enabled"] = enabled
            if enabled:
                _log("对外网关（/v1）：已开启，立即生效。")
            else:
                _log("对外网关（/v1）：已关闭，客户端调用将返回 404（管理界面不受影响）。")

        if "aggregate" in body:
            agg = bool(body.get("aggregate"))
            cfg.setdefault("gateway", {})["aggregate"] = agg
            app.state.gw_aggregate = agg
            app.state.model_cached_at = 0            # 目录 TTL 缓存作废，下次请求立即重建
            result["aggregate"] = agg
            _log("聚合渠道模型：" + ("已纳入 /v1/models 目录。" if agg
                                     else "已从 /v1/models 目录移除。"))

        if "vision" in body:
            vis = bool(body.get("vision"))
            cfg.setdefault("gateway", {})["vision"] = vis
            app.state.gw_vision = vis
            app.state.model_cached_at = 0            # 目录声明跟随开关，缓存作废
            result["vision"] = vis
            _log("视觉透传：" + ("已开启——客户端可传图，图片原样发往上游。" if vis
                                 else "已关闭——图片在网关剥除（文字占位），上游零图片流量。"))

        if "port" in body and str(body.get("port") or "").strip():
            raw = str(body.get("port")).strip()
            if not raw.isdigit() or not 1 <= int(raw) <= 65535:
                return _err("端口需为 1-65535 的整数")
            new_port = int(raw)
            if new_port != cur:
                probe_err = port_bind_error(new_port)
                if probe_err:
                    return _err(probe_err + "，换个端口再保存")
            cfg.setdefault("web", {})["port"] = new_port
            result["port"] = new_port
            result["restart_required"] = new_port != cur
            _log(f"服务端口已保存为 {new_port}"
                 + ("，重启服务后生效。" if new_port != cur else "（与当前端口相同）。"))

        save_config(cfg)
        return _ok(result)

    return app


def run_web(pool: AccountPool, host: str = "127.0.0.1", port: int = 8178,
            api_key: str = "") -> None:
    import uvicorn
    uvicorn.run(create_web_app(pool, api_key, port=port), host=host, port=port,
                log_level="warning")
