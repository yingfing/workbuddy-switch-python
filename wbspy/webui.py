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
    GET    /api/mobile/connection       手机连接网关状态（设备身份）
    POST   /api/mobile/connection       手机连接开关（开启后手机 App 可直连）
    POST   /api/system/restart          重启服务进程
    GET    /api/snapshots               快照列表
    POST   /api/switch/save             保存当前桌面登录
    POST   /api/switch/switch           切换到快照
    POST   /api/switch/restore          恢复切换前登录
    GET    /api/pool                    网关账号池实时状态
    GET    /api/models                  聚合模型目录
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .automation import run_all, poll_worker
from ._version import __version__
from .channels import CHANNELS, WELFARE, get_channel, methods_of, welfare_group
from .document import parse_auth
from .edge_sync import MobileGateway
from .gateway import create_app
from .login_session import GEMINI_CALLBACK_CODES, LoginSessionManager
from .pool import AccountPool
from .usage import LEDGER
from .store import (load_accounts, remove_account, list_snapshots,
                    load_config, save_config)
from .switch import restore_previous, save_current_account, switch_account

STATIC_DIR = Path(__file__).parent / "static"


def _parse_changelog() -> list[dict]:
    """CHANGELOG.md → [{version, date, body}]（倒序）。

    兼容三种安装形态：源码克隆（包目录的上级）、pip 安装（包目录内随包分发）、
    下载替换（仓库根）。找不到文件返回空列表——前端不弹窗即可。
    """
    import re as _re
    candidates = [Path(__file__).resolve().parent.parent / "CHANGELOG.md",
                  Path(__file__).resolve().parent / "CHANGELOG.md",
                  Path.cwd() / "CHANGELOG.md"]
    text = ""
    for cand in candidates:
        if cand.is_file():
            try:
                text = cand.read_text("utf-8")
                break
            except OSError:
                continue
    entries: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = _re.match(r"^##\s*\[([^\]]+)\]\s*[—–-]+\s*(.+?)\s*$", line)
        if m:
            cur = {"version": m.group(1).strip(), "date": m.group(2).strip(), "body": ""}
            entries.append(cur)
        elif cur is not None and line.strip() != "":
            cur["body"] += (("\n" if cur["body"] else "") + line.rstrip())
    for e in entries:                       # 去掉每节尾部的空行堆积
        e["body"] = e["body"].strip()
    return entries


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


# -- 概览页健康横幅 ------------------------------------------------------------

def _round_health(per_account: dict) -> tuple[int, int]:
    """最近一轮签到的 (成功账号数, 总数)。

    账号级失败 = summary 任意项以「失败：」开头（automation._retry 的约定）。
    """
    n = 0
    ok_n = 0
    for rec in (per_account or {}).values():
        s = rec.get("summary") if isinstance(rec, dict) else None
        if not isinstance(s, dict):
            continue
        n += 1
        vals = [v for v in s.values() if isinstance(v, str)]
        if vals and not any(v.startswith("失败：") for v in vals):
            ok_n += 1
    return ok_n, n


def health_state(total: int, api_ready: int, checkin: dict, mobile: dict,
                 week_requests: int, week_errors: int,
                 auto_enabled: bool,
                 recent_requests: int = 0, recent_errors: int = 0,
                 recent_window_s: int = 600) -> dict:
    """概览页顶部「对号/叉号」横幅的状态判定（纯函数，便于测试）。

    error = 硬故障：全部账号 token 失效 / 自动化开启但本轮签到全挂 /
            手机网关报错 / 最近几分钟请求错误率 ≥50%（上游封控或服务
            中断——token 还没被判失效但请求已经在批量失败，只有实时
            可用率能抓住这种「账面正常、实际不可用」的封控）。
    warn  = 有小问题但不影响运行：部分账号失效 / 部分签到失败 /
            最近几分钟错误率 ≥20% / 近 7 日请求错误率高（≥5 次且 >20%）。
    ok    = 一切完美。
    """
    issues: list[tuple[str, str]] = []
    if total > 0 and api_ready == 0:
        issues.append(("error", f"全部 {total} 个账号 token 已失效，需要重新登录"))
    elif total > 0 and api_ready < total:
        issues.append(("warn",
                       f"{total - api_ready} 个账号 token 已失效（{api_ready}/{total} 可用）"))
    if auto_enabled:
        ok_n, n = _round_health(checkin.get("per_account") or {})
        if n:
            if ok_n == 0:
                issues.append(("error", f"最近一轮签到 0/{n} 成功"))
            elif ok_n < n:
                issues.append(("warn", f"最近一轮签到 {ok_n}/{n} 成功，{n - ok_n} 个失败"))
    if mobile.get("enabled"):
        err = (mobile.get("last_error") or "").strip()
        if err:
            issues.append(("error", f"手机网关异常：{err}"))
    if recent_requests >= 5 and recent_errors / recent_requests >= 0.5:
        issues.append(("error",
                       f"最近 {max(1, round(recent_window_s / 60))} 分钟请求错误率 "
                       f"{recent_errors}/{recent_requests}（≥50%）——上游可能已封控或服务中断"))
    elif recent_requests >= 5 and recent_errors / recent_requests >= 0.2:
        issues.append(("warn",
                       f"最近 {max(1, round(recent_window_s / 60))} 分钟请求错误率 "
                       f"{recent_errors}/{recent_requests} 偏高"))
    if week_requests >= 5 and week_errors / week_requests > 0.2:
        issues.append(("warn",
                       f"近 7 日网关请求错误率 {week_errors}/{week_requests}"))
    if not issues:
        return {"level": "ok", "issues": []}
    level = "error" if any(lv == "error" for lv, _ in issues) else "warn"
    return {"level": level, "issues": [{"level": lv, "text": t} for lv, t in issues]}


def create_web_app(pool: AccountPool, api_key: str = "", port: int | None = None) -> FastAPI:
    app = create_app(pool, api_key=api_key)     # 网关路由（/v1/*、/healthz）
    app.state.web_port = port                   # 当前监听端口（重启 argv 重建用）
    app.state.pool = pool                       # 诊断/测试可直达账号池
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
            "welfare": [{"id": w, "label": WELFARE[w], "group": welfare_group(w)}
                        for w in ch.welfare if w in WELFARE],
            "managed": "checkin" in ch.capabilities,
        } for ch in CHANNELS.values()])

    @app.post("/api/login/start")
    async def login_start(request: Request):
        body = await request.json()
        # 回调基址跟随本次请求的面板地址（反向代理请设 WBSPY_PUBLIC_ORIGIN）
        origin = str(request.base_url).rstrip("/")
        try:
            result = sessions.start(body.get("channel", ""), body.get("method"),
                                    origin=origin)
            if result.get("kind") == "browser" and "accounts.google.com" in (result.get("auth_url") or ""):
                _ensure_gemini_listener()
            _log(f"登录发起：{body.get('channel', '?')}（ticket {result.get('ticket', '?')}）")
            return _ok(result)
        except (KeyError, RuntimeError, ValueError) as exc:
            return _err(exc)

    @app.get("/api/changelog")
    def api_changelog():
        """更新日志（结构化，倒序）。前端：新版本弹窗 + 「更新日志」tab。"""
        entries = _parse_changelog()
        current = next((e["version"] for e in entries
                        if e["version"] == __version__), __version__)
        return _ok({"current": __version__, "entries": entries, "in_changelog": bool(entries)})

    @app.get("/api/login/poll/{ticket}")
    def login_poll(ticket: str):
        result = sessions.poll(ticket)
        if result.get("status") == "success":
            doc = result.get("document") or {}
            _log(f"登录成功：{doc.get('label') or doc.get('uid') or '?'}（{ticket}）")
            pool.refresh_documents(load_accounts())   # 新账号即时进池
            # 目录两层缓存（池 5 分钟 + 网关 /v1/models 5 分钟）即时作废：
            # 否则新登的账号（如 GLM）最长要等约 10 分钟才出现在模型列表
            pool.invalidate_model_cache()
            app.state.model_cached_at = 0.0
        elif result.get("status") == "failed":
            _log(f"登录失败（{ticket}）：{result.get('error', '?')}")
        return _ok(result)

    @app.get("/authorize")
    def authorize_callback(request: Request):
        """TRAE / LobsterAI 授权回调：把查询参数投递给等待中的登录票据。"""
        from fastapi.responses import HTMLResponse
        params = dict(request.query_params)
        message = sessions.deliver_callback(params)
        _log(f"授权回调收到：{message}（keys: {','.join(list(params)[:8]) or '无'}）")
        return HTMLResponse(
            '<!doctype html><meta charset="utf-8"><title>wbspy 授权</title>'
            '<body style="font:16px system-ui;padding:48px;text-align:center">'
            f'<p>{message}</p></body>')

    # -- 账号 ---------------------------------------------------------------

    def _account_rows() -> list[dict]:
        """账号列表行（api_accounts 与 dashboard 共用）。"""
        rows = []
        credit_cache = pool._credits_cache()
        for d in load_accounts():
            rec = credit_cache.get(d.uid)
            total = rec.get("total") if isinstance(rec, dict) else None
            rows.append({
                "uid": d.uid, "label": d.label(), "region": d.region,
                "api_ready": d.api_ready, "protected": d.protected_token,
                "expires_at": d.token_expires_at, "domain": d.domain,
                "channel": d.channel_name,
                # 分组键：同渠道同地区才允许拖拽排序（优先级只组内生效）
                "group": f"{d.channel_id or '?'}:{d.region or ''}",
                # 「查询余额」落盘的积分缓存（None = 从未查询）
                "credits": total if isinstance(total, (int, float))
                           and not isinstance(total, bool) else None,
            })
        # 组内按用户拖拽保存的顺序返回（组间保持原顺序）——前端展示顺序
        # 即拖拽基准，拖完 POST 回来即可
        order_map = pool._order_map()
        groups: dict[str, list] = {}
        for a in rows:
            groups.setdefault(a["group"], []).append(a)
        out: list = []
        for g, accs in groups.items():
            lst = order_map.get(g)
            if isinstance(lst, list) and lst:
                accs.sort(key=lambda a: lst.index(a["uid"])
                          if a["uid"] in lst else len(lst))
            out.extend(accs)
        return out

    def _last_checkin_round() -> dict:
        """最近一轮自动化签到结果（dashboard 概览用；无历史返回空）。"""
        from .automation import HISTORY_PATH
        try:
            import json as _json
            if HISTORY_PATH.exists():
                raw = _json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
                if raw and isinstance(raw, list):
                    entry = raw[0]
                    per = {r.get("uid"): {"label": r.get("label"),
                                          "summary": r.get("summary")}
                           for r in entry.get("results", []) if isinstance(r, dict)}
                    return {"at": entry.get("at"), "per_account": per}
        except Exception:
            pass
        return {}

    @app.get("/api/dashboard")
    def api_dashboard():
        """概览页聚合端点：一次拉全仪表盘数据（统计卡 + 图表 + 签到状态）。

        全部读缓存/落盘数据，零上游请求——面板首屏不做任何网络等待。
        """
        from .usage import LEDGER
        from .store import load_config
        rows = _account_rows()
        by_channel: dict[str, int] = {}
        credits_sum = 0.0
        credits_known = 0
        api_ready = 0
        for r in rows:
            by_channel[r["channel"]] = by_channel.get(r["channel"], 0) + 1
            if r["credits"] is not None:
                credits_sum += r["credits"]
                credits_known += 1
            if r["api_ready"]:
                api_ready += 1
        # 7 天用量序列（按天：请求/错误/token）
        usage = LEDGER.summary(7)
        series = [{"date": k,
                   "requests": v.get("requests", 0),
                   "errors": v.get("errors", 0),
                   "tokens": v.get("prompt_tokens", 0) + v.get("completion_tokens", 0)}
                  for k, v in sorted(usage.items())]
        today = time.strftime("%Y-%m-%d")
        today_req = next((s["requests"] for s in series if s["date"] == today), 0)
        gw_state = app.state.mobile_gateway.status()
        alive = len(pool.healthy("*")) if hasattr(pool, "healthy") else None
        mobile_enabled = bool((load_config().get("mobile") or {}).get("enabled"))
        auto_enabled = bool((load_config().get("automation") or {}).get("enabled"))
        last_round = _last_checkin_round()
        mobile_state = {"enabled": mobile_enabled,
                        "last_error": gw_state.get("last_error") or ""}
        recent = LEDGER.recent_stats()
        return _ok({
            "version": __version__,
            "accounts": {"total": len(rows), "api_ready": api_ready,
                         "by_channel": by_channel,
                         "credits_sum": credits_sum if credits_known else None,
                         "credits_known": credits_known},
            "usage": {"series": series, "today_requests": today_req,
                      "week_requests": sum(s["requests"] for s in series),
                      "week_tokens": sum(s["tokens"] for s in series),
                      "week_errors": sum(s["errors"] for s in series),
                      "recent": recent},
            "checkin": last_round,
            "pool": {"alive": alive, "total": len(rows)},
            "mobile": {"enabled": mobile_enabled,
                       "connected": bool(gw_state.get("running")) and bool(gw_state.get("connected")),
                       "last_error": mobile_state["last_error"]},
            "auto_enabled": auto_enabled,
            "health": health_state(
                total=len(rows), api_ready=api_ready, checkin=last_round,
                mobile=mobile_state,
                week_requests=sum(s["requests"] for s in series),
                week_errors=sum(s["errors"] for s in series),
                auto_enabled=auto_enabled,
                recent_requests=recent["requests"],
                recent_errors=recent["errors"],
                recent_window_s=recent["window_s"]),
        })

    @app.get("/api/accounts")
    def api_accounts():
        return _ok(_account_rows())

    @app.post("/api/accounts/order")
    async def accounts_order(request: Request):
        """拖拽排序落盘：body {group, uids}。priority 策略下组内按此顺序取号。"""
        body = await request.json()
        try:
            pool.set_priority_order(str(body.get("group") or ""),
                                    body.get("uids"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        uids = body.get("uids") or []
        preview = (" → ".join(map(str, uids[:8]))
                   + ("…" if len(uids) > 8 else "")) \
            if isinstance(uids, list) and uids else "（空）"
        _log(f"账号优先级已保存（{body.get('group')}）：{preview}")
        return _ok()

    @app.delete("/api/accounts/{uid}")
    def api_account_remove(uid: str, domain: str = ""):
        if not remove_account(uid, domain=domain):
            raise HTTPException(404, "账号不存在")
        pool.refresh_documents(load_accounts())
        pool.invalidate_model_cache()
        app.state.model_cached_at = 0.0
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

    # -- 手机连接网关（edge-sync 伪装桌面端）----------------------------------

    from .store import ROOT as _store_root
    app.state.mobile_gateway = MobileGateway(pool, log=_log,
                                             state_dir=_store_root / "data")

    def _mobile_sync() -> dict:
        """按 config.mobile.enabled 同步网关线程状态。"""
        enabled = bool((load_config().get("mobile") or {}).get("enabled"))
        gw = app.state.mobile_gateway
        if enabled:
            gw.start()
        elif gw.status()["running"]:
            gw.stop()
        return {"enabled": enabled, **gw.status()}

    @app.get("/api/mobile/connection")
    def mobile_connection_status():
        cfg = load_config().get("mobile") or {}
        gw = app.state.mobile_gateway
        dev = gw.device()                            # 惰性生成也在此暴露给面板
        return _ok({"enabled": bool(cfg.get("enabled")),
                    **gw.status(),
                    "device_id": dev["device_id"],
                    "device_name": dev["device_name"]})

    @app.post("/api/mobile/connection")
    async def mobile_connection_toggle(request: Request):
        """手机连接开关：开 = wbspy 伪装桌面端上线，手机 App 可直连派任务。"""
        body = await request.json()
        enabled = bool(body.get("enabled"))
        cfg = load_config()
        cfg.setdefault("mobile", {})["enabled"] = enabled
        save_config(cfg)
        state = _mobile_sync()
        _log("手机连接：已开启（手机 App「连接电脑端」可检测到本机）"
             if enabled else "手机连接：已关闭")
        return _ok(state)

    _mobile_sync()                                       # 服务重启后恢复开关状态

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

    @app.get("/api/diagnose")
    def api_diagnose():
        """一键排查：把账号池/健康判定/错误分布/配置摘要/最近日志聚合成
        一份脱敏纯文本报告（token/API key 不出现原文，uid 只露前 12 位），
        诊断页一键复制后可直接发给 AI 排障。"""
        now = time.time()
        cfg = load_config()
        gw_cfg = cfg.get("gateway") or {}
        st = pool.status()                          # label/region/channel/dead/cooldowns
        n_ready = sum(1 for s in st if s.get("api_ready"))
        recent = LEDGER.recent_stats()
        week = LEDGER.summary(7)
        week_req = sum(v.get("requests", 0) for v in week.values())
        week_err = sum(v.get("errors", 0) for v in week.values())
        today = time.strftime("%Y-%m-%d")
        today_req = (week.get(today) or {}).get("requests", 0)
        mobile_st = app.state.mobile_gateway.status()
        auto_enabled = bool((cfg.get("automation") or {}).get("enabled"))
        mobile_enabled = bool((cfg.get("mobile") or {}).get("enabled"))
        checkin = _last_checkin_round()
        rows = _account_rows()
        health = health_state(
            total=len(rows), api_ready=n_ready, checkin=checkin,
            mobile={"enabled": mobile_enabled,
                    "last_error": mobile_st.get("last_error") or ""},
            week_requests=week_req, week_errors=week_err,
            auto_enabled=auto_enabled,
            recent_requests=recent["requests"], recent_errors=recent["errors"],
            recent_window_s=recent["window_s"])

        L: list[str] = []
        L.append(f"【wbspy 诊断报告】")
        L.append(f"生成 {time.strftime('%Y-%m-%d %H:%M:%S')} · 版本 {__version__} · "
                 f"平台 {sys.platform}")
        L.append("")
        L.append("── 健康判定 ──")
        L.append(f"整体: {health['level'].upper()}")
        for it in health["issues"]:
            L.append(f"  • {it['text']}")
        if not health["issues"]:
            L.append("  • 一切完美，正在正常运行")
        L.append("")
        L.append("── 最近 10 分钟请求 ──")
        if recent["requests"]:
            rate = recent["errors"] / recent["requests"] * 100
            L.append(f"共 {recent['requests']} 次 · 错 {recent['errors']} 次"
                     f"（错误率 {rate:.0f}%）")
            for code, n in sorted(recent.get("errors_by_code", {}).items(),
                                  key=lambda kv: -kv[1]):
                L.append(f"  · {code} ×{n}")
        else:
            L.append("窗口内暂无请求")
        L.append("")
        L.append(f"── 账号池（{len(st)} 个账号，{n_ready} 个可用）──")
        for i, s in enumerate(st, 1):
            exp = s.get("token_expires_at")
            exp_txt = time.strftime("%m-%d %H:%M", time.localtime(exp / 1000)) \
                if isinstance(exp, (int, float)) else "未知"
            flags = []
            if not s.get("api_ready"):
                flags.append("token 已失效")
            if s.get("dead"):
                flags.append("死亡标记（等待续期复活）")
            L.append(f"{i}. 「{s.get('label')}」渠道 {s.get('channel')} · "
                     f"token 至 {exp_txt}"
                     + ("" if not flags else " · " + " · ".join(flags)))
            for c in s.get("cooldowns") or []:
                until = time.strftime("%H:%M:%S", time.localtime(c["cooldown_until"]))
                wait = max(0, int(c["cooldown_until"] - now))
                L.append(f"   冷却: {c['model']} → 还有 {wait}s（至 {until}）")
        L.append("")
        L.append("── 近 7 日 ──")
        pct = week_err / week_req * 100 if week_req else 0
        L.append(f"请求 {week_req} · 错误 {week_err}（{pct:.1f}%）· 今日 {today_req}")
        L.append("")
        L.append("── 配置摘要 ──")
        key_state = "已设置" if (gw_cfg.get("api_key") or
                                 (cfg.get("security") or {}).get("api_key")) else "默认未设置"
        L.append(f"取号策略 {gw_cfg.get('distribution') or 'round-robin'} · "
                 f"记忆注入 {'开' if gw_cfg.get('memory_inject', True) else '关'} · "
                 f"视觉透传 {'开' if gw_cfg.get('vision', True) else '关'} · "
                 f"网关 API Key {key_state}")
        L.append(f"自动化 {'开' if auto_enabled else '关'} · "
                 f"手机桥接 {'开' if mobile_enabled else '关'}"
                 + (f"（{'已连接' if mobile_st.get('connected') else '未连接'}）"
                    if mobile_enabled else "")
                 + f" · 网关端口 {getattr(app.state, 'web_port', '?')}")
        L.append("")
        L.append("── 模型目录 ──")
        # 只读池内已加载缓存，不主动打上游（诊断零副作用、无网络依赖）
        cache = getattr(pool, "_mba_cache", None) or []
        try:
            n_all = sum(len(rows) for _d, rows in cache)
            n_cm = sum(1 for _d, rows in cache for m in rows
                       if isinstance(m.get("credit_multiplier"), (int, float))
                       and not isinstance(m.get("credit_multiplier"), bool))
            tail = ("（目录缓存未加载，打开「我的模型」页后再诊断）"
                    if not cache else
                    " —— 倍率全部缺失，官方目录疑已改版（看样本 keys 认字段）"
                    if n_all and n_cm == 0 else "")
            L.append(f"{len(cache)} 个账号共 {n_all} 个模型 · 带倍率 {n_cm} 个" + tail)
            for d, rows in cache[:2]:           # 样本：字段名一眼识别改版姿势
                for m in rows[:3]:
                    keys = ",".join(sorted(str(k) for k in m.keys()))
                    L.append(f"  · [{d.label()}] {m['id']} "
                             f"cm={m.get('credit_multiplier')!r} keys={keys}")
        except Exception as exc:
            L.append(f"目录读取失败：{type(exc).__name__}: {exc}")
        L.append("")
        L.append(f"── 最近运行日志（{min(30, len(app.state.auto_log))} 行）──")
        L.extend(app.state.auto_log[-30:])
        report = "\n".join(L)
        return _ok({"report": report, "health": health, "recent": recent,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    # -- 11128 抓现行（被拒请求体留存） -----------------------------------------

    @app.get("/api/denials")
    def api_denials():
        from .upstream import DENIALS_DIR, DENIALS_KEEP
        items = []
        if DENIALS_DIR.is_dir():
            for p in sorted(DENIALS_DIR.glob("*.json"), reverse=True):
                items.append({"name": p.name, "size": p.stat().st_size,
                              "at": time.strftime("%m-%d %H:%M:%S",
                                                  time.localtime(p.stat().st_mtime))})
        return _ok({"items": items[:DENIALS_KEEP], "dir": str(DENIALS_DIR)})

    @app.get("/api/denials/{name}")
    def api_denial_body(name: str):
        from .upstream import DENIALS_DIR
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(404, "not found")
        p = DENIALS_DIR / name
        if not p.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(p, media_type="application/json", filename=name)

    @app.delete("/api/denials/{name}")
    def api_denial_delete(name: str):
        from .upstream import DENIALS_DIR
        if "/" in name or "\\" in name or ".." in name:
            return _err("bad name", 404)
        p = DENIALS_DIR / name
        if p.is_file():
            p.unlink()
        return _ok()

    @app.post("/api/probe")
    async def api_probe(request: Request):
        """11128 取证探针：任意 messages 片段经完整出站管线发给上游，回传 200/403。

        body: {"messages": [{"role": "user", "content": "..."}]}
        用账号池活号发最小请求（max_tokens=1）。刻意绕开 chat() 的对策二
        重试（system 降级会把「有罪」探成「无罪」）与 annotate_denial 落盘
        （探针高频调用会把真实抓包挤出 denials 滚动窗口）。用途：把被拒
        请求的消息对半拆开逐段探——403 的那半就是新指纹所在——这是
        xdpool 同款定位法，从「猜」变成「看」。
        """
        body = await request.json()
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return _err("messages 不能为空")
        alive = pool.healthy("*")
        if not alive:
            return _err("账号池无可用账号", 503)
        import uuid as _uuid
        from .pool import _client_for
        from .upstream import UpstreamError, classify_business_error, \
            normalize_payload
        client = _client_for(alive[0])
        payload_text = json.dumps(
            normalize_payload({"messages": msgs, "max_tokens": 1}))
        t0 = time.monotonic()
        try:
            resp = client.http.post(
                f"{client.doc.origin}/v2/chat/completions",
                headers=client._chat_headers(str(_uuid.uuid4())),
                data=payload_text, timeout=60, allow_redirects=False, stream=True)
        except (requests.RequestException, UpstreamError) as exc:
            return _err(f"探针请求失败：{exc}", 502)
        status = resp.status_code
        if resp.ok:
            resp.close()
            return _ok({"status": status, "denied": False,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "account": alive[0].label(), "message": ""})
        err = classify_business_error(status, resp.text[:1024 * 1024],
                                      resp.headers.get("Retry-After", ""),
                                      resp.headers.get("Content-Type", ""))
        resp.close()
        return _ok({"status": status, "denied": err.status == 403,
                    "upstream_code": err.upstream_code,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "account": alive[0].label(), "message": err.message[:300]})

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

    @app.get("/api/health")
    def api_health():
        """面板首屏健康总览：哪里不行、怎么处理，一眼看完。

        报警源（按严重度排）：凭据失效（池标死）> 渠道全灭 > 登录过期
        （等自动续期）> 积分低于降级阈值。每条都带行动指引，打开页面
        第一屏就知道「哪里不行、怎么搞」。
        """
        import time as _time
        now = _time.time()
        gw = (load_config().get("gateway") or {})
        thr = gw.get("priority_threshold")
        thr = float(thr) if isinstance(thr, (int, float)) and not isinstance(thr, bool) else 100.0
        cache = gw.get("credits_cache") or {}
        from .pool import _key
        alerts: list[dict] = []
        lanes: dict[str, dict] = {}
        for d in load_accounts():
            lane = d.channel_name or d.domain or "未知渠道"
            st = lanes.setdefault(lane, {"total": 0, "alive": 0})
            st["total"] += 1
            if pool.dead_until.get(_key(d), 0) > now:
                alerts.append({"level": "error", "title": f"「{d.label()}」凭据失效",
                               "action": "到「账号池」页重新网页授权（扫码或浏览器均可）",
                               "tab": "accounts"})
            elif not d.api_ready:
                alerts.append({"level": "warn", "title": f"「{d.label()}」登录已过期",
                               "action": "等待自动续期（最长 5 分钟）；未恢复请重新网页授权",
                               "tab": "accounts"})
            else:
                st["alive"] += 1
                rec = cache.get(d.uid)
                total = rec.get("total") if isinstance(rec, dict) else None
                if thr > 0 and isinstance(total, (int, float)) and total < thr:
                    # 渠道没有 checkin 能力（如国际版 workbuddy，后端无签到
                    # 接口）→ 积分只能消耗不能自动补，「去领取」是误导
                    can_checkin = "checkin" in (d.capabilities or ())
                    alerts.append({
                        "level": "warn",
                        "title": f"「{d.label()}」积分 {int(total)} 低于降级阈值 {int(thr)}",
                        "action": ("到「积分自动化」页领取额度，或在账号池页调低阈值"
                                   if can_checkin else
                                   "该渠道无签到接口，积分无法自动补给——"
                                   "建议在账号池页调低该号阈值，用完即止"),
                        "tab": "auto" if can_checkin else "accounts"})
        for lane, st in lanes.items():
            if st["total"] and not st["alive"]:
                alerts.append({"level": "error",
                               "title": f"{lane} 全部账号不可用（{st['total']} 个）",
                               "action": "该渠道模型此刻无法调用；到「账号池」页重新授权",
                               "tab": "accounts"})
        rank = {"error": 0, "warn": 1}
        alerts.sort(key=lambda a: rank.get(a.get("level"), 9))
        return _ok({"healthy": not alerts,
                    "total": sum(s["total"] for s in lanes.values()),
                    "alive": sum(s["alive"] for s in lanes.values()),
                    "lanes": lanes, "alerts": alerts})

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
        """逐账号查询 WorkBuddy 余额（只读，不消耗积分；单账号失败不拦整体）。

        客户端按渠道分派（pool.client）：ZCode 的余额走 ZCodeClient 专用
        接口（billing/balance），此前硬编码 WorkBuddyClient 导致 ZCode 的
        chatglm.cn 域名不在 api_origin 白名单里，被安全兜底拒发凭据——
        界面红字「服务地区或域名无法识别」。未声明 credits 能力的渠道
        返回 notice（中性提示）而非 error（红字），不是故障不该报警。"""
        from .store import load_accounts
        from .pool import _client_for, _key
        out = []
        for d in load_accounts():
            row = {"uid": d.uid, "label": d.label(), "channel": d.channel_name,
                   "region": d.region, "api_ready": d.api_ready}
            if "credits" not in d.capabilities:
                row.update(notice="该渠道未声明余额查询能力")
            elif not d.token:
                row.update(error="加密凭据，请重新网页授权后查询")
            else:
                try:
                    cli = pool.clients.get(_key(d)) or _client_for(d)
                    credits = cli.credits()
                    row.update(credits=credits)
                    # 落盘缓存 → priority 策略的积分降级、账号列表阈值标红都用它
                    pool.remember_credits(d.uid, credits.get("total"))
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

    @app.get("/api/settings/panel-key")
    def panel_key_get():
        key = (load_config().get("panel_key") or "").strip()
        return _ok({"enabled": bool(key)})

    @app.post("/api/settings/panel-key")
    async def panel_key_set(request: Request):
        """面板访问密钥开关：默认关闭（家人局域网不设防）。开启后所有 /api/*
        管理端点要求 X-Panel-Key（?key= 亦可），/v1 网关不受影响（有自己的
        api_key）。设空 = 一键关闭。热生效，无需重启。"""
        body = await request.json()
        new_key = str(body.get("panel_key") or "").strip()
        cfg = load_config()
        if new_key:
            if len(new_key) < 4:
                return _err("密钥至少 4 个字符")
            cfg["panel_key"] = new_key
        else:
            cfg.pop("panel_key", None)               # 一键关闭
        save_config(cfg)
        _log("面板访问密钥已" + ("开启（本浏览器自动记住）" if new_key else "关闭"))
        return _ok({"enabled": bool(new_key), "key": new_key})

    @app.middleware("http")
    async def _panel_guard(request: Request, call_next):
        """面板密钥守卫：空配置时零开销直通（默认行为与旧版完全一致）。"""
        key = (load_config().get("panel_key") or "").strip()
        p = request.url.path
        if key and (p.startswith("/api/") or p == "/authorize"):
            supplied = (request.headers.get("x-panel-key")
                        or request.query_params.get("key") or "")
            if supplied != key:
                from fastapi.responses import JSONResponse
                return JSONResponse({"ok": False, "error": "面板密钥不匹配"},
                                    status_code=401)
        return await call_next(request)

    # -- 网关开关 + 服务端口（开关立即生效；端口保存前试绑校验，重启生效）--------

    def _fb_group_rows() -> list[dict]:
        """容灾组 + 每组当前目录里匹配到的模型 id（关键词筛选预览）。"""
        from .gateway import _fb_groups
        groups = _fb_groups()
        cat: list[str] = []
        for r in app.state.model_cache or []:
            rid = str(r.get("id") or "")
            if rid:
                cat.append(rid)
        try:
            from . import aggregate as _agg
            for r in _agg.models():
                cat.append(str(r.get("id") or ""))
        except Exception:
            pass
        out = []
        for g in groups:
            kw = str(g.get("keyword") or "").strip().casefold()
            matched = [rid for rid in cat if kw and kw in rid.casefold()]
            out.append({"name": g.get("name") or kw, "keyword": kw,
                        "matched": matched, "count": len(matched)})
        return out

    # -- 渠道屏蔽（对话下架；签到/余额/续期照常，默认什么都不屏蔽）--------------

    @app.get("/api/settings/channel-hide")
    def channel_hide_get():
        from .channels import CHANNELS
        disabled = pool.disabled_channels()
        chans = [{"id": c.id, "name": c.name, "note": c.note,
                  "disabled": c.id in disabled}
                 for c in CHANNELS.values() if "chat" in c.capabilities]
        return _ok({"channels": chans})

    @app.post("/api/settings/channel-hide")
    async def channel_hide_set(request: Request):
        """body: {disabled?: [channel_id]}——空数组 = 全部恢复（默认态）。"""
        from .channels import CHANNELS
        body = await request.json()
        ids = body.get("disabled") or []
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise HTTPException(400, "disabled 必须是渠道 id 字符串数组")
        unknown = [x for x in ids if x not in CHANNELS]
        if unknown:
            raise HTTPException(400, f"未知渠道：{'/'.join(unknown)}")
        cfg = load_config()
        cfg.setdefault("gateway", {})["disabled_channels"] = ids
        save_config(cfg)
        pool.invalidate_model_cache()          # 模型目录即时刷新
        return _ok({"disabled": ids})

    @app.get("/api/settings/gateway")
    def gateway_settings_get():
        cfg = load_config()
        cur = getattr(app.state, "web_port", None)
        return _ok({
            "enabled": bool(getattr(app.state, "gw_enabled", True)),
            "aggregate": bool(getattr(app.state, "gw_aggregate", True)),
            "vision": bool(getattr(app.state, "gw_vision", True)),
            "memory_inject": bool(getattr(app.state, "gw_memory", True)),
            "distribution": str((cfg.get("gateway") or {}).get("distribution")
                                or "round-robin"),
            "priority_threshold": pool._priority_threshold(),
            "fallback_groups": _fb_group_rows(),
            "port": (cfg.get("web") or {}).get("port") or "",
            "current_port": cur,
        })

    @app.post("/api/settings/gateway")
    async def gateway_settings_set(request: Request):
        """body: {enabled?: bool, aggregate?: bool, vision?: bool,
                  distribution?: str, priority_threshold?: number,
                  port?: str|int}

        enabled  → 写 config gateway.enabled 并热生效（/v1 立即 404 或恢复）；
        aggregate → 写 config gateway.aggregate 并热生效（/v1/models 是否注入聚合渠道模型）；
        distribution → 账号分配策略（round-robin/priority/balanced），热生效；
        priority_threshold → priority 策略的积分降级阈值（0 = 关闭积分感知），热生效；
        port     → 校验 + 试绑检测（自己占着的当前端口除外）+ 落盘 config web.port，
                   重启服务后生效（restart_required=true 提示前端引导重启）。
        """
        body = await request.json()
        cfg = load_config()
        cur = getattr(app.state, "web_port", None)
        result = {"enabled": bool(getattr(app.state, "gw_enabled", True)),
                  "aggregate": bool(getattr(app.state, "gw_aggregate", True)),
                  "vision": bool(getattr(app.state, "gw_vision", True)),
            "memory_inject": bool(getattr(app.state, "gw_memory", True)),
                  "distribution": str((cfg.get("gateway") or {}).get("distribution")
                                      or "round-robin"),
                  "priority_threshold": pool._priority_threshold(),
                  "port": (cfg.get("web") or {}).get("port") or "",
                  "restart_required": False}

        if "distribution" in body:
            from .pool import DISTRIBUTIONS
            dist = str(body.get("distribution") or "round-robin")
            if dist not in DISTRIBUTIONS:
                raise HTTPException(400, f"分发策略不认识（可选：{'/'.join(DISTRIBUTIONS)}）")
            cfg.setdefault("gateway", {})["distribution"] = dist
            result["distribution"] = dist
            _log(f"账号分配策略：{dist}（热生效，下一次取号即按新策略）。")

        if "priority_threshold" in body:
            try:
                thr = pool.set_priority_threshold(body.get("priority_threshold"))
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            result["priority_threshold"] = thr
            _log(f"积分降级阈值：{thr:g}（priority 策略热生效）"
                 + ("，已关闭积分感知" if thr <= 0 else "") + "。")

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

        if "memory_inject" in body:
            mem = bool(body.get("memory_inject"))
            cfg.setdefault("gateway", {})["memory_inject"] = mem
            app.state.gw_memory = mem
            result["memory_inject"] = mem
            _log("记忆注入：" + ("已开启——workbuddy 泳道对话自动携带该账号的服务端记忆。"
                                 if mem else "已关闭——对话不再注入记忆。"))

        if "fallback_groups" in body:
            groups = body.get("fallback_groups")
            if not isinstance(groups, list):
                return _err("fallback_groups 须为 [{name, keyword}] 列表")
            clean = []
            for g in groups:
                if not isinstance(g, dict):
                    continue
                kw = str(g.get("keyword") or "").strip()
                if not kw:
                    continue
                clean.append({"name": str(g.get("name") or kw)[:40],
                              "keyword": kw[:60]})
            cfg.setdefault("gateway", {})["fallback_groups"] = clean
            result["fallback_groups"] = clean
            _log(f"容灾组已保存：{len(clean)} 组（"
                 + "、".join(f"{g['name']}~{g['keyword']}" for g in clean[:6])
                 + (f" 等 {len(clean)} 组" if len(clean) > 6 else "")
                 + "）。调用的模型整体挂掉时自动降级组内其他可用模型。")

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
