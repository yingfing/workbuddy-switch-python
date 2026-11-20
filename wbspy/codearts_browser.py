"""码道 CodeArts 每日签到：CDP 驱动系统自带浏览器（headless）领活动福利。

设计取舍（用户拍板）：
- **不下载浏览器、不用 Playwright**——只探系统已有的 Chrome/Chromium/Edge
  可执行文件，通过 CDP（Chrome DevTools Protocol）驱动；
- **userData 持久**——登录快照放在 wbspy 数据目录下的 codearts-profile，
  华为云 SSO 的 Cookie/票据随 profile 存活，跨次运行自动续期；
- **首次登录用有头模式**（wbspy codearts-login）：弹出真实浏览器窗口，
  用户手动完成华为云登录；此后每日签到全程 headless；
- **失败打印日志**——浏览器缺失、CDP 调用失败、页面跳到登录页、接口业务
  错误，全部逐条打印并写进托管 summary，绝不静默。

接口链（2026-11 逆向自 dsh-daily-checkin，页面内 fetch、Cookie 鉴权、无签名）：
  GET  /chat/rest/me                                    验登录态（domainName）
  GET  /chat/PromptCenterService/v1/ops/delivery?channel=WEB   活动列表
  POST /chat/PromptCenterService/v1/ops/claim           {campaignId, idempotentKey}
  POST /chat/PromptCenterService/v1/ops/confirm         {campaignId}
积分：体验版 100/天、标准版 150、高级/旗舰 200（活动截止 2026-12-31）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from .cdp import CdpClient, CdpError
from .platform_paths import data_root

BASE_URL = "https://devcloud.cn-north-4.huaweicloud.com"
HOME_URL = f"{BASE_URL}/chat/home"
PROFILE_SUBDIR = "codearts-profile"
BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
            "chrome", "microsoft-edge", "msedge", "chrome-headless-shell")
READY_TIMEOUT = 30.0
LOGIN_POLL_SECONDS = 5.0
LOGIN_TIMEOUT = 600.0

_CLAIM_JS = """
(async () => {
  const req = async (method, pathname, body) => {
    try {
      const r = await fetch(pathname, {
        method, credentials: 'include',
        headers: {'Accept': 'application/json, text/plain, */*',
                  'Content-Type': 'application/json',
                  'x-requested-with': 'XMLHttpRequest',
                  'x-codearts-doer-scenario-type': 'web',
                  'X-Language': 'zh-cn', 'Agent-Type': 'PromptCenter'},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const t = await r.text();
      try { return {status: r.status, json: JSON.parse(t)}; }
      catch { return {status: r.status, json: {raw: String(t).slice(0, 200)}}; }
    } catch (e) { return {status: 0, json: {error: String(e && e.message || e)}}; }
  };
  if (!location.origin.includes('devcloud.cn-north-4.huaweicloud.com'))
    return {ok: false, reason: 'login-required', url: location.href.slice(0, 200)};
  const me = await req('GET', '/chat/rest/me');
  if (me.status !== 200 || !me.json || typeof me.json.domainName !== 'string')
    return {ok: false, reason: 'login-required', url: location.href.slice(0, 200)};
  const delivery = await req('GET',
      '/chat/PromptCenterService/v1/ops/delivery?channel=WEB');
  const items = (delivery.json && delivery.json.data && delivery.json.data.items) || [];
  if (delivery.status !== 200 || !Array.isArray(items))
    return {ok: false, reason: 'protocol', detail: '活动列表异常 HTTP ' + delivery.status};
  const daily = /签到|每日|daily/i;
  const results = [];
  for (const it of items) {
    if (!it || !it.campaignId) continue;
    const name = String(it.campaignName || it.title || it.campaignId).slice(0, 40);
    if (!daily.test(name) && !daily.test(it.description || '')) continue;
    const status = String(it.status || '');
    const claimed = ["CLAIMED", "ALREADY_CLAIMED", "DONE"].includes(status);
    if (it.claimable !== true && status !== 'ELIGIBLE') {
      if (claimed) results.push({already: true, name});
      continue;
    }
    const cid = it.campaignId;
    const claim = await req('POST', '/chat/PromptCenterService/v1/ops/claim',
        {campaignId: cid, idempotentKey: 'claim_' + cid + '_' + Date.now()});
    const code = Number(claim.json && claim.json.code);
    if (claim.status === 200 && (code === 0 || code === 200)) {
      const confirm = await req('POST', '/chat/PromptCenterService/v1/ops/confirm',
          {campaignId: cid});
      results.push({claimed: true, name,
          confirmOk: [0, 200].includes(Number(confirm.json && confirm.json.code))});
    } else {
      results.push({claimed: false, name,
          msg: String((claim.json && (claim.json.message || claim.json.error)) || 'HTTP ' + claim.status).slice(0, 120)});
    }
  }
  return {ok: true, name: me.json.domainName, results};
})()
"""


def find_browser() -> str | None:
    for name in BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    return None


def profile_dir() -> Path:
    path = data_root() / PROFILE_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _launch(browser: str, headless: bool) -> subprocess.Popen:
    args = [browser,
            f"--user-data-dir={profile_dir()}",
            "--remote-debugging-port=0",
            "--disable-gpu", "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,860"]
    if headless:
        args += ["--headless=new", "--no-sandbox"]     # OneCloud 常以 root 运行
    args.append("about:blank")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_devtools(profile: Path, proc: subprocess.Popen, timeout: float = 15.0) -> int:
    """读 user-data-dir 下的 DevToolsActivePort（--port=0 随机端口的官方解法）。"""
    marker = profile / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"浏览器启动即退出（exit {proc.returncode}），"
                               "可能是参数不被支持——请查看 wbspy 服务日志")
        if marker.exists():
            lines = marker.read_text().split()
            if lines and lines[0].isdigit():
                return int(lines[0])
        time.sleep(0.3)
    raise RuntimeError("等待浏览器调试端口超时（DevToolsActivePort 未生成）")


def _connect_page(port: int) -> CdpClient:
    import requests as _rq
    targets = _rq.get(f"http://127.0.0.1:{port}/json/list", timeout=10).json()
    for t in targets:
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return CdpClient(t["webSocketDebuggerUrl"])
    raise RuntimeError("浏览器调试列表中没有可用页面 target")


def _navigate(cdp: CdpClient, url: str, log) -> None:
    cdp.call("Page.navigate", {"url": url})
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        try:
            state = cdp.evaluate("document.readyState")
        except CdpError as exc:
            log(f"    codearts: 等待页面就绪出错 - {exc}")
            raise
        if state == "complete":
            return
        time.sleep(0.5)
    raise RuntimeError(f"页面加载超时（{READY_TIMEOUT:.0f}s）")


def _claim_once(cdp: CdpClient, log) -> dict:
    out = cdp.evaluate(_CLAIM_JS, timeout=60)
    if not isinstance(out, dict):
        raise RuntimeError(f"页面脚本返回异常类型：{type(out).__name__}")
    return out


def _summarize(out: dict) -> str:
    if not out.get("ok"):
        reason = out.get("reason")
        if reason == "login-required":
            raise RuntimeError(f"登录态失效（页面在 {out.get('url') or '登录页'}），"
                               "请运行 wbspy codearts-login 重新登录")
        detail = out.get("detail") or reason or "未知"
        raise RuntimeError(f"领取未完成：{detail}")
    results = out.get("results") or []
    claimed = [r for r in results if r.get("claimed")]
    already = [r for r in results if r.get("already")]
    parts = []
    if claimed:
        names = "、".join(r["name"] for r in claimed)
        parts.append(f"领取成功 {len(claimed)} 项（{names}）"
                     + ("" if all(r.get("confirmOk") for r in claimed) else "，部分到账待复核"))
    if already:
        parts.append("今日已领")
    if not parts:
        return "今日无待领的每日福利"
    return "；".join(parts)


def checkin_by_browser(log=print) -> dict:
    """headless 跑一轮码道签到，返回 summary 文字。失败打印日志并转文字。"""
    browser = find_browser()
    if not browser:
        log("    codearts: 未找到系统浏览器（需要 Chrome/Chromium/Edge 之一），跳过")
        return {"checkin": "失败：未找到系统浏览器"}
    proc = None
    cdp = None
    try:
        proc = _launch(browser, headless=True)
        port = _wait_devtools(profile_dir(), proc)
        cdp = _connect_page(port)
        _navigate(cdp, HOME_URL, log)
        out = _claim_once(cdp, log)
        return {"checkin": _summarize(out)}
    except Exception as exc:
        log(f"    codearts: 失败 - {exc}")
        return {"checkin": f"失败：{exc}"}
    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def interactive_login(log=print) -> int:
    """有头浏览器登录一次（wbspy codearts-login）。返回 0=成功 1=失败。"""
    browser = find_browser()
    if not browser:
        log("未找到系统浏览器：需要 Chrome/Chromium/Edge 之一。")
        return 1
    log(f"启动 {browser}（持久 profile：{profile_dir()}）……")
    proc = _launch(browser, headless=False)
    cdp = None
    try:
        port = _wait_devtools(profile_dir(), proc)
        cdp = _connect_page(port)
        log(f"请在弹出的浏览器窗口里完成华为云登录（超时 {LOGIN_TIMEOUT // 60} 分钟）。")
        log("登录成功后本命令自动退出；Cookie 保存在持久 profile，之后签到免登录。")
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(LOGIN_POLL_SECONDS)
            try:
                # 只读地址栏，不导航——导航会打断用户的登录流程
                state = cdp.evaluate(
                    "JSON.stringify({url: location.href.slice(0, 200), "
                    "onHome: location.pathname.includes('/chat')})", timeout=20)
                info = json.loads(state) if isinstance(state, str) else {}
                if info.get("onHome"):
                    log("检测到已进入码道页面，登录态已保存。")
                    return 0
            except Exception as exc:
                log(f"    （等待登录中：{exc}）")
        log("等待登录超时，未检测到成功登录。")
        return 1
    except Exception as exc:
        log(f"登录失败：{exc}")
        return 1
    finally:
        if cdp:
            try:
                cdp.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
