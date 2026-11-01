"""免费可调用 API 注册表：76 家厂商的免费额度与模型目录。

数据提炼自 dsh-xdpool 的 providers-registry.json（吸收其「聚合渠道」页），
原表按 category 分五类：

    apikey     60 家 — 填 API Key 即接入（多数有免费层）
    free-tier  10 家 — 有免费额度的托管服务
    local       3 家 — 本地推理（Ollama 等）
    oauth       2 家 — 需网页授权
    free        1 家 — 完全免费通道

仅做展示与导航（谁有免费额度、去哪儿申请、模型有哪些）；接入调用
仍走各自 provider 的网关配置，本版不代填密钥。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "providers-registry.json"

CATEGORY_LABELS = {
    "apikey": "API Key",
    "free-tier": "有免费额度",
    "local": "本地推理",
    "oauth": "需授权",
    "free": "免费",
}


@lru_cache(maxsize=1)
def load_providers() -> list[dict]:
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def provider_rows() -> list[dict]:
    """网页展示用精简行：厂商、分类、接入方式、模型数、官网。"""
    rows = []
    for p in load_providers():
        auth = p.get("auth") or {}
        rows.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "category": p.get("category"),
            "category_label": CATEGORY_LABELS.get(p.get("category"), p.get("category")),
            "auth_kind": auth.get("kind") or ("local" if p.get("local") else "apiKey"),
            "signup_url": auth.get("url") or p.get("website"),
            "base_url": p.get("baseUrl"),
            "model_count": len(p.get("models") or []),
            "models": [m.get("id") for m in (p.get("models") or [])[:12]],
            "notes": (p.get("notes") or "")[:160],
        })
    return rows


def category_stats() -> dict:
    from collections import Counter
    counter = Counter(p.get("category") for p in load_providers())
    return {CATEGORY_LABELS.get(k, k): v for k, v in counter.items()}


def free_model_index() -> list[dict]:
    """全部厂商的免费/免费额度模型平铺索引（供「免费可调用 API」视图）。"""
    out = []
    for p in load_providers():
        if p.get("category") not in ("free", "free-tier"):
            continue
        for m in p.get("models") or []:
            out.append({
                "provider": p.get("name"),
                "provider_id": p.get("id"),
                "model": m.get("id"),
                "model_name": m.get("name"),
                "category": p.get("category"),
                "signup_url": (p.get("auth") or {}).get("url") or p.get("website"),
            })
    return out
