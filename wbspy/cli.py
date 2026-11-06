"""命令行入口：python -m wbspy <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .automation import loop, run_all
from .channels import CHANNELS, get_channel, methods_of
from .document import parse_auth
from .login import login
from .pool import AccountPool
from .store import (load_accounts, load_config, remove_account, save_account,
                    save_config, list_snapshots)
from .switch import restore_previous, save_current_account, switch_account
from .upstream import WorkBuddyClient

import requests


def _cmd_login(args) -> int:
    doc = login(args.channel, method=args.method, open_browser=not args.no_browser)
    save_account(doc)
    print(f"✓ 已入库：{doc.label()}")
    return 0


def _cmd_channels(_args) -> int:
    print(f"{'渠道':<22}{'方式':<18}{'说明'}")
    for ch in CHANNELS.values():
        print(f"{ch.name:<22}{'+'.join(methods_of(ch)):<18}{ch.note}")
    return 0


def _cmd_accounts(args) -> int:
    accounts = load_accounts()
    if args.action == "list":
        if not accounts:
            print("账号库为空。先执行 wbspy login <渠道> 添加账号。")
            return 0
        for doc in accounts:
            ready = "可用" if doc.api_ready else ("加密凭据(仅切换)" if doc.protected_token else "token 已过期")
            print(f"· {doc.label()}  [{ready}]")
    elif args.action == "remove":
        uid = args.uid
        matched = [d for d in accounts if d.uid.startswith(uid)]
        if not matched:
            print(f"未找到 uid 前缀为 {uid} 的账号。")
            return 1
        for doc in matched:
            remove_account(doc.uid)
            print(f"✓ 已移除：{doc.label()}")
    return 0


def _cmd_quota(_args) -> int:
    for doc in load_accounts():
        print(f"· {doc.label()}")
        if not doc.token:
            print("    加密凭据，请重新网页授权后查询。")
            continue
        try:
            c = WorkBuddyClient(doc).credits()
            print(f"    剩余 {c['total']} 积分"
                  + (f"（其中 {c['expiring_soon']} 三天内到期）" if c.get("expiring_soon") else ""))
            for p in c.get("packages", []):
                exp = f"，{time.strftime('%m-%d', time.localtime(p['expires_at'] / 1000))} 到期" \
                    if p.get("expires_at") else ""
                print(f"      - {p['name']}: {p['remain']}/{p['size']}"
                      + ("（月度）" if p["monthly"] else "") + exp)
        except Exception as exc:
            print(f"    积分查询失败：{exc}")
    return 0


def _cmd_checkin(_args) -> int:
    run_all(load_accounts())
    return 0


def _cmd_auto(args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("账号库为空，先 wbspy login。")
        return 1
    if args.once:
        run_all(accounts)
    elif args.daily:
        from .automation import schedule
        schedule(accounts)
    else:
        loop(accounts, interval_hours=args.interval)
    return 0


def _cmd_models(_args) -> int:
    pool = AccountPool(load_accounts())
    for row in pool.models():
        print(f"· {row['id']:<44}{row['name']}")
    return 0


def _cmd_chat(args) -> int:
    pool = AccountPool(load_accounts())
    payload = {"model": args.model, "messages": [{"role": "user", "content": args.prompt}]}
    try:
        doc, resp = pool.chat_failover(payload)
    except Exception as exc:
        print(f"对话失败：{exc}")
        return 1
    print(f"[{doc.label()}]")
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            text = line.decode("utf-8", "ignore").strip()
            if text.startswith("data: ") and text != "data: [DONE]":
                try:
                    delta = json.loads(text[6:])["choices"][0].get("delta", {})
                    print(delta.get("content", ""), end="", flush=True)
                except (ValueError, KeyError, IndexError):
                    continue
    print()
    resp.close()
    return 0


DEFAULT_API_KEY = "workbuddy"


def _resolve_api_key(explicit: str, config: dict, allow_insecure: bool = False) -> str:
    """网关访问密钥解析：--api-key → WBSPY_API_KEY → config.api_key →
    默认 ``workbuddy``（内置兜底，局域网开箱即用）。显式 --allow-insecure
    表示完全放开校验（api_key 置空）。"""
    if allow_insecure:
        return ""
    return (explicit or os.environ.get("WBSPY_API_KEY", "")
            or config.get("api_key", "") or DEFAULT_API_KEY)


def _effective_port(explicit: int | None, config: dict) -> int:
    """服务端口解析：命令行 --port → config web.port（网页设置卡可改）→ 8178。"""
    if explicit:
        return explicit
    try:
        return int((config.get("web") or {}).get("port") or 8178)
    except (TypeError, ValueError):
        return 8178


def _ensure_port_free(port: int) -> None:
    """启动前试绑目标端口：被占用/无权限时打印中文指引后退出（exit 1）。

    不靠 uvicorn 的英文报错裸退——端口冲突是最常见的启动失败，值得一步到位。
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        import sys as _sys
        reason = ("需要管理员权限（<1024 是特权端口）" if isinstance(exc, PermissionError)
                  else "已被其他程序占用")
        print(f"启动失败：端口 {port} {reason}。", file=_sys.stderr)
        print("  可以：", file=_sys.stderr)
        print(f"    1. 查占用者：ss -tlnp | grep :{port}", file=_sys.stderr)
        print("    2. 临时换端口：wbspy web --port <其他端口>", file=_sys.stderr)
        print('    3. 永久改端口：网页「网关」设置卡里改（重启生效），'
              '或编辑 ~/.wbspy/config.json 加 "web": {"port": 8178}', file=_sys.stderr)
        raise SystemExit(1)
    finally:
        probe.close()


def _cmd_gateway(args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("账号库为空，先 wbspy login。")
        return 1
    config = load_config()
    port = _effective_port(getattr(args, "port", None), config)
    api_key = _resolve_api_key(args.api_key, config,
                               getattr(args, "allow_insecure", False))
    _public_guard(args.host, api_key, getattr(args, "allow_insecure", False))
    _ensure_port_free(port)
    from .pool import AccountPool as Pool
    from .gateway import run_gateway
    print(f"网关启动：http://{args.host}:{port}/v1  （账号 {len(accounts)} 个）")
    run_gateway(Pool(accounts), host=args.host, port=port, api_key=api_key)
    return 0


def _public_guard(host: str, api_key: str, allow_insecure: bool = False) -> None:
    """公网/局域网绑定且未配 api_key 时拒绝启动（防止账号池被人白嫖）。"""
    if api_key or allow_insecure or host in ("127.0.0.1", "localhost", "::1"):
        return
    import sys as _sys
    print("拒绝启动：监听地址不是本机回环且未设置 api_key —— "
          "任何能连到这个端口的人都可以直接用你的账号池和聚合渠道额度。", file=_sys.stderr)
    print("  二选一：", file=_sys.stderr)
    print("    1. 加 --api-key <你的密钥>（调用方需带 Authorization: Bearer <密钥>）", file=_sys.stderr)
    print("    2. 确认有其他访问控制（如反向代理鉴权）时加 --allow-insecure 强行启动", file=_sys.stderr)
    raise SystemExit(2)


def _cmd_web(args) -> int:
    """Web 界面 + 网关同进程。"""
    config = load_config()
    port = _effective_port(getattr(args, "port", None), config)
    api_key = _resolve_api_key(args.api_key, config,
                               getattr(args, "allow_insecure", False))
    _public_guard(args.host, api_key, getattr(args, "allow_insecure", False))
    _ensure_port_free(port)
    pool = AccountPool(load_accounts())
    from .gateway import create_app  # noqa: F401  确保依赖就绪
    from .webui import run_web
    src = ("命令行" if getattr(args, "port", None)
           else ("config web.port" if (config.get("web") or {}).get("port") else "默认"))
    print(f"Web 界面：http://{args.host}:{port}/   （界面 + 网关同端口，端口来源：{src}）")
    if not pool.documents:
        print("提示：账号库为空，先在网页「登录」页添加账号。")
    run_web(pool, host=args.host, port=port, api_key=api_key)
    return 0


def _cmd_switch(args) -> int:
    if args.action == "save":
        save_current_account(args.name, region=args.region)
    elif args.action == "list":
        snaps = list_snapshots()
        if not snaps:
            print("暂无快照。wbspy switch save 先保存当前登录。")
        for name, doc in snaps:
            print(f"· {name:<40}{doc.label()}")
    elif args.action == "switch":
        if not args.name:
            print("请指定快照名：wbspy switch switch <name>")
            return 1
        switch_account(args.name, region=args.region)
    elif args.action == "restore":
        restore_previous(args.region)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wbspy", description="WorkBuddy Switch Python 版")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("channels", help="列出全部登录渠道与方式")
    p.set_defaults(fn=_cmd_channels)

    p = sub.add_parser("login", help="登录并入库（qr 优先，自动回退）")
    p.add_argument("channel", choices=sorted(CHANNELS))
    p.add_argument("--method", choices=["qr", "browser", "local"], default=None,
                   help="强制指定登录方式（缺省按 渠道>浏览器>本机 回退）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(fn=_cmd_login)

    p = sub.add_parser("accounts", help="账号库管理")
    p.add_argument("action", choices=["list", "remove"])
    p.add_argument("uid", nargs="?", default="")
    p.set_defaults(fn=_cmd_accounts)

    p = sub.add_parser("quota", help="查询各账号积分")
    p.set_defaults(fn=_cmd_quota)

    p = sub.add_parser("checkin", help="立即跑一轮签到/旅行/任务")
    p.set_defaults(fn=_cmd_checkin)

    p = sub.add_parser("auto", help="自动化托管（定时签到/旅行/任务）")
    p.add_argument("--interval", type=float, default=6.0, help="轮询间隔小时数")
    p.add_argument("--daily", action="store_true", help="每日定点（config automation.daily_at，默认 09:00）")
    p.add_argument("--once", action="store_true", help="只跑一轮（挂 crontab 用）")
    p.set_defaults(fn=_cmd_auto)

    p = sub.add_parser("models", help="聚合模型目录")
    p.set_defaults(fn=_cmd_models)

    p = sub.add_parser("chat", help="快速对话（走账号池）")
    p.add_argument("--model", default="deepseek-v4.1-flash")
    p.add_argument("prompt")
    p.set_defaults(fn=_cmd_chat)

    p = sub.add_parser("gateway", help="启动本机 OpenAI 兼容网关")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None,
                   help="缺省读 config web.port，再缺省 8178")
    p.add_argument("--api-key", default="")
    p.add_argument("--allow-insecure", action="store_true",
                   help="公网/局域网绑定且无 api_key 时强行启动（不推荐）")
    p.set_defaults(fn=_cmd_gateway)

    p = sub.add_parser("web", help="Web 交互界面 + 网关（同端口）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None,
                   help="缺省读 config web.port（网页可改），再缺省 8178")
    p.add_argument("--api-key", default="")
    p.add_argument("--allow-insecure", action="store_true",
                   help="公网/局域网绑定且无 api_key 时强行启动（不推荐）")
    p.set_defaults(fn=_cmd_web)

    p = sub.add_parser("switch", help="桌面客户端账号切换")
    p.add_argument("action", choices=["save", "list", "switch", "restore"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--region", choices=["cn", "global"], default="cn")
    p.set_defaults(fn=_cmd_switch)

    p = sub.add_parser("update", help="检查新版本（只提示链接，不自动升级）")
    p.set_defaults(fn=_cmd_update)

    p = sub.add_parser("config", help="查看/修改配置：list | get/set/unset <点分键> [值]")
    p.add_argument("action", choices=["list", "get", "set", "unset"])
    p.add_argument("key", nargs="?", default="",
                   help="点分路径，如 automation.notify_url、api_key")
    p.add_argument("value", nargs="?", default="",
                   help="值（set 用；自动识别数字/布尔/JSON）")
    p.set_defaults(fn=_cmd_config)

    return parser


def _cfg_dig(cfg: dict, path: str):
    """按点分路径取值，返回 (value, found)。"""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _cfg_parse_value(raw: str, key: str):
    """值类型推断：布尔/整数/浮点/JSON/字符串（api_key 等密钥键强制字符串）。"""
    if key.split(".")[-1] in ("api_key", "notify_url", "daily_at", "mode"):
        return raw
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith(("{", "[")):
        import json as _json
        try:
            return _json.loads(raw)
        except ValueError:
            pass
    return raw


def _cmd_config(args) -> int:
    """配置读写：wbspy config list / get api_key / set automation.notify_url <url> / unset …"""
    import json as _json
    cfg = load_config()
    if args.action == "list":
        print(_json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0
    key = args.key.strip()
    if not key:
        print("缺少键名，例：wbspy config get api_key", file=sys.stderr)
        return 1
    if args.action == "get":
        val, found = _cfg_dig(cfg, key)
        if not found:
            print(f"未设置：{key}（未配置时走代码内置默认）")
            return 1
        print(val if isinstance(val, str) else
              _json.dumps(val, ensure_ascii=False, indent=2))
        return 0
    if args.action == "unset":
        parent, ok = _cfg_dig(cfg, key.rsplit(".", 1)[0]) if "." in key else (cfg, True)
        leaf = key.rsplit(".", 1)[-1]
        if ok and isinstance(parent, dict) and leaf in parent:
            del parent[leaf]
            save_config(cfg)
            print(f"已清除 {key}")
            return 0
        print(f"{key} 本来就没设置", file=sys.stderr)
        return 1
    # set
    if args.value == "":
        print("缺少值：wbspy config set <键> <值>", file=_sys.stderr)
        return 1
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = _cfg_parse_value(args.value, key)
    save_config(cfg)
    val, _ = _cfg_dig(cfg, key)
    print(f"已保存 {key} = {_json.dumps(val, ensure_ascii=False)}")
    print("网页端 /api/* 的配置即时生效；网关密钥等启动期配置重启服务后生效。")
    return 0


def _cmd_update(_args) -> int:
    from .update import check_update
    print("正在检查新版本……")
    r = check_update()
    if not r["ok"]:
        print(f"✗ {r['error']}")
        print(f"  可手动打开发布页查看：{r['url']}")
        return 1
    if r["update_available"]:
        print(f"有新版本：v{r['current']} → v{r['latest']}")
        if r.get("notes"):
            print("\n更新说明（节选）：")
            for line in r["notes"].splitlines()[:8]:
                print(f"  {line}")
        print(f"\n发布页：{r['url']}")
        print("升级方式（按你的安装方式选其一）：")
        print("  git 安装： git pull && pip install -e .")
        print("  pip 安装： pip install -U https://github.com/yingfing/"
              "workbuddy-switch-python/archive/refs/heads/main.tar.gz")
        print("  Docker：  docker compose pull && docker compose up -d")
        print("升级后重启服务即可生效。")
    else:
        print(f"✓ 已是最新版本（v{r['current']}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK/cp936，中文输出会 UnicodeEncodeError；
    # 统一转 UTF-8（重定向到文件/管道时同样受益）。不支持的环境静默跳过。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n已退出。")
        return 130
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
