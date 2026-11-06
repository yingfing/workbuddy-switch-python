"""76 家厂商注册表：渠道路由的模型/凭据元数据（数据源自 dsh-xdpool）。

这里只保留 ``load_providers``——aggregate（渠道路由）与 context_budget
（模型上下文预算）依赖它。原「免费可调用 API」展示页已按需求移除，
注册表本身仍是聚合渠道接入的底座。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "providers-registry.json"


@lru_cache(maxsize=1)
def load_providers() -> list[dict]:
    with open(DATA_PATH, encoding="utf-8") as fh:
        return json.load(fh)
