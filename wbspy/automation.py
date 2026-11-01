"""积分自动化：覆盖全部积分项的每日托管。

覆盖清单（对齐 dsh-xdpool upstream.ts / WorkBuddy-Switch rewards·task-runner）：

1. 猫猫领养（未领养则 agreement→first，旅行前置）
2. 每日签到 daily-checkin
3. 成长任务：accept 待做任务 + claim 已完成奖励
4. 连登兑换：streak 全档位已解锁未领取的自动 redeem
5. 幸运抽奖：lottery chances 抽完
6. 猫猫旅行：arrived→claim(带 record_id)；idle→depart
7. 补签卡：heatmap 昨日断签且卡有余 → makeup-cards/use

调度：`run_once` 单账号一轮（每项独立重试）；`loop` 固定间隔；`schedule`
每日定点（读 config.json automation.daily_at）；每轮结果可推送 webhook
（automation.notify_url，POST JSON）。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.request
from typing import Callable

from .document import Document
from .store import load_config
from .upstream import UpstreamError, WorkBuddyClient

MAX_RETRY = 2


def _retry(fn, label: str, log: Callable[[str], None], summary: dict,
           tries: int = MAX_RETRY):
    """单项动作重试；结果写进 summary。"""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except UpstreamError as exc:
            if exc.status == 401 or attempt == tries:
                summary[label] = f"失败：{exc.message}"
                log(f"    {label}: 失败 - {exc.message}")
                return None
            time.sleep(2 * attempt)
        except Exception as exc:                          # 网络抖动等
            if attempt == tries:
                summary[label] = f"失败：{exc}"
                log(f"    {label}: 失败 - {exc}")
                return None
            time.sleep(2 * attempt)


def run_once(doc: Document, log: Callable[[str], None] = print) -> dict:
    """对单个账号跑一轮全部积分动作。"""
    summary: dict[str, str] = {}
    if doc.region != "cn" or not doc.api_ready:
        summary["skip"] = "国际版或无明文 token，跳过活动"
        return summary
    client = WorkBuddyClient(doc)
    log(f"· {doc.label()}：")

    # 1. 猫猫领养（旅行前置）
    result = _retry(lambda: client.buddy_ensure(), "buddy", log, summary)
    if result:
        summary["buddy"] = result

    # 2. 每日签到
    def _checkin():
        if client.checkin_status().get("checked_in"):
            return "今日已签"
        r = client.checkin()
        return "已签" if r.get("already") else f"+{r.get('credit')} 积分"
    result = _retry(_checkin, "checkin", log, summary)
    if result:
        summary["checkin"] = result

    # 3. 成长任务：claim 可领的 + accept 未接的攒进度
    def _tasks():
        tasks = client.growth_tasks()
        earned = []
        for t in tasks:
            if t["claimable"]:
                r = client.claim_task(t["code"])
                if not r.get("already"):
                    earned.append(str(r.get("credit") or "?"))
        accepted = [t["code"] for t in tasks if t.get("accept_needed")]
        accepted_n = 0
        if accepted:
            try:
                client.accept_tasks(accepted)
                accepted_n = len(accepted)
            except UpstreamError:
                pass
        done_n = sum(1 for t in tasks if t.get("progress_done") and not t["claimed"])
        got = "+".join(earned) if earned else "0"
        msg = "claimed %d items (+%s)" % (len(earned), got)
        if accepted_n:
            msg += ", accepted %d" % accepted_n
        if done_n:
            msg += ", %d ready to claim" % done_n
        return msg
    result = _retry(_tasks, "tasks", log, summary)
    if result:
        summary["tasks"] = result

    # 4. 连登兑换
    def _streak():
        redeemed = client.redeem_streak_available()
        info = client.streak_full()
        tail = f"，连登 {info['days']} 天" + (
            f"，距下一档 {info['next_tier_remaining']} 天" if info.get("next_tier_remaining") else "")
        return f"兑换 {len(redeemed)} 档{tail}" if redeemed else f"无可兑换{tail}"
    result = _retry(_streak, "streak", log, summary)
    if result:
        summary["streak"] = result

    # 5. 幸运抽奖
    def _lottery():
        prizes = client.lottery_draw_all()
        return f"抽了 {len(prizes)} 次" if prizes else "无抽奖次数"
    result = _retry(_lottery, "lottery", log, summary)
    if result:
        summary["lottery"] = result

    # 6. 猫猫旅行（claim 带 record_id；出发选地点 4，奖励相同）
    def _travel():
        state = client.travel_status()
        if state["state"] == "arrived":
            claim = client.travel_claim(state.get("record_id"))
            summary["travel"] = f"领取 +{claim.get('credit')} 积分"
            state = client.travel_status()
        if state["state"] == "idle" and not state.get("daily_limit_reached"):
            client.travel_depart(4)
            return summary.get("travel") or "已出发（古镇客栈）"
        return summary.get("travel") or f"状态：{state['state']}"
    result = _retry(_travel, "travel", log, summary)
    if result:
        summary["travel"] = result

    # 7. 补签卡
    def _makeup():
        missed = client.heatmap_missed_yesterday()
        if not missed:
            return "昨日无断签"
        info = client.streak_full()
        if not info.get("makeup_cards"):
            return "昨日断签但无补签卡"
        client.use_makeup_card(missed)
        return f"已补签 {missed}"
    result = _retry(_makeup, "makeup", log, summary)
    if result:
        summary["makeup"] = result

    return summary


def run_all(documents: list[Document], log: Callable[[str], None] = print) -> dict[str, dict]:
    out = {}
    for doc in documents:
        out[doc.uid] = run_once(doc, log)
    _notify(out, log)
    return out


def _notify(result: dict, log: Callable[[str], None]) -> None:
    """轮次结果推送 webhook（config automation.notify_url），失败静默。"""
    url = (load_config().get("automation") or {}).get("notify_url")
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"event": "wbspy_automation", "result": result}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        log("通知已推送。")
    except Exception as exc:
        log(f"通知推送失败：{exc}")


def loop(documents: list[Document], interval_hours: float = 6.0,
         log: Callable[[str], None] = print) -> None:  # pragma: no cover
    log(f"自动化已启动：每 {interval_hours:g} 小时一轮，Ctrl+C 退出。")
    while True:
        log(f"— {time.strftime('%Y-%m-%d %H:%M:%S')} 开始本轮 —")
        try:
            run_all(documents, log)
        except Exception as exc:
            log(f"本轮异常：{exc}")
        time.sleep(interval_hours * 3600)


def seconds_until(hour: int, minute: int = 0) -> float:
    now = _dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    return (target - now).total_seconds()


def schedule(documents: list[Document],
             log: Callable[[str], None] = print) -> None:  # pragma: no cover
    """每日定点托管（读 config automation.daily_at，默认 09:00）。"""
    cfg = (load_config().get("automation") or {})
    hh, mm = ("09:00" if not cfg.get("daily_at") else str(cfg["daily_at"])).split(":")[:2]
    log(f"自动化定点托管：每日 {hh}:{mm} 执行（Ctrl+C 退出）。")
    while True:
        wait = seconds_until(int(hh), int(mm))
        log(f"下一轮：{time.strftime('%m-%d %H:%M', time.localtime(time.time() + wait))}")
        time.sleep(max(1, wait))
        try:
            run_all(documents, log)
        except Exception as exc:
            log(f"本轮异常：{exc}")
        time.sleep(60)                                    # 避免整点重入
