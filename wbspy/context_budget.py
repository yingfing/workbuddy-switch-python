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


def _drop_oldest(kept: list) -> bool:
    """从最旧删一条非 system、非末条消息；tool 链成对删（保配对语义）。

    assistant(tool_calls) 与其后的 tool 结果必须同生共死——只删一半
    上游直接 400。返回是否删了东西。
    """
    for i in range(len(kept)):
        m = kept[i]
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        if i == len(kept) - 1:
            return False                      # 末条永不裁（对话必须有收尾）
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            j = i + 1                         # 连带删紧随的 tool 结果（可多条）
            while j < len(kept) and isinstance(kept[j], dict) \
                    and kept[j].get("role") == "tool":
                j += 1
            del kept[i:j]
            return True
        del kept[i]                           # user / assistant / 孤儿 tool
        return True
    return False


def trim(payload: dict) -> tuple[dict, int]:
    """返回 (新 payload, 被裁掉的消息数)。窗口够时不裁剪。

    预算判定必须含 tool_calls 体积（estimate_tokens 已计）——0.8MB/191
    工具的 coding 会话里，尾部巨型工具结果 + 工具 schema 才是真实大头。
    保护集只有 system 全部 + 末条：末两条保护曾让巨型 tool 对裁不动，
    照发上游吃 11148。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 3:
        return payload, 0
    budget = int(window_of(str(payload.get("model", ""))) * SAFETY)
    if estimate_tokens(messages) <= budget:
        return payload, 0

    kept = list(messages)
    trimmed = 0
    while estimate_tokens(kept) > budget and _drop_oldest(kept):
        trimmed += 1
    return {**payload, "messages": kept}, trimmed
