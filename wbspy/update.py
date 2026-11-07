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

import re
import time
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


def _candidate_ips(path_probe: str = "") -> list[tuple[str, str]]:
    """按优先级产出 (ip, 说明) 候选，DoH 优先、内置表兜底。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(ips: list[str], label: str) -> None:
        for ip in ips:
            if ip and ip not in seen:
                seen.add(ip)
                out.append((ip, label))

    for server_ip, server_host in DOH_SERVERS:
        ips = _doh_resolve(server_ip, server_host, GITHUB_API_HOST)
        if ips:
            add(ips, f"DoH {server_host}")
            break                                      # 一个 DoH 成功即够
    add(list(FALLBACK_IPS), "内置优选")
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
