"""更新检查：只对比版本并给出升级链接，绝不自作主张替用户升级。

    wbspy update            命令行检查
    GET /api/update/check   网页检查（用户点按钮时才请求，不自动轮询）

升级方式因安装方式而异（git pull / pip 装包 / docker compose pull），
工具不猜测环境、不执行替换自身——链接和命令都给你，你来决定。
"""

from __future__ import annotations

import re

import requests

from . import __version__

REPO = "yingfing/workbuddy-switch-python"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"


def _vtuple(version: str) -> tuple[int, ...]:
    """'v0.2.0' / '0.3.1-rc1' → 可比较的数字元组（忽略非数字后缀）。"""
    parts = []
    for piece in re.sub(r"^v", "", version.strip()).split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def check_update(timeout: float = 6.0) -> dict:
    """查询 GitHub 最新 Release，与本地版本对比。

    网络不通、接口限流等情况不抛异常——返回 ok=False 与原因，
    检查更新永远不该把人挡住。
    """
    try:
        resp = requests.get(RELEASE_API, timeout=timeout,
                            headers={"Accept": "application/vnd.github+json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "current": __version__,
                "error": f"无法连接 GitHub 检查更新（{exc.__class__.__name__}）。",
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
