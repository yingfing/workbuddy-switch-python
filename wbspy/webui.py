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

from .automation import run_all
from .channels import CHANNELS, get_channel, methods_of
from .document import parse_auth
from .gateway import create_app
from .login_session import GEMINI_CALLBACK_CODES, LoginSessionManager
from .pool import AccountPool
from .providers import category_stats, free_model_index, provider_rows
from .usage import LEDGER
from .store import (load_accounts, remove_account, list_snapshots)
from .switch import restore_previous, save_current_account, switch_account

STATIC_DIR = Path(__file__).parent / "static"


def _ok(data=None, **extra) -> dict:
    return {"ok": True, "data": data, **extra}


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": str(message)})


def create_web_app(pool: AccountPool, api_key: str = "") -> FastAPI:
    app = create_app(pool, api_key=api_key)     # 网关路由（/v1/*、/healthz）
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
    app.state.auto_log: list[str] = []

    def _log(line: str) -> None:
        app.state.auto_log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        del app.state.auto_log[:-200]

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

    @app.get("/api/automation/status")
    def automation_status():
        return _ok({"running": app.state.auto_running, "log": app.state.auto_log[-60:]})

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

    # -- 免费可调用 API（吸收 dsh-xdpool「聚合渠道」页）--------------------------

    @app.get("/api/providers")
    def api_providers():
        return _ok({"stats": category_stats(), "providers": provider_rows()})

    @app.get("/api/free-models")
    def api_free_models():
        mine = pool.models()
        return _ok({
            "mine": mine,
            "embeddings": [
                {"id": f"{d.region}:{m['id']}", "name": m["name"], "region": d.region}
                for d in pool.healthy() for m in (
                    {"id": "bge-m3", "name": "BGE-M3"},
                    {"id": "hunyuan-embedding", "name": "Hunyuan Embedding"})
            ],
            "free": free_model_index(),
        })

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
            if not d.token:
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
        body = await request.json()
        pid = str(body.get("provider", "")).strip()
        if not pid:
            return _err("缺少 provider")
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

    return app


def run_web(pool: AccountPool, host: str = "127.0.0.1", port: int = 8178,
            api_key: str = "") -> None:
    import uvicorn
    uvicorn.run(create_web_app(pool, api_key), host=host, port=port, log_level="warning")
