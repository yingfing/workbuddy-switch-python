"""账号库存储：~/.wbspy/ 下管理账号、快照与配置。

    ~/.wbspy/
      accounts/<sha256(uid)>.json   账号库（网页授权与本机读取的凭据文档）
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


def account_key(uid: str) -> str:
    return hashlib.sha256(uid.encode()).hexdigest()[:24]


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", name.strip()).strip("-.")
    return cleaned[:80] or "snapshot"


# -- 账号库 ------------------------------------------------------------------


def save_account(doc: Document) -> Path:
    ensure_dirs()
    path = ACCOUNTS_DIR / f"{account_key(doc.uid)}.json"
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


def remove_account(uid: str) -> bool:
    path = ACCOUNTS_DIR / f"{account_key(uid)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


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
