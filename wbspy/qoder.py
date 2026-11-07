"""Qoder 渠道：静态模型目录 + 未接入推理的诚实报错。

**模型目录**：Qoder 无公开目录端点，官方客户端同样使用内置静态表。
本表逐条搬运 dsh-xdpool vendor/channel-pack 的 QODER_FALLBACK_MODELS
（国际版 17 条 / 国内版 14 条，2026-09 校准），``cm`` 即官方
priceFactor（计费倍率，0 = 免费模型），``cw`` 为上下文窗口（官方
档位表最大档）。

**对话推理**：走官方 wasm 加密（qoder-auth-wasm 生成
``Bearer COSY.<载荷>.<签名>``，端点 api2.qoder.sh /
gateway.qoder.com.cn），本版尚未接入——chat 一律返回明确的 501，
绝不把请求误投 WorkBuddy 端点。
"""

from __future__ import annotations

_M = 1_000_000

CATALOGS: dict[str, list[dict]] = {
    "qoder": [                                   # 国际版（17 条）
        {"id": "auto", "name": "Auto", "cw": 200_000, "cm": 0.5},
        {"id": "ultimate", "name": "Ultimate", "cw": _M, "cm": 2.0},
        {"id": "performance", "name": "Performance", "cw": _M, "cm": 1.1},
        {"id": "efficient", "name": "Efficient", "cw": _M, "cm": 0.3},
        {"id": "smodel", "name": "Sonus", "cw": _M, "cm": 8.0},
        {"id": "cmodel", "name": "Cantus", "cw": _M, "cm": 4.0},
        {"id": "qmodel_38max", "name": "Qwen3.8-Max", "cw": _M, "cm": 0.2},
        {"id": "qfmodel", "name": "Qwen3.8-Flash", "cw": _M, "cm": 0.0},
        {"id": "qmodel_latest", "name": "Qwen3.7-Max", "cw": _M, "cm": 0.1},
        {"id": "qmodel", "name": "Qwen3.7-Plus", "cw": _M, "cm": 0.04},
        {"id": "kmodel_latest", "name": "Kimi-K3", "cw": _M, "cm": 1.4},
        {"id": "kmodel", "name": "Kimi-K2.8-Preview", "cw": _M, "cm": 0.8},
        {"id": "gmodel", "name": "GLM-5.3", "cw": _M, "cm": 0.8},
        {"id": "gfmodel", "name": "GLM-5.3-Flash", "cw": _M, "cm": 0.1},
        {"id": "dmodel", "name": "DeepSeek-V4-Pro", "cw": _M, "cm": 0.5},
        {"id": "dfmodel", "name": "DeepSeek-Flash", "cw": _M, "cm": 0.1},
        {"id": "mmodel", "name": "MiniMax-M3", "cw": _M, "cm": 0.2},
    ],
    "qodercn": [                                 # 国内版（14 条）
        {"id": "auto", "name": "Auto", "cw": 200_000, "cm": 0.5},
        {"id": "qmodel_38max", "name": "Qwen3.8-Max", "cw": _M, "cm": 0.2},
        {"id": "qfmodel", "name": "Qwen3.8-Flash", "cw": _M, "cm": 0.0},
        {"id": "qmodel_latest", "name": "Qwen3.7-Max", "cw": _M, "cm": 0.1},
        {"id": "qmodel", "name": "Qwen3.7-Plus", "cw": _M, "cm": 0.04},
        {"id": "q37fmodel", "name": "Qwen3.7-Flash", "cw": _M, "cm": 0.1},
        {"id": "dmodel", "name": "DeepSeek-V4-Pro", "cw": _M, "cm": 0.5},
        {"id": "dfmodel", "name": "DeepSeek-Flash", "cw": _M, "cm": 0.1},
        {"id": "gmodel", "name": "GLM-5.3", "cw": _M, "cm": 0.8},
        {"id": "gfmodel", "name": "GLM-5.3-Flash", "cw": _M, "cm": 0.1},
        {"id": "gm51model", "name": "GLM-5.2", "cw": _M, "cm": 0.6},
        {"id": "kmodel_latest", "name": "Kimi-K3", "cw": _M, "cm": 1.4},
        {"id": "kmodel", "name": "Kimi-K2.8-Preview", "cw": _M, "cm": 0.8},
        {"id": "mmodel", "name": "MiniMax-M2.7", "cw": 200_000, "cm": 0.2},
    ],
}


class QoderClient:
    """Qoder 泳道客户端：目录查静态表；推理未接入，报错明确不含糊。"""

    def __init__(self, doc):
        self.doc = doc

    def models(self) -> list[dict]:
        rows = CATALOGS.get(self.doc.channel_id) or CATALOGS["qoder"]
        return [{"id": r["id"], "name": r["name"],
                 "credit_multiplier": r["cm"], "context_window": r["cw"]}
                for r in rows]

    def chat(self, payload: dict, **_kw):
        raise _unsupported()

    def refresh(self):
        # 渠道未声明 refresh 能力，续期 worker 不会调；防御性兜底
        raise _unsupported()


def _unsupported():
    from .upstream import UpstreamError
    return UpstreamError(
        501, "qoder_infer_unsupported",
        "Qoder 渠道的加密推理协议（wasm COSY 签名）尚未在本版接入，"
        "模型目录仅供查看；对话请改用 WorkBuddy / Cline 泳道模型。")
