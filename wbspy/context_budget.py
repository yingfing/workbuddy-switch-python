"""上下文预算：超长会话在本地裁剪，不撞上游上下文墙。

对应 dsh-xdpool context-budget.ts 的本地思路（简化版）：

* token 估算用 2.2 字符/token（中英混合经验值）；
* 窗口取注册表 contextWindow，未知模型默认 128k；
* 裁剪策略：保住 system 全部 + 最后两条，从最旧的非保护消息开始丢，
  直到估算值落到窗口的 92% 以下；
* 裁剪动作在摘要里可见（trimmed: N），方便前端提示「较早上下文已被截断」。
"""

from __future__ import annotations

from .providers import load_providers
from .usage import estimate_tokens

DEFAULT_WINDOW = 128_000
SAFETY = 0.92

_WINDOWS: dict[str, int] | None = None


def _windows() -> dict[str, int]:
    global _WINDOWS
    if _WINDOWS is None:
        _WINDOWS = {}
        for p in load_providers():
            for m in p.get("models") or []:
                cw = m.get("contextWindow")
                if cw:
                    _WINDOWS[m["id"]] = int(cw)
    return _WINDOWS


def window_of(model: str) -> int:
    return _windows().get(model, DEFAULT_WINDOW)


def trim(payload: dict) -> tuple[dict, int]:
    """返回 (新 payload, 被裁掉的消息数)。窗口够时不裁剪。"""
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 3:
        return payload, 0
    budget = int(window_of(str(payload.get("model", ""))) * SAFETY)
    if estimate_tokens(messages) <= budget:
        return payload, 0

    system_idx = {i for i, m in enumerate(messages)
                  if isinstance(m, dict) and m.get("role") == "system"}
    protected = system_idx | {len(messages) - 1, len(messages) - 2}
    kept = list(messages)
    trimmed = 0
    while estimate_tokens(kept) > budget and len(kept) > len(protected):
        drop = next((i for i in range(len(kept)) if i not in protected), None)
        if drop is None:
            break
        del kept[drop]
        trimmed += 1
        protected = system_idx | {len(kept) - 1, len(kept) - 2}
    return {**payload, "messages": kept}, trimmed
