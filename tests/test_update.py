"""更新检查模块：三层检测（Release / main 提交 / 幽灵版本）、通道复用、失败降级。"""

import re
from pathlib import Path

from wbspy import __version__, update


def test_vtuple_variants():
    assert update._vtuple("0.2.0") == (0, 2, 0)
    assert update._vtuple("v0.10.3") == (0, 10, 3)
    assert update._vtuple("0.3.0-rc1") == (0, 3, 0)
    assert update._vtuple("") == (0,)
    assert update._vtuple("1") > update._vtuple("0.9.9")


class _FakeResp:
    """str → 当作 release tag 构造响应；dict/list → 直接作为 JSON 响应。"""

    def __init__(self, payload, ok=True, status=200):
        self.ok, self.status = ok, status
        if isinstance(payload, str):
            payload = {"tag_name": payload, "html_url": "https://x/r/1",
                       "body": "- 修复若干问题"}
        self._payload = payload
        import json as _json
        self.data = _json.dumps(payload).encode()

    def json(self):
        return self._payload


CUR_TAG = "v" + __version__          # 动态跟随当前版本，发版后 mock 仍自洽


def _fake_github(tag=None, tags=None, main_sha=None, fail=()):
    tag = tag or CUR_TAG
    """按请求路径打桩 releases/latest / tags / commits/main 三端点。"""
    tags = tags if tags is not None else [tag]

    def fake_get(url, **k):
        path = url.split("api.github.com")[-1]
        if "releases/latest" in path:
            if "release" in fail:
                raise ConnectionError("refused")
            return _FakeResp(tag)
        if "/tags" in path:
            if "tags" in fail:
                raise ConnectionError("refused")
            return _FakeResp([{"name": t} for t in tags])
        if "/commits/main" in path:
            if "commits" in fail:
                raise ConnectionError("refused")
            return _FakeResp({"sha": main_sha} if main_sha else {"sha": ""})
        raise AssertionError("意外端点 " + url)

    return fake_get


# -- 基础：Release 对比与网络降级 --------------------------------------------

def test_check_update_no_newer_release(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tag="v0.2.1"))
    r = update.check_update()
    assert r["ok"] and not r["update_available"] and r["latest"] == "0.2.1"


def test_check_update_newer_release(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tag="v0.3.0"))
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


# -- 三层检测：幽灵版本与 main 提交 ------------------------------------------

def test_ghost_version_flagged(monkeypatch):
    """本地版本号不在远端 tags → ghost=True（0.2.4 幽灵版本正是此场景）。"""
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tag="v0.2.12", tags=["v0.2.12", "v0.2.11"]))
    r = update.check_update()
    assert r["ok"] and r["ghost"] is True


def test_published_version_not_ghost(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tags=[CUR_TAG, "v0.2.1"]))
    r = update.check_update()
    assert r["ok"] and r["ghost"] is False


def test_aux_endpoints_fail_gracefully(monkeypatch):
    """tags/commits 不可达 → ghost/main_sync 降级为未知，主结果不受影响。"""
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(fail=("tags", "commits")))
    r = update.check_update()
    assert r["ok"] and r["ghost"] is None and r["main_sync"] == "unknown"


def test_main_ahead_detected(monkeypatch):
    """git 安装且本地 HEAD ≠ 远端 main → main_sync='ahead'（有未发版修复）。"""
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tags=[CUR_TAG],
                                     main_sha="abcdef1234567890"))
    monkeypatch.setattr(update, "_local_head", lambda *a, **k: "1110000")
    r = update.check_update()
    assert r["main_sync"] == "ahead" and r["main_commit"] == "abcdef1"
    assert r["local_commit"] == "1110000"


def test_main_same_when_synced(monkeypatch):
    monkeypatch.setattr(update.requests, "get",
                        _fake_github(tags=[CUR_TAG],
                                     main_sha="abcdef1"))
    monkeypatch.setattr(update, "_local_head", lambda *a, **k: "abcdef1")
    r = update.check_update()
    assert r["main_sync"] == "same" and r["ghost"] is False


def test_channel_reused_for_aux_requests(monkeypatch):
    """release 探测成功后，tags/commits 复用同一通道，不再重复探测。"""
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        if "/tags" in url:
            return _FakeResp([{"name": CUR_TAG}])
        if "/commits/main" in url:
            return _FakeResp({"sha": "aabbccddeeff"})
        return _FakeResp(CUR_TAG)

    def no_probe(*a, **k):
        raise AssertionError("通道已建立后不应再探测候选 IP")

    monkeypatch.setattr(update.requests, "get", fake_get)
    monkeypatch.setattr(update, "_candidate_ips", no_probe)
    r = update.check_update()
    assert r["ok"] and len(calls) == 3
    assert r["ghost"] is False and r["main_commit"] == "aabbccd"


# -- 本地 HEAD 读取（只读 .git 文件，不执行 git 命令）------------------------

def test_local_head_reads_git_files(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(
        "0123456789abcdef0123456789abcdef01234567\n")
    assert update._local_head(tmp_path) == "0123456"


def test_local_head_packed_refs_fallback(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted \n"
        "fedcba9876543210fedcba9876543210fedcba98 refs/heads/main\n")
    assert update._local_head(tmp_path) == "fedcba9"


def test_local_head_detached(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("0123456789abcdef\n")
    assert update._local_head(tmp_path) == "0123456"


def test_local_head_none_without_git(tmp_path):
    assert update._local_head(tmp_path) is None


# -- 版本号单一事实源守卫 ------------------------------------------------------

def test_version_single_source():
    """__version__ 必须转发 _version.py（教训：双处硬编码，升版只改一半，
    0.2.5~0.2.12 运行时始终自报 0.2.4）。"""
    import wbspy
    src = (Path(wbspy.__file__).parent / "_version.py").read_text()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert m and m.group(1) == wbspy.__version__


def test_pyproject_reads_dynamic_version():
    """pyproject.toml 不得再硬编码 version 行（单一事实源守卫）。"""
    root = Path(update.__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in text
    assert not re.search(r'^version\s*=\s*"', text, re.M)
