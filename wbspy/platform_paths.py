"""桌面客户端数据目录的三平台解析。

移植自 WorkBuddy-Switch（desktop/platform.mjs、client-registry.mjs）的
configRoot / data 目录规则，使 local 登录与桌面切换在 Windows / macOS /
Linux 上都能定位到客户端的 workbuddy-desktop.info 登录文件：

* macOS   → ~/Library/Application Support/<data>
* Windows → %APPDATA%\\<data>（Local 侧另有 LOCALAPPDATA 规则）
* Linux   → $XDG_CONFIG_HOME/<data>（默认 ~/.config）

个别客户端历史上也把数据放在 home 直下（~/.workbuddy），因此每个
客户端都同时给出 configRoot 与 home 两组候选，按顺序取第一个存在者。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PLATFORM = {"linux": "linux", "win32": "win32", "darwin": "darwin"}.get(
    sys.platform, "linux")

# 渠道 → (数据目录候选名列表, 登录文件名)
CLIENT_DATA: dict[str, tuple[list[str], str]] = {
    "buddy": ([".workbuddy"], "workbuddy-desktop.info"),
    "workbuddy": ([".workbuddy-ai"], "workbuddy-desktop-ai.info"),
    "trae": (["Trae CN", "Trae"], "workbuddy-desktop.info"),
    "qodercn": (["com.qodercn.app.stable", "Qoder CN", "QoderCN"],
                "workbuddy-desktop.info"),
    "qoder": (["com.qoder.app.stable", "Qoder"], "workbuddy-desktop.info"),
}


def config_root(platform: str = PLATFORM, home: str | Path | None = None,
                env: dict | None = None) -> Path:
    """Electron userData 的平台根目录（对应参考实现 configRoot）。"""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    if platform == "darwin":
        return home / "Library" / "Application Support"
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config")


def data_root(platform: str = PLATFORM, home: str | Path | None = None,
              env: dict | None = None) -> Path:
    """Windows 侧部分数据在 LOCALAPPDATA（对应参考实现 workBuddyAuthRoot 规则）。"""
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if platform == "linux":
        return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return config_root(platform, home, env)


def candidate_dirs(channel_id: str, platform: str = PLATFORM,
                   home: str | Path | None = None,
                   env: dict | None = None) -> list[Path]:
    """某渠道桌面客户端数据目录的全部平台候选（按优先级）。"""
    try:
        names, _ = CLIENT_DATA[channel_id]
    except KeyError:
        raise RuntimeError(f"渠道 {channel_id} 暂无本机凭据来源。") from None
    roots = [config_root(platform, home, env),
             data_root(platform, home, env),
             Path(home or Path.home())]
    return [root / name for root in roots for name in names]


def find_local_auth(channel_id: str, platform: str = PLATFORM,
                    home: str | Path | None = None,
                    env: dict | None = None) -> Path:
    """定位登录文件；找不到抛 FileNotFoundError。"""
    try:
        _, filename = CLIENT_DATA[channel_id]
    except KeyError:
        raise RuntimeError(f"渠道 {channel_id} 暂无本机凭据来源。") from None
    searched = []
    for d in candidate_dirs(channel_id, platform, home, env):
        path = d / filename
        if path.is_file():
            return path
        searched.append(str(path))
    raise FileNotFoundError(
        f"未找到 {channel_id} 的本机登录文件，已搜索：\n  " + "\n  ".join(searched))
