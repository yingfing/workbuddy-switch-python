"""用量账本：请求 / token / 换号救回 / 错误，按天持久化 + CSV 导出。

对应原版 WorkBuddy-Switch 的 usage.mjs / analytics.ts 的核心统计面。
线程安全；数据落 ~/.wbspy/usage.json（按天分桶，保留最近 90 天）。
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from pathlib import Path

USAGE_PATH = Path.home() / ".wbspy" / "usage.json"
RETENTION_DAYS = 90


class UsageLedger:
    def __init__(self, path: Path = USAGE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._today = time.strftime("%Y-%m-%d")
        self._data: dict = self._load()

    # -- 持久化 --------------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            import os
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _roll(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - RETENTION_DAYS * 86400))
            self._data = {k: v for k, v in self._data.items() if k >= cutoff}
        return self._data.setdefault(today, {
            "requests": 0, "errors": 0, "failover_saves": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "by_model": {}, "by_provider": {},
        })

    # -- 记录 ------------------------------------------------------------------

    def record(self, *, model: str = "", provider: str = "", account: str = "",
               prompt_tokens: int = 0, completion_tokens: int = 0,
               failover: bool = False, error: str = "") -> None:
        with self._lock:
            day = self._roll()
            day["requests"] += 1
            if error:
                day["errors"] += 1
            if failover:
                day["failover_saves"] += 1
            day["prompt_tokens"] += prompt_tokens
            day["completion_tokens"] += completion_tokens
            if model:
                slot = day["by_model"].setdefault(model, {"requests": 0, "tokens": 0})
                slot["requests"] += 1
                slot["tokens"] += prompt_tokens + completion_tokens
            if provider or account:
                slot = day["by_provider"].setdefault(provider or account,
                                                     {"requests": 0, "tokens": 0})
                slot["requests"] += 1
                slot["tokens"] += prompt_tokens + completion_tokens
            self._save()

    # -- 读取 / 导出 ------------------------------------------------------------

    def summary(self, days: int = 7) -> dict:
        with self._lock:
            keys = sorted(self._data.keys())[-days:]
            return {k: self._data[k] for k in keys}

    def to_csv(self, days: int = 90) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "requests", "errors", "failover_saves",
                         "prompt_tokens", "completion_tokens"])
        with self._lock:
            for key in sorted(self._data.keys())[-days:]:
                d = self._data[key]
                writer.writerow([key, d.get("requests", 0), d.get("errors", 0),
                                 d.get("failover_saves", 0), d.get("prompt_tokens", 0),
                                 d.get("completion_tokens", 0)])
        return buf.getvalue()


LEDGER = UsageLedger()


def estimate_tokens(messages: list) -> int:
    """粗估 prompt tokens（中英混合约 2.2 字符/ token）。

    tool_calls 的 arguments 必须计入：coding 会话里工具调用参数与
    工具结果常占大头（0.8MB 会话实测），漏算会让上下文预算误判
    「裁剪后已达标」，真实请求却超上游限制吃 11148。
    """
    total = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(p.get("text", "")) for p in content if isinstance(p, dict))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {} if isinstance(tc, dict) else {}
            total += len(str(fn.get("arguments") or "")) + len(str(fn.get("name") or ""))
    return int(total / 2.2)
