"""命令行入口：python -m wbspy <command>"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .automation import loop, run_all
from .channels import CHANNELS, get_channel, methods_of
from .document import parse_auth
from .login import login
from .pool import AccountPool
from .store import (load_accounts, load_config, remove_account, save_account,
                    list_snapshots)
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
        try:
            data = WorkBuddyClient(doc).quota()
            print(f"· {doc.label()}")
            print(f"    {json.dumps(data, ensure_ascii=False)[:400]}")
        except Exception as exc:
            print(f"· {doc.label()}  积分查询失败：{exc}")
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


def _cmd_gateway(args) -> int:
    accounts = load_accounts()
    if not accounts:
        print("账号库为空，先 wbspy login。")
        return 1
    config = load_config()
    api_key = args.api_key or os.environ.get("WBSPY_API_KEY", "") \
        or config.get("api_key", "")
    from .pool import AccountPool as Pool
    from .gateway import run_gateway
    print(f"网关启动：http://{args.host}:{args.port}/v1  （账号 {len(accounts)} 个）")
    run_gateway(Pool(accounts), host=args.host, port=args.port, api_key=api_key)
    return 0


def _cmd_web(args) -> int:
    """Web 界面 + 网关同进程。"""
    config = load_config()
    api_key = args.api_key or os.environ.get("WBSPY_API_KEY", "") \
        or config.get("api_key", "")
    pool = AccountPool(load_accounts())
    from .gateway import create_app  # noqa: F401  确保依赖就绪
    from .webui import run_web
    print(f"Web 界面：http://{args.host}:{args.port}/   （界面 + 网关同端口）")
    if not pool.documents:
        print("提示：账号库为空，先在网页「登录」页添加账号。")
    run_web(pool, host=args.host, port=args.port, api_key=api_key)
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
    p.add_argument("--port", type=int, default=8178)
    p.add_argument("--api-key", default="")
    p.set_defaults(fn=_cmd_gateway)

    p = sub.add_parser("web", help="Web 交互界面 + 网关（同端口）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8178)
    p.add_argument("--api-key", default="")
    p.set_defaults(fn=_cmd_web)

    p = sub.add_parser("switch", help="桌面客户端账号切换")
    p.add_argument("action", choices=["save", "list", "switch", "restore"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--region", choices=["cn", "global"], default="cn")
    p.set_defaults(fn=_cmd_switch)

    p = sub.add_parser("update", help="检查新版本（只提示链接，不自动升级）")
    p.set_defaults(fn=_cmd_update)

    return parser


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
