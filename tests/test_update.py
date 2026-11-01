"""更新检查模块：版本比较、失败降级、判定逻辑。"""

from wbspy import __version__, update


def test_vtuple_variants():
    assert update._vtuple("0.2.0") == (0, 2, 0)
    assert update._vtuple("v0.10.3") == (0, 10, 3)
    assert update._vtuple("0.3.0-rc1") == (0, 3, 0)
    assert update._vtuple("") == (0,)
    assert update._vtuple("1") > update._vtuple("0.9.9")


def test_check_update_no_newer_release(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"tag_name": "v0.2.0", "html_url": "https://x/r/1", "body": "n"}

    monkeypatch.setattr(update.requests, "get", lambda *a, **k: R())
    r = update.check_update()
    assert r["ok"] and not r["update_available"] and r["latest"] == "0.2.0"


def test_check_update_newer_release(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"tag_name": "v0.3.0", "html_url": "https://x/r/2",
                                "body": "- 修复若干问题"}

    monkeypatch.setattr(update.requests, "get", lambda *a, **k: R())
    r = update.check_update()
    assert r["ok"] and r["update_available"]
    assert "修复" in r["notes"] and r["url"] == "https://x/r/2"


def test_check_update_network_failure_degrades(monkeypatch):
    def boom(*a, **k): raise ConnectionError("timeout")
    monkeypatch.setattr(update.requests, "get", boom)
    r = update.check_update()
    assert not r["ok"] and r["current"] == __version__
    assert "无法连接" in r["error"] and r["url"]
