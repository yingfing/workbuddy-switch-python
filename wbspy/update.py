"""更新检查：三层检测，只提示并给出升级方式，绝不自作主张替用户升级。

    wbspy update            命令行检查
    GET /api/update/check   网页检查（用户点按钮时才请求，不自动轮询）

三层检测（v0.2.13 起）：

    1. Release 对比    本地版本号 vs GitHub 最新 Release——发版通道，
                       只有 CI 构建验证过的稳定包才算数；
    2. main 提交对比   git 源码安装时读本地 HEAD，与远端 main 最新提交比，
                       能发现「修了还没发版」的最新代码（提示 git pull）；
    3. 幽灵版本识别    本地版本号从未在 GitHub 打过 tag（如曾经的 0.2.4）时
                       明说，不再让「号小碰巧提示更新、号大永远已是最新」
                       全凭运气——版本号相同也不代表代码相同。

升级方式因安装方式而异（git pull / pip 装包 / docker compose pull），
工具不猜测环境、不执行替换自身——链接和命令都给你，你来决定。

网络策略（国内用户 api.github.com 常被 DNS 污染/间歇不可达）：

    1. 系统解析直连 api.github.com；
    2. 失败 → 阿里 DoH（223.5.5.5）查真实 IP；
    3. 再失败 → Google DoH（8.8.8.8）查真实 IP；
    4. 仍拿不到 → 内置官方 IP 兜底表；
    5. 每个 IP 直连尝试（TLS SNI 与证书校验保持 api.github.com，
       不降低安全性），逐个错开重试，绝不并发轰炸。
    探测成功的通道在同一次检查内复用（release/tags/commits 三个请求
    共用一条通道），不逐请求重探。
"""

from __future__ import annotations

import io
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import __version__

REPO = "yingfing/workbuddy-switch-python"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"
RELEASE_PATH = f"/repos/{REPO}/releases/latest"
TAGS_PATH = f"/repos/{REPO}/tags?per_page=100"
MAIN_PATH = f"/repos/{REPO}/commits/main"

GITHUB_API_HOST = "api.github.com"
# GitHub 官方地址段兜底表（会过期，但 DoH 通常已解决；api 子域常见为 140.82.1xx.6 / 20.205.243.16x）
FALLBACK_IPS = ["140.82.112.6", "140.82.113.6", "140.82.114.6",
                "20.205.243.168", "20.205.243.166"]
# DoH 解析服务：国内可达的放前面（(ip, sni/host)）
DOH_SERVERS = [("223.5.5.5", "dns.alidns.com"), ("8.8.8.8", "dns.google")]

STAGGER_SEC = 1.5          # 每个 IP 之间错开，避免连发
TIMEOUT = 6.0
DOWNLOAD_TIMEOUT = 120.0   # 源码包下载（体积大，放宽）

# 上次探测成功的优选 IP 缓存（只供本工具自用，不改系统 hosts）：
# 下次检查先试它，省掉直连超时 + DoH 的等待。探测到更优通道即更新。
BEST_IP_FILE = Path.home() / ".wbspy" / "github_best.json"


def _load_best_ip() -> dict:
    """读上次优选记录（ip/host/at）；损坏即视为无缓存。"""
    try:
        d = _json.loads(BEST_IP_FILE.read_text(encoding="utf-8"))
        if isinstance(d.get("ip"), str) and d["ip"]:
            return d
    except Exception:                              # noqa: BLE001
        pass
    return {}


def _save_best_ip(ip: str, host: str) -> None:
    """记录本次探测成功的通道（写盘失败不影响主流程）。"""
    try:
        BEST_IP_FILE.parent.mkdir(parents=True, exist_ok=True)
        BEST_IP_FILE.write_text(_json.dumps(
            {"ip": ip, "host": host, "at": time.strftime("%Y-%m-%d %H:%M:%S")}),
            encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass


def _vtuple(version: str) -> tuple[int, ...]:
    """'v0.2.0' / '0.3.1-rc1' → 可比较的数字元组（忽略非数字后缀）。"""
    parts = []
    for piece in re.sub(r"^v", "", version.strip()).split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


# -- 低层请求（IP 直连时保持 SNI 与证书校验）---------------------------------

def _pool_request(ip: str, host: str, path: str, timeout: float):
    """连 ip:443，但 TLS SNI / Host 头 / 证书校验都按 host 来。

    urllib3 ≥2.0 才支持 server_hostname 覆写；老版本调用方自行跳过。
    """
    from urllib3 import HTTPSConnectionPool
    pool = HTTPSConnectionPool(
        ip, 443, server_hostname=host, ca_certs=_ca_certs(),
        timeout=timeout, maxsize=1, retries=False)
    return pool.request("GET", path, headers={"Host": host},
                        preload_content=True)


def _probe_delay(ip: str, host: str) -> float | None:
    """探测候选 IP：发一次轻量 HTTPS 请求测往返延时（毫秒）。

    含 TCP + TLS 握手，与真实下载的建连成本一致；失败（超时/被重置）
    返回 None——探测失败的基本也下载不了，排到候选队尾垫底。"""
    t0 = time.monotonic()
    try:
        _pool_request(ip, host, "/", 4.0)
        return (time.monotonic() - t0) * 1000
    except Exception:                              # noqa: BLE001
        return None


def _ca_certs() -> str:
    try:
        import certifi
        return certifi.where()
    except ImportError:                                # pragma: no cover
        import ssl
        return ssl.get_default_verify_paths().cafile or ""


def _doh_resolve(server_ip: str, server_host: str, name: str,
                 timeout: float = TIMEOUT) -> list[str]:
    """DoH JSON API 查 A 记录，返回 IP 列表（可空）。"""
    try:
        resp = _pool_request(server_ip, server_host,
                             f"/resolve?name={name}&type=A", timeout)
        data = resp.json() if hasattr(resp, "json") else __import__("json").loads(resp.data)
        answers = data.get("Answer") or []
        return [a["data"] for a in answers if a.get("type") == 1 and a.get("data")]
    except Exception:                                  # noqa: BLE001
        return []


def _candidate_ips(path_probe: str = "", host: str = GITHUB_API_HOST,
                   verbose: bool = False) -> list[tuple[str, str]]:
    """按优先级产出 (ip, 说明) 候选：缓存 IP → DoH → 内置表兜底。

    verbose=True 时逐步打印获取过程（下载兜底场景：多等一秒也要让人
    知道在干嘛）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(ips: list[str], label: str) -> None:
        for ip in ips:
            if ip and ip not in seen:
                seen.add(ip)
                out.append((ip, label))

    best = _load_best_ip()
    if best.get("ip"):
        add([best["ip"]], "上次优选")
        if verbose:
            print(f"  · 上次优选缓存：{best['ip']}（优先参测）", flush=True)
    for server_ip, server_host in DOH_SERVERS:
        if verbose:
            print(f"  · 正在通过 DoH {server_host}（{server_ip}）解析 {host} ……", flush=True)
        ips = _doh_resolve(server_ip, server_host, host)
        if ips:
            add(ips, f"DoH {server_host}")
            if verbose:
                print(f"    ✓ 解析到 {len(ips)} 个 IP：{', '.join(ips[:4])}", flush=True)
            break                                      # 一个 DoH 成功即够
        if verbose:
            print("    ✗ 解析失败，换下一个 DoH 服务", flush=True)
    add(list(FALLBACK_IPS), "内置优选")
    if verbose:
        print(f"  · 内置优选表补充 {len(FALLBACK_IPS)} 个兜底 IP", flush=True)
    return out


# -- 网络通道（一次检查复用一条）----------------------------------------------

class _HttpFail(Exception):
    """单次请求失败（消息带通道标签，进探测日志）。"""


class _Channel:
    """一次更新检查共用一条网络通道。

    首个请求按优先级探测（直连 → DoH 解析 IP → 内置优选 IP），成功后
    记住通道，后续 tags/commits 请求直接复用，不再逐请求重探。
    通道建立后的请求失败不重试（通道刚验证过可用，多半是端点问题），
    由调用方按「尽力而为」降级。
    """

    def __init__(self) -> None:
        self._mode = ""          # ""=未探测 / "direct" / "ip"
        self._ip = ""
        self._errs: list[str] = []

    def get_json(self, path: str) -> dict:
        if self._mode == "direct":
            return self._direct(path)
        if self._mode == "ip":
            return self._via(self._ip, path, "复用通道")
        try:
            data = self._direct(path)
            self._mode = "direct"
            return data
        except _HttpFail as exc:
            self._errs.append(str(exc))
        for ip, label in _candidate_ips():
            time.sleep(STAGGER_SEC)                # 错开重试节奏
            try:
                data = self._via(ip, path, label)
                self._mode, self._ip = "ip", ip
                _save_best_ip(ip, GITHUB_API_HOST)   # 记下可用通道，下次免探测
                return data
            except _HttpFail as exc:
                self._errs.append(str(exc))
        raise RuntimeError("；".join(self._errs) or "无可用路径")

    def _direct(self, path: str) -> dict:
        try:
            resp = requests.get(f"https://{GITHUB_API_HOST}{path}",
                                timeout=TIMEOUT,
                                headers={"Accept": "application/vnd.github+json"})
        except Exception as exc:                   # noqa: BLE001
            raise _HttpFail(f"直连({exc.__class__.__name__})") from exc
        if resp.ok:
            return resp.json()
        raise _HttpFail(f"直连({resp.status_code})")

    def _via(self, ip: str, path: str, label: str) -> dict:
        try:
            resp = _pool_request(ip, GITHUB_API_HOST, path, TIMEOUT)
        except Exception as exc:                   # noqa: BLE001
            raise _HttpFail(f"{label}/{ip}({exc.__class__.__name__})") from exc
        if resp.status == 200:
            import json as _json
            return _json.loads(resp.data)
        raise _HttpFail(f"{label}/{ip}({resp.status})")


# -- 本地 git 状态 ------------------------------------------------------------

def _local_head(repo=None) -> str | None:
    """git 源码安装时的本地 HEAD 短 sha；pip 装包/打包产物返回 None。

    只读 .git 元数据文件，不执行 git 命令——部署环境可能没有 git。
    .git 是文件（worktree 指针）时同样按未知处理。
    """
    try:
        root = repo or Path(__file__).resolve().parents[1]
        gitdir = root / ".git"
        if not gitdir.is_dir():
            return None
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:7]                        # detached HEAD
        ref = head[5:].strip()
        ref_file = gitdir / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()[:7]
        packed = gitdir / "packed-refs"            # 浅克隆/打包可能只有 packed-refs
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:7]
    except Exception:                              # noqa: BLE001
        return None
    return None


# -- 三层检测 ------------------------------------------------------------------

def check_update(timeout: float = TIMEOUT) -> dict:
    """查询 GitHub 并做三层检测，返回结果字典。

    所有路径都不通时返回 ok=False 与原因——检查更新永远不该把人挡住。
    """
    ch = _Channel()
    try:
        data = ch.get_json(RELEASE_PATH)
    except RuntimeError as exc:
        return {"ok": False, "current": __version__,
                "error": f"GitHub 各通道均不可达（{exc}）。可稍后重试或手动打开发布页。",
                "url": RELEASE_PAGE}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "current": __version__,
                "error": f"无法检查更新（{exc.__class__.__name__}）。",
                "url": RELEASE_PAGE}

    latest = str(data.get("tag_name") or data.get("name") or "").lstrip("v")
    body = str(data.get("body") or "").strip()
    if not latest:
        return {"ok": False, "current": __version__,
                "error": "GitHub 未返回版本号。", "url": RELEASE_PAGE}

    out = {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "update_available": _vtuple(latest) > _vtuple(__version__),
        "notes": body[:400],
        "url": data.get("html_url") or RELEASE_PAGE,
        # 以下为 v0.2.13 三层检测扩展字段（旧前端自动忽略）
        "ghost": None,             # True=本地版本从未打过 tag；None=tags 不可达
        "local_commit": _local_head(),
        "main_commit": None,
        "main_sync": "unknown",    # same / ahead / unknown（非 git 安装恒 unknown）
    }
    try:   # 幽灵检测：本地版本号不在远端 tag 列表 = 从未正式发布
        tags = ch.get_json(TAGS_PATH)
        if isinstance(tags, list):
            names = {str(t.get("name", "")).lstrip("v") for t in tags}
            out["ghost"] = __version__ not in names
    except Exception:                              # noqa: BLE001
        pass
    try:   # main 最新提交对比（尽力而为，失败不影响主结果）
        info = ch.get_json(MAIN_PATH)
        main_sha = str(info.get("sha") or "")[:7]
        if main_sha:
            out["main_commit"] = main_sha
            local = out["local_commit"]
            if local:
                out["main_sync"] = "same" if main_sha == local else "ahead"
    except Exception:                              # noqa: BLE001
        pass
    return out


# -- 实际执行更新（--apply）----------------------------------------------------
# 设计原则：git pull 快速尝试（超时即弃，绝不吊死）；git 不通就复用本文件
# 的优选 IP 通道直接下载源码包替换——「git 优先，下载兜底」两条腿走路。

GIT_PULL_TIMEOUT = 60.0

ENV_REPO = "WBSPY_HOME"    # 安装目录环境变量：更新目标只认它（或 --repo），绝不自动猜


def _resolve_repo(explicit: Path | None = None) -> tuple[Path | None, str]:
    """更新目标目录：只信任显式指定（--repo 参数 / WBSPY_HOME 环境变量）。

    绝不自动猜测目录——猜错并覆盖了不相干的文件（甚至系统目录）就是事故。
    指定了也必须通过项目特征校验（有 .git 且有 wbspy/_version.py）才动手；
    未指定时返回 None 与一段设置指引。"""
    env_val = os.environ.get(ENV_REPO) or ""
    cand = explicit or (Path(env_val) if env_val.strip() else None)
    if cand is None:        # Path("") == Path(".")，空串必须拦截成「未指定」，
        return None, (      # 否则会把当前工作目录当候选（隐式猜测，绝不可）
            f"未指定项目目录：请先设置环境变量 {ENV_REPO} 指向 wbspy 安装目录，例如\n"
            f"  echo 'export {ENV_REPO}=/opt/wbspy' >> ~/.bashrc && source ~/.bashrc\n"
            f"（或运行时用 --repo 参数临时指定）。工具绝不自动猜测目录——"
            f"猜错并覆盖了别的文件就是事故。")
    cand = cand.expanduser().resolve()
    if not ((cand / ".git").exists() and (cand / "wbspy" / "_version.py").is_file()):
        return None, (
            f"{ENV_REPO}={cand} 不是 wbspy 项目目录（缺少 .git 或 wbspy/_version.py），"
            f"拒绝更新——请检查路径是否写错。")
    return cand, ""


def _git_pull(repo: Path, _retried: bool = False) -> tuple[bool, str]:
    """git pull --ff-only，超时即弃。返回（成功, 摘要）。

    本地工作区改动挡路（多为上次「下载兜底替换」绕过 git 产生的残留）时，
    自动丢弃工作区改动并重试一次——更新场景这些改动不可能是用户成果。"""
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"], cwd=repo, text=True,
            timeout=GIT_PULL_TIMEOUT, capture_output=True,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin",
                 "HOME": str(Path.home())})
    except FileNotFoundError:
        return False, "系统没有 git 命令"
    except subprocess.TimeoutExpired:
        return False, f"git pull 超过 {GIT_PULL_TIMEOUT:.0f}s（网络抖动），放弃改走下载"
    if r.returncode == 0:
        out = (r.stdout or "").strip().splitlines()
        return True, out[-1] if out else "已更新"
    err = (r.stderr or r.stdout or "").strip()
    if not _retried and ("overwritten" in err or "Aborting" in err):
        print("  · 本地工作区改动挡路（多为上次下载替换残留）→ 自动丢弃并重试", flush=True)
        subprocess.run(["git", "checkout", "--", "."], cwd=repo,
                       capture_output=True, timeout=30)
        subprocess.run(["git", "clean", "-fdq"], cwd=repo,
                       capture_output=True, timeout=30)
        return _git_pull(repo, _retried=True)
    lines = err.strip().splitlines()
    return False, (lines[-1] if lines else f"git 退出码 {r.returncode}")


def _download_tarball(ref: str, dest: Path) -> str:
    """经优选 IP 下载源码包（codeload.github.com）到 dest。返回通道说明。

    流程全程可见：获取可用 IP（DoH 逐步打印）→ 并发测延时（每个 IP 的
    毫秒数逐行打印）→ 按延时从低到高依次下载（探测失败的垫底再试）。
    之前的静默等待让用户以为卡死——每一步都要让人看见。"""
    host = "codeload.github.com"
    path = f"/{REPO}/tar.gz/{ref}"
    print("  正在获取可用 IP……", flush=True)
    candidates = _candidate_ips(host=host, verbose=True)
    if not candidates:
        raise RuntimeError("DoH 与内置表均未产出候选 IP，无可用下载通道")
    print(f"  共 {len(candidates)} 个候选，正在并发测试延时……", flush=True)
    delays: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as ex:
        futs = {ex.submit(_probe_delay, ip, host): (ip, label)
                for ip, label in candidates}
        for fut in as_completed(futs):
            ip, label = futs[fut]
            d = fut.result()
            delays[ip] = d
            print(f"    · {label} {ip}: "
                  + ("✗ 不可达" if d is None else f"{d:.0f} ms"), flush=True)
    alive = sorted((c for c in candidates if delays.get(c[0]) is not None),
                   key=lambda c: delays[c[0]])
    dead = [c for c in candidates if delays.get(c[0]) is None]
    if not alive:
        raise RuntimeError("；".join(f"{ip} 不可达" for ip, _ in dead)
                           or "无可用下载通道")
    print("  按延时排序下载（低的先试）："
          + " → ".join(ip for ip, _ in alive[:4])
          + (" …" if len(alive) > 4 else ""), flush=True)
    errs: list[str] = []
    queue = alive + dead                          # 探测不可达的垫底兜底
    for i, (ip, label) in enumerate(queue, 1):
        print(f"  · 下载中（{i}/{len(queue)}）{label} {ip} …", flush=True)
        try:
            resp = _pool_request(ip, host, path, DOWNLOAD_TIMEOUT)
        except Exception as exc:                   # noqa: BLE001
            errs.append(f"{label}/{ip}({exc.__class__.__name__})")
            print(f"    ✗ {exc.__class__.__name__}", flush=True)
            time.sleep(STAGGER_SEC)
            continue
        if resp.status == 200:
            dest.write_bytes(resp.data)
            _save_best_ip(ip, host)
            d = delays.get(ip)
            print(f"    ✓ 下载完成（{len(resp.data) / 1024:.0f} KB"
                  + (f"，该 IP 延时 {d:.0f} ms" if d is not None else "") + "）",
                  flush=True)
            return f"{label} {ip}"
        errs.append(f"{label}/{ip}(HTTP {resp.status})")
        print(f"    ✗ HTTP {resp.status}", flush=True)
        time.sleep(STAGGER_SEC)
    raise RuntimeError("；".join(errs) or "无可用下载通道")


def _apply_tarball(tar_path: Path, tag: str, repo: Path,
                   strict: bool = True, ref: str | None = None) -> None:
    """解开源码包并覆盖项目目录（保留 .git；校验版本一致才落盘）。

    strict=True（发版 tag 场景）：包内版本必须与 tag 一致，防串包；
    strict=False（main 未发版提交场景）：target 是 commit 号，拿它对
    版本号必失败——只要求包内版本是可识别的合法版本即可。替换成功后
    尽力 fetch + reset 对齐 git 历史（替换绕过了 git，不对齐的话下次
    pull 会被工作区改动 Aborting 挡死——实测踩过的坑）。"""
    expect = str(tag).lstrip("v")
    print(f"  · 解包并校验包内版本（期望 {expect}）……", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(tmp)                        # noqa: S202 源为官方 codeload
        root = next(Path(tmp).iterdir())              # archive 顶层目录 wbspy-main/
        ver_file = root / "wbspy" / "_version.py"
        got = ""
        if ver_file.is_file():
            m = re.search(r'__version__ *= *"([^"]+)"', ver_file.read_text("utf-8"))
            got = m.group(1) if m else ""
        if got != expect:
            if strict:
                raise RuntimeError(f"下载包版本 {got or '?'} 与发布版本 {expect} 不一致，放弃替换")
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][\w.]+)?", got or ""):
                raise RuntimeError(f"下载包版本 {got or '?'} 无法识别，放弃替换")
        print("  · 校验通过，替换项目文件（保留 .git）……", flush=True)
        shutil.copytree(root, repo, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git"))
        print("  ✓ 文件替换完成")
    _align_git(repo, ref)


def _align_git(repo: Path, ref: str | None) -> None:
    """下载替换绕过了 git：尽力 fetch + reset 把 HEAD 对齐到刚替换的内容，
    让 git 状态自洽（失败不影响本次使用，下次 pull 挡路会自动清理重试）。"""
    if not ref or not (repo / ".git").exists():
        return
    print("  · 对齐 git 历史到新版本……", flush=True)
    try:
        f = subprocess.run(["git", "fetch", "origin", ref], cwd=repo,
                           capture_output=True, text=True, timeout=90,
                           env={"GIT_TERMINAL_PROMPT": "0",
                                "PATH": "/usr/bin:/bin:/usr/local/bin"})
        if f.returncode == 0:
            subprocess.run(["git", "reset", "--hard", "-q", "FETCH_HEAD"],
                           cwd=repo, capture_output=True, timeout=30)
            print("    ✓ git HEAD 已对齐，之后 git pull 恢复正常")
            return
    except Exception:                              # noqa: BLE001
        pass
    print("    ⚠ fetch 未成（不影响本次使用）；下次 git pull 挡路会自动清理重试")


def _try_restart() -> bool:
    """尝试重启 wbspy-web 服务（仅 Linux 源码部署场景）。"""
    if sys.platform != "linux":
        print("  桌面版请手动重启 wbspy 程序生效。")
        return False
    print("  正在尝试重启 wbspy-web 服务……")
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", "wbspy-web"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print("  ✓ 服务已重启，稍后刷新网页即可。")
            return True
    except Exception:                              # noqa: BLE001
        pass
    print("  ⚠ 自动重启未成功（sudo 需要密码？），请手动执行："
          "cd /opt/wbspy && sudo systemctl restart wbspy-web")
    return False


def apply_update(assume_yes: bool = False, repo: Path | None = None) -> int:
    """检查更新 → 询问 → git pull（优先）→ 优选 IP 下载替换（兜底）→ 重启。

    返回退出码：0 已最新/已更新，1 检查失败或用户放弃，2 更新执行失败。"""
    print("正在检查新版本（探测 GitHub 可用通道，网络差时最长约半分钟）……", flush=True)
    r = check_update()
    if not r["ok"]:
        print(f"✗ {r['error']}")
        return 1
    need = r["update_available"] or r.get("main_sync") == "ahead"
    if not need:
        print(f"✓ 已是最新版本（v{r['current']}）")
        return 0
    # 目录先于询问解析：没设 WBSPY_HOME 时别问完一句才发现不能动手
    repo, err = _resolve_repo(repo)
    if repo is None:
        print(f"✗ {err}")
        return 1
    if r["update_available"]:
        print(f"有新版本：v{r['current']} → v{r['latest']}")
        ref = f"refs/tags/v{r['latest']}"
        target = r["latest"]
    else:
        print(f"⇣ main 分支已有未发版提交："
              f"{r.get('local_commit') or '?'} → {r.get('main_commit') or '?'}")
        ref = "refs/heads/main"
        target = r.get("main_commit") or "main"
    if r.get("notes") and r["update_available"]:
        print("\n更新说明（节选）：")
        for line in r["notes"].splitlines()[:6]:
            print(f"  {line}")
    if not assume_yes:
        try:
            ans = input("现在更新吗？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已放弃。之后可手动更新：git pull（或重跑 wbspy update --apply）")
            return 0
    print(f"项目目录：{repo}")
    # 第一条腿：git pull——60s 内无输出属正常（git 进度不透传，避免输出混乱）
    print(f"[1/4] git pull（最长等 {GIT_PULL_TIMEOUT:.0f}s，期间无输出属正常）……", flush=True)
    ok, msg = _git_pull(repo)
    if ok:
        print(f"✓ git pull 完成：{msg}")
        _try_restart()
        return 0
    print(f"⇣ git pull 未成：{msg}")
    # 第二条腿：优选 IP 下载源码包替换（.git 保留，git 历史不丢）
    print("[2/4] 改走优选 IP 直接下载源码包……", flush=True)
    print("[3/4] 校验并替换项目文件……", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "src.tar.gz"
        try:
            chan = _download_tarball(ref, tar_path)
            print(f"  ✓ 下载完成（通道：{chan}）")
            # 发版 tag 严格校验版本；main 未发版场景 target 是 commit 号，宽松校验
            _apply_tarball(tar_path, target, repo,
                           strict=bool(r["update_available"]), ref=ref)
        except RuntimeError as exc:
            print(f"✗ 下载兜底也失败：{exc}")
            print(f"  可手动打开发布页：{RELEASE_PAGE}")
            return 2
    print(f"[4/4] 更新到 {target} 完成（.git 保留，后续仍可用 git pull）")
    _try_restart()
    return 0
