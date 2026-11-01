"""账号重名防护：scope+uid 复合键、老版本自动迁移、账号池隔离。"""

import hashlib
import json

import pytest

from wbspy import pool as pool_mod
from wbspy.document import Document, parse_auth
from wbspy import store


def make_doc(uid: str, domain: str, token: str = "tk") -> Document:
    return parse_auth({
        "account": {"uid": uid, "nickname": "测试号"},
        "auth": {"accessToken": token, "refreshToken": "rf", "domain": domain},
    })


@pytest.fixture()
def tmp_store(monkeypatch, tmp_path):
    root = tmp_path / ".wbspy"
    monkeypatch.setattr(store, "ROOT", root)
    monkeypatch.setattr(store, "ACCOUNTS_DIR", root / "accounts")
    monkeypatch.setattr(store, "SNAPSHOTS_DIR", root / "snapshots")
    monkeypatch.setattr(store, "CONFIG_PATH", root / "config.json")
    monkeypatch.setattr(store, "BACKUP_PATH", root / "last-switch-backup.json")
    return root


def test_same_uid_different_channels_coexist(tmp_store):
    """TRAE 的 "1" 和 Qoder 的 "1" 是两个人：文件必须分开。"""
    store.save_account(make_doc("12345", "api.minimax.cn"))
    store.save_account(make_doc("12345", "api.cline.bot"))
    docs = store.load_accounts()
    assert len(docs) == 2
    assert {d.domain for d in docs} == {"api.minimax.cn", "api.cline.bot"}


def test_same_account_resave_overwrites(tmp_store):
    """同一账号重新登录：覆盖更新，不产生重复条目。"""
    store.save_account(make_doc("u1", "www.codebuddy.cn", token="old"))
    store.save_account(make_doc("u1", "www.codebuddy.cn", token="new"))
    docs = store.load_accounts()
    assert len(docs) == 1 and docs[0].token == "new"


def test_legacy_filename_migrated(tmp_store):
    """老版本 <sha256(uid)>.json 文件自动改名到新键。"""
    doc = make_doc("legacy", "api.minimax.cn")
    old_name = hashlib.sha256("legacy".encode()).hexdigest()[:24]
    legacy_path = store.ACCOUNTS_DIR if store.ACCOUNTS_DIR.exists() else None
    store.ensure_dirs()
    (store.ACCOUNTS_DIR / f"{old_name}.json").write_text(
        json.dumps({"saved_at": 0, "document": doc.raw}), encoding="utf-8")
    store.ensure_dirs()                      # 触发迁移
    new_name = store.account_key(doc)
    assert (store.ACCOUNTS_DIR / f"{new_name}.json").exists()
    assert not (store.ACCOUNTS_DIR / f"{old_name}.json").exists()
    assert store.load_accounts()[0].uid == "legacy"


def test_migrate_idempotent_and_collision_safe(tmp_store):
    """迁移幂等；目标已存在时不覆盖已迁移文件。"""
    doc = make_doc("m1", "api.minimax.cn")
    store.ensure_dirs()
    old_name = hashlib.sha256("m1".encode()).hexdigest()[:24]
    legacy = store.ACCOUNTS_DIR / f"{old_name}.json"
    legacy.write_text(json.dumps({"document": doc.raw}), encoding="utf-8")
    store.ensure_dirs()
    new_path = store.ACCOUNTS_DIR / f"{store.account_key(doc)}.json"
    assert new_path.exists()
    new_path.write_text("sentinel", encoding="utf-8")
    # 再放一个同账号旧文件，迁移不得覆盖已存在的 sentinel
    legacy.write_text(json.dumps({"document": doc.raw}), encoding="utf-8")
    store.ensure_dirs()
    assert new_path.read_text(encoding="utf-8") == "sentinel"


def test_remove_by_uid_and_domain(tmp_store):
    store.save_account(make_doc("9", "api.minimax.cn"))
    store.save_account(make_doc("9", "api.cline.bot"))
    # 带 domain：只删一个
    assert store.remove_account("9", domain="api.minimax.cn")
    assert len(store.load_accounts()) == 1
    # 不带 domain：删所有 uid 匹配
    assert store.remove_account("9")
    assert store.load_accounts() == []


def test_pool_keys_isolated(tmp_store):
    """账号池字典/冷却表按 (scope, uid) 隔离，跨渠道同 uid 不互相覆盖。"""
    d1 = make_doc("777", "api.minimax.cn", token="a")
    d2 = make_doc("777", "api.cline.bot", token="b")
    p = pool_mod.AccountPool([d1, d2])
    assert len(p.documents) == 2
    assert len(p.clients) == 2
    p.apply_error(d1, pool_mod.UpstreamError(429, "upstream_rate_limit", "rate limited"), "m1")
    assert p.cooldowns.get(p_mod_key(d1), "m1") > 0
    assert p.cooldowns.get(p_mod_key(d2), "m1") == 0.0


def p_mod_key(doc):
    return (doc.storage_scope, doc.uid)
