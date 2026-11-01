"""账号切换：保存 / 切换 / 恢复桌面客户端登录。

语义对齐原版（WorkBuddy-Switch auth.mjs 的 mergeAuth + controller.mjs）：

* **只处理明确的登录字段**：切换 = 把目标快照的 ``account`` 与 ``auth``
  字段合并进当前登录文件，机器本地元数据（设置、插件、工作区、机器
  标识等）全部保留；``accounts``/``allAccounts`` 列表按 uid 去重重建，
  避免官方客户端启动时恢复无关的 lastLogin 身份。
* 切换前先把当前登录备份到 ``~/.wbspy/last-switch-backup.json``，
  ``restore`` 可一键还原；本机加密凭据原样保存、不提取客户端内置密钥。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .document import Document, parse_auth
from .platform_paths import find_local_auth
from .store import BACKUP_PATH, ensure_dirs, load_snapshot, save_snapshot


def auth_file(region: str = "cn") -> Path:
    """桌面客户端登录文件（按当前平台解析，找不到才落回旧 home 路径以兼容旧快照）。"""
    channel = "workbuddy" if region == "global" else "buddy"
    try:
        return find_local_auth(channel)
    except FileNotFoundError:
        legacy = {
            "cn": Path.home() / ".workbuddy" / "workbuddy-desktop.info",
            "global": Path.home() / ".workbuddy-ai" / "workbuddy-desktop-ai.info",
        }
        return legacy.get(region, legacy["cn"])


def merge_auth(current_raw: str, target_raw: str) -> str:
    """保留 current 的机器本地字段，替换 account+auth，重建账号列表。"""
    target = parse_auth(target_raw)
    current = parse_auth(current_raw)
    root = {**current.raw, "account": target.raw["account"], "auth": target.raw["auth"]}
    merged: dict[str, list] = {}
    for field in ("accounts", "allAccounts"):
        mine, theirs = current.raw.get(field), target.raw.get(field)
        if not isinstance(mine, list) and not isinstance(theirs, list):
            continue
        seen: dict[str, dict] = {}
        for item in [*(mine or []), *(theirs or [])]:
            if isinstance(item, dict) and item.get("uid"):
                seen[str(item["uid"])] = item
        merged[field] = list(seen.values())
    root.update(merged)
    return json.dumps(root, ensure_ascii=False)


def save_current_account(name: str | None = None, region: str = "cn") -> Document:
    """把桌面客户端当前登录保存为快照。"""
    path = auth_file(region)
    if not path.exists():
        raise FileNotFoundError(f"未找到桌面客户端登录文件：{path}")
    raw = path.read_text(encoding="utf-8")
    doc = parse_auth(raw)
    label = name or f"{region}-{doc.uid[:12]}-{time.strftime('%Y%m%d-%H%M%S')}"
    save_snapshot(label, raw)
    print(f"✓ 已保存快照「{label}」：{doc.label()}")
    return doc


def switch_account(name: str, region: str = "cn") -> None:
    """切换桌面客户端登录到指定快照（先备份，可恢复）。"""
    path = auth_file(region)
    if not path.exists():
        raise FileNotFoundError(f"未找到当前登录文件：{path}（请先在官方客户端登录一次）")
    target_raw = load_snapshot(name)
    current_raw = path.read_text(encoding="utf-8")

    # 备份当前 → 恢复失败时可用 wbspy restore 找回
    ensure_dirs()
    BACKUP_PATH.write_text(json.dumps({"region": region, "at": int(time.time()),
                                       "raw": current_raw}, ensure_ascii=False),
                           encoding="utf-8")
    path.write_text(merge_auth(current_raw, target_raw), encoding="utf-8")
    target = parse_auth(target_raw)
    print(f"✓ 已切换到「{name}」：{target.label()}")
    print("  请重启官方客户端生效；切错了可执行 wbspy restore 还原。")


def restore_previous(region: str = "cn") -> None:
    """恢复切换前的登录（检测到切换后有新登录时拒绝，保护当前状态）。"""
    if not BACKUP_PATH.exists():
        raise FileNotFoundError("没有可恢复的切换前备份。")
    payload = json.loads(BACKUP_PATH.read_text(encoding="utf-8"))
    region = payload.get("region", region)
    path = auth_file(region)
    current = parse_auth(path.read_text(encoding="utf-8")) if path.exists() else None
    backup = parse_auth(payload["raw"])
    if current and current.uid != backup.uid:
        # 切换之后客户端又登录过别的账号——恢复会覆盖新登录，拒绝
        raise RuntimeError("检测到切换后产生了新登录，为保护当前状态已停止恢复。"
                           "如确认要覆盖，请先手动保存当前账号再重试。")
    path.write_text(payload["raw"], encoding="utf-8")
    BACKUP_PATH.unlink(missing_ok=True)
    print(f"✓ 已恢复切换前登录：{backup.label()}")
