"""登录渠道注册表。

登录方式按方便程度分层（用户要求：能扫码直接扫码，不能扫码再读本机）：

``qr``      协议级扫码——本地生成二维码，手机扫一扫，轮询拿 token（Raccoon）
``browser`` 网页授权——authUrl 同时渲染成终端二维码（手机扫码打开页面，
            页面上往往还有厂商自己的扫码/短信入口）+ 本机浏览器打开，
            state/回调轮询接 token（WorkBuddy 国内/国际版等）
``local``   本机凭据——直接读桌面客户端登录文件，零交互（兜底）

元数据提炼自 dsh-xdpool src/channels.ts 的 REGISTERED_CHANNELS 与
WorkBuddy-Switch desktop/client-registry.mjs。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    org: str
    method: str                      # qr / browser / local
    origin: str = ""                 # 登录页 / API 基址
    auth_platform: str = ""          # WorkBuddy 网页授权 platform 参数
    note: str = ""
    credits_claim: bool = False      # 是否有每日签到
    credits_status: bool = False     # 是否有签到状态查询
    unit: str = "credits"            # credits / dollars / tokens
    quota: bool = False              # 额度按百分比桶返回
    login_kind: str = "qr"           # method 的细分实现键


CHANNELS: dict[str, Channel] = {c.id: c for c in (
    # --- WorkBuddy 两个区（网页授权 + 本机凭据双通道，协议来自 WorkBuddy-Switch）---
    Channel("buddy", "WorkBuddy 国内版", "腾讯", "browser+local",
            origin="https://www.codebuddy.cn", auth_platform="workbuddy",
            note="DeepSeek · Kimi · GLM 免费线，每日赠送积分；桌面客户端可读本机快照",
            credits_claim=True, credits_status=True, login_kind="workbuddy-state"),
    Channel("workbuddy", "WorkBuddy 国际版", "腾讯", "browser+local",
            origin="https://www.workbuddy.ai", auth_platform="workbuddy-ai",
            note="独立端点与账号体系，积分与国内版不互通；后端没有每日签到接口",
            credits_status=True, login_kind="workbuddy-state"),
    # --- 扫码直登（协议级二维码轮询，来自 dsh-xdpool channel pack）---
    Channel("raccoon", "Raccoon", "商汤", "qr",
            origin="https://api.raccoon.sensetime.com",
            note="扫码直登：终端出二维码，手机小浣熊 App 扫一扫；短信验证码备用",
            login_kind="raccoon-qr"),
    # --- 其余 browser 渠道（打开厂商登录页，完成后按各自协议接 token）---
    Channel("codearts", "CodeArts", "华为云", "browser",
            origin="https://www.huaweicloud.com", login_kind="codearts-ticket",
            note="免费体验套餐自动续期不收费；新人积分 + DeepSeek · GLM · openPangu 每日免费额度",
            credits_claim=True),
    Channel("lobsterai", "LobsterAI", "有道", "browser",
            origin="https://lobsterai.youdao.com",
            note="每日签到领积分，积分换模型调用额度", credits_claim=True),
    Channel("qoder", "Qoder 国际版", "阿里云", "browser",
            login_kind="qoder-device", origin="https://qoder.com",
            note="每日 100 Credits，加密推理端点（wasm auth）", credits_claim=True),
    Channel("qodercn", "Qoder 国内版", "阿里云", "browser",
            login_kind="qoder-device", origin="https://qoder.com",
            note="每日 100 Credits，国内端点与模型池（wasm auth）", credits_claim=True),
    Channel("trae", "TRAE", "字节跳动", "local+browser",
            origin="https://www.trae.cn",
            note="手机号注册即送免费额度；本机有 Trae CN 客户端可直接读快照切换",
            credits_claim=True),
    Channel("cline", "Cline", "Cline Bot", "browser",
            login_kind="cline-device", origin="https://cline.bot",
            note="绑定 GitHub 账号后送美元计价免费额度；无每日签到", unit="dollars"),
    Channel("loomy", "Loomy", "讯飞星火", "qr",
            login_kind="loomy-wechat", origin="https://xinghuo.xfyun.cn",
            note="星火模型每日免费额度；登录页自带扫码或手机号入口",
            credits_claim=True),
    Channel("minimax", "MiniMax Code", "MiniMax", "qr",
            origin="https://account.minimax.cn",
            note="设备码 + PKCE 登录：网页显示设备码与验证链接，每日签到领额度",
            credits_claim=True, credits_status=True, login_kind="minimax-device"),
    Channel("zcode", "ZCode", "智谱", "browser",
            origin="https://chatglm.cn",
            note="GLM 模型按 token 计免费额度，需本机可开浏览器",
            credits_claim=True, unit="tokens"),
    Channel("gemini", "Gemini Code Assist", "Google", "browser",
            origin="https://codeassist.google.com",
            note="个人 Google 账号免费配额，本地回调 OAuth（网页出授权链接）",
            quota=True, login_kind="gemini-oauth"),
)}


def get_channel(channel_id: str) -> Channel:
    try:
        return CHANNELS[channel_id]
    except KeyError:
        raise KeyError(f"未知渠道：{channel_id}（可用：{', '.join(CHANNELS)}）") from None


def methods_of(channel: Channel) -> list[str]:
    """渠道支持的登录方式，按优先级排序（qr > browser > local）。"""
    order = {"qr": 0, "browser": 1, "local": 2}
    return sorted((m for m in channel.method.split("+") if m), key=order.__getitem__)
