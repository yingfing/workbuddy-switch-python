"""更新检查模块：版本比较、多通道择优（DoH/优选 IP）、失败降级。"""

from wbspy import __version__, update


def test_vtuple_variants():
    assert update._vtuple("0.2.0") == (0, 2, 0)
    assert update._vtuple("v0.10.3") == (0, 10, 3)
    assert update._vtuple("0.3.0-rc1") == (0, 3, 0)
    assert update._vtuple("") == (0,)
    assert update._vtuple("1") > update._vtuple("0.9.9")


class _FakeResp:
    def __init__(self, tag, ok=True, status=200):
        self._tag, self.ok, self.status = tag, ok, status
        import json as _json
        self.data = _json.dumps(
            {"tag_name": self._tag, "html_url": "https://x/r/1",
             "body": "- 修复若干问题"}).encode()

    def json(self):
        import json as _json
        return _json.loads(self.data)


def test_check_update_no_newer_release(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        lambda *a, **k: _FakeResp("v0.2.1"))
    r = update.check_update()
    assert r["ok"] and not r["update_available"] and r["latest"] == "0.2.1"


def test_check_update_newer_release(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        lambda *a, **k: _FakeResp("v0.3.0"))
    r = update.check_update()
    assert r["ok"] and r["update_available"]
    assert "修复" in r["notes"] and r["url"] == "https://x/r/1"


def test_direct_blocked_and_no_fallbacks(monkeypatch):
    """直连失败且无候选 IP（DoH 均不通、内置表置空）→ 优雅降级。"""
    def blocked(*a, **k):
        raise ConnectionError("DNS 污染")
    monkeypatch.setattr(update.requests, "get", blocked)
    monkeypatch.setattr(update, "FALLBACK_IPS", [])
    monkeypatch.setattr(update, "DOH_SERVERS", [])
    r = update.check_update()
    assert not r["ok"] and r["current"] == __version__
    assert "均不可达" in r["error"] and "直连" in r["error"] and r["url"]


def test_direct_blocked_ip_fallback_success(monkeypatch):
    """直连失败 → 候选 IP 池的第一个 IP 直连成功（打桩 _pool_request）。"""
    def blocked(*a, **k):
        raise ConnectionError("blocked")
    monkeypatch.setattr(update.requests, "get", blocked)
    monkeypatch.setattr(update, "_candidate_ips",
                        lambda *a: [("198.51.100.9", "测试通道")])
    monkeypatch.setattr(update, "STAGGER_SEC", 0)
    monkeypatch.setattr(update, "_pool_request",
                        lambda ip, host, path, t: _FakeResp("v9.9.9"))
    r = update.check_update()
    assert r["ok"] and r["latest"] == "9.9.9" and r["update_available"]


def test_doh_parse_and_stagger_in_candidates(monkeypatch):
    """DoH 返回的 IP 应进入候选列表且排在内置表之前。"""
    monkeypatch.setattr(update, "_doh_resolve",
                        lambda ip, host, name: ["203.0.113.7"])
    monkeypatch.setattr(update, "STAGGER_SEC", 0)
    cands = update._candidate_ips()
    assert cands[0] == ("203.0.113.7", "DoH dns.alidns.com")
    assert any(ip == "140.82.112.6" for ip, _ in cands)
