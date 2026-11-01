"""更新检查：只对比版本并给出升级链接，绝不自作主张替用户升级。

    wbspy update            命令行检查
    GET /api/update/check   网页检查（用户点按钮时才请求，不自动轮询）

升级方式因安装方式而异（git pull / pip 装包 / docker compose pull），
工具不猜测环境、不执行替换自身——链接和命令都给你，你来决定。

网络策略（国内用户 api.github.com 常被 DNS 污染/间歇不可达）：

    1. 系统解析直连 api.github.com；
    2. 失败 → 阿里 DoH（223.5.5.5）查真实 IP；
    3. 再失败 → Google DoH（8.8.8.8）查真实 IP；
    4. 仍拿不到 → 内置官方 IP 兜底表；
    5. 每个 IP 直连尝试（TLS SNI 与证书校验保持 api.github.com，
       不降低安全性），逐个错开重试，绝不并发轰炸。
"""

from __future__ import annotations

import re
import time

import requests

from . import __version__

REPO = "yingfing/workbuddy-switch-python"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"

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


# -- 拉取 Release ------------------------------------------------------------

def _fetch_release() -> dict:
    """依次尝试：直连 → 各候选 IP 错开直连。返回 GitHub JSON 或抛 RuntimeError。"""
    attempts: list[str] = []
    try:
        resp = requests.get(RELEASE_API, timeout=TIMEOUT,
                            headers={"Accept": "application/vnd.github+json"})
        if resp.ok:
            return resp.json()
        attempts.append(f"直连({resp.status_code})")
    except Exception as exc:                           # noqa: BLE001
        attempts.append(f"直连({exc.__class__.__name__})")

    for ip, label in _candidate_ips():
        time.sleep(STAGGER_SEC)                        # 错开重试节奏
        try:
            resp = _pool_request(ip, GITHUB_API_HOST,
                                 "/repos/yingfing/workbuddy-switch-python/releases/latest",
                                 TIMEOUT)
            if resp.status == 200:
                import json as _json
                return _json.loads(resp.data)
            attempts.append(f"{label}/{ip}({resp.status})")
        except Exception as exc:                       # noqa: BLE001
            attempts.append(f"{label}/{ip}({exc.__class__.__name__})")

    raise RuntimeError("；".join(attempts) if attempts else "无可用路径")


def check_update(timeout: float = TIMEOUT) -> dict:
    """查询 GitHub 最新 Release，与本地版本对比。

    所有路径都不通时返回 ok=False 与原因——检查更新永远不该把人挡住。
    """
    try:
        data = _fetch_release()
    except RuntimeError as exc:
        return {"ok": False, "current": __version__,
                "error": f"GitHub 各通道均不可达（{exc}）。可稍后重试或手动打开发布页。",
                "url": RELEASE_PAGE}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "current": __version__,
                "error": f"无法检查更新（{exc.__class__.__name__}）。",
                "url": RELEASE_PAGE}

    latest = str(data.get("tag_name") or data.get("name") or "").lstrip("v")
    body = str(data.get("body") or "").strip()
    if not latest:
        return {"ok": False, "current": __version__,
                "error": "GitHub 未返回版本号。", "url": RELEASE_PAGE}
    return {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "update_available": _vtuple(latest) > _vtuple(__version__),
        "notes": body[:400],
        "url": data.get("html_url") or RELEASE_PAGE,
    }
