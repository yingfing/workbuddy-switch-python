"""账号库存储：~/.wbspy/ 下管理账号、快照与配置。

    ~/.wbspy/
      accounts/<sha256(scope|uid)>.json   账号库（scope=API origin，隔离各渠道 uid 命名空间）
      snapshots/<name>.json         桌面客户端登录快照（切换用）
      last-switch-backup.json       最近一次切换前的原登录（可恢复）
      config.json                   网关 key、自动化开关等
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .document import Document, parse_auth

ROOT = Path.home() / ".wbspy"
ACCOUNTS_DIR = ROOT / "accounts"
SNAPSHOTS_DIR = ROOT / "snapshots"
BACKUP_PATH = ROOT / "last-switch-backup.json"
CONFIG_PATH = ROOT / "config.json"


def ensure_dirs() -> None:
    for d in (ROOT, ACCOUNTS_DIR, SNAPSHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_names()


def account_key(doc: Document) -> str:
    """账号文件名键：sha256(scope|uid)。

    老版本（≤ v0.2.0）只哈希 uid，跨渠道 uid 撞车会互相覆盖；
    现在拼上 storage_scope（API origin）隔离各渠道命名空间。
    旧文件由 _migrate_legacy_names 在首次访问时自动改名迁移。
    """
    payload = f"{doc.storage_scope}|{doc.uid}".encode()
    return hashlib.sha256(payload).hexdigest()[:24]


def _read_account_file(path: Path) -> Document | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return parse_auth(payload["document"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
        return None


def _migrate_legacy_names() -> None:
    """老版本文件名 <sha256(uid)>.json → 新键 <sha256(scope|uid)>.json。

    幂等：名字已正确的文件直接跳过；目标已存在说明同账号重复，保留
    已迁移的那份，旧文件留在原地不删（宁可冗余不可丢凭据）。
    """
    if not ACCOUNTS_DIR.exists():
        return
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        doc = _read_account_file(path)
        if doc is None:
            continue
        new_key = account_key(doc)
        if path.stem == new_key:
            continue
        target = ACCOUNTS_DIR / f"{new_key}.json"
        if not target.exists():
            path.rename(target)


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", name.strip()).strip("-.")
    return cleaned[:80] or "snapshot"


# -- 账号库 ------------------------------------------------------------------


def save_account(doc: Document) -> Path:
    ensure_dirs()
    path = ACCOUNTS_DIR / f"{account_key(doc)}.json"
    payload = {"saved_at": int(time.time() * 1000), "document": doc.raw}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_accounts() -> list[Document]:
    out: list[Document] = []
    if not ACCOUNTS_DIR.exists():
        return out
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            out.append(parse_auth(payload["document"]))
        except (ValueError, KeyError, json.JSONDecodeError):
            continue  # 损坏条目跳过，不影响其余账号
    return out


def remove_account(uid: str, domain: str = "") -> bool:
    """按 uid（可选再按 domain 精确到单个账号）删除账号文件。

    兼容老接口：调用方只传 uid 时，删除所有 uid 匹配的账号
    （跨渠道 uid 撞车场景下建议前端带上 domain 精确删除）。
    """
    ensure_dirs()
    removed = False
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        doc = _read_account_file(path)
        if doc is None or doc.uid != uid:
            continue
        if domain and doc.domain != domain:
            continue
        path.unlink()
        removed = True
    return removed


# -- 快照（桌面客户端登录快照，切换用）---------------------------------------


def save_snapshot(name: str, raw_json: str) -> Path:
    ensure_dirs()
    path = SNAPSHOTS_DIR / f"{safe_name(name)}.json"
    path.write_text(raw_json, encoding="utf-8")
    return path


def list_snapshots() -> list[tuple[str, Document]]:
    out: list[tuple[str, Document]] = []
    if not SNAPSHOTS_DIR.exists():
        return out
    for path in sorted(SNAPSHOTS_DIR.glob("*.json")):
        try:
            out.append((path.stem, parse_auth(path.read_text(encoding="utf-8"))))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def load_snapshot(name: str) -> str:
    path = SNAPSHOTS_DIR / f"{safe_name(name)}.json"
    if not path.exists():
        raise FileNotFoundError(f"快照不存在：{name}")
    return path.read_text(encoding="utf-8")


# -- 配置 ---------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_config(config: dict[str, Any]) -> None:
    ensure_dirs()
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
