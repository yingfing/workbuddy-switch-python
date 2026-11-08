"""wbspy 协议核心单元测试：python3 -m unittest tests.test_wbspy -v"""

import json
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wbspy.document import api_origin, parse_auth, workbuddy_origin  # noqa: E402
from wbspy.switch import merge_auth  # noqa: E402
from wbspy.upstream import (  # noqa: E402
    apply_channel_lead_in,
    classify_business_error,
    is_third_party_opening,
    normalize_payload,
    relocate_system_role,
)


def make_doc(uid="u1", token="t1", domain="www.codebuddy.cn", **auth_extra):
    return {"account": {"uid": uid, "nickname": "测试"}, "auth": {"accessToken": token, "domain": domain, **auth_extra}}


class TestDocument(unittest.TestCase):
    def test_region_origins(self):
        self.assertEqual(api_origin("www.codebuddy.cn"), "https://www.codebuddy.cn")
        self.assertEqual(api_origin("www.workbuddy.cn"), "https://www.workbuddy.cn")
        self.assertEqual(api_origin("www.workbuddy.ai"), "https://www.workbuddy.ai")
        self.assertEqual(workbuddy_origin("www.codebuddy.cn"), "https://www.workbuddy.cn")
        self.assertEqual(workbuddy_origin("www.workbuddy.ai"), "https://www.workbuddy.ai")
        with self.assertRaises(ValueError):
            api_origin("evil.example.com")  # 未知域名拒绝外发凭据

    def test_parse_and_expiry(self):
        doc = parse_auth(make_doc(expiresAt=1_000_000_000))          # 秒 → 毫秒
        self.assertEqual(doc.token_expires_at, 1_000_000_000_000)
        self.assertFalse(doc.api_ready)
        doc = parse_auth(make_doc(expiresAt=9_999_999_999_999))      # 未来毫秒
        self.assertTrue(doc.api_ready)
        wrapper = {"account": {"uid": "u2"}, "auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "x"}}}
        doc = parse_auth(wrapper)
        self.assertTrue(doc.protected_token)
        self.assertFalse(doc.api_ready)
        with self.assertRaises(ValueError):
            parse_auth({"account": {}, "auth": {}})

    def test_merge_auth_preserves_local_fields(self):
        current = {"account": {"uid": "a"}, "auth": {"accessToken": "ta", "domain": "d1"},
                   "settings": {"theme": "dark"}, "accounts": [{"uid": "a", "x": 1}]}
        target = {"account": {"uid": "b"}, "auth": {"accessToken": "tb", "domain": "d2"},
                  "accounts": [{"uid": "b", "y": 2}]}
        merged = json.loads(merge_auth(json.dumps(current), json.dumps(target)))
        self.assertEqual(merged["account"]["uid"], "b")
        self.assertEqual(merged["auth"]["accessToken"], "tb")
        self.assertEqual(merged["settings"], {"theme": "dark"})            # 机器本地字段保留
        self.assertEqual([a["uid"] for a in merged["accounts"]], ["a", "b"])  # 去重合并


class TestChannelGuard(unittest.TestCase):
    def test_fingerprint_detection(self):
        self.assertTrue(is_third_party_opening("You are an AI agent powered by Foo"))
        self.assertTrue(is_third_party_opening("You are Claude Code, an official CLI"))
        self.assertFalse(is_third_party_opening("请帮我写一首诗"))

    def test_lead_in_moves_fingerprint_off_anchor(self):
        messages = [{"role": "system", "content": "You are an AI agent powered by X."},
                    {"role": "user", "content": "hi"}]
        out = apply_channel_lead_in(messages)
        self.assertTrue(out[0]["content"].startswith(
            "You are WorkBuddy, a helpful AI assistant."))

    def test_relocation_keeps_bytes_changes_role(self):
        payload = {"messages": [{"role": "system", "content": "SECRET-SYSTEM-TEXT"},
                                {"role": "user", "content": "hi"}], "model": "m"}
        out = relocate_system_role(payload)
        roles = [m["role"] for m in out["messages"]]
        self.assertEqual(roles, ["system", "user", "user"])
        self.assertIn("SECRET-SYSTEM-TEXT", out["messages"][1]["content"])

    def test_normalize_payload(self):
        out = normalize_payload({"model": "m",
                                 "messages": [{"role": "developer", "content": "be nice"}],
                                 "tool_choice": {"type": "function", "function": {"name": "search"}},
                                 "max_tokens": None, "evil": 1})
        self.assertEqual(out["messages"][0]["role"], "system")
        self.assertEqual(out["tool_choice"], "search")
        self.assertNotIn("evil", out)
        self.assertTrue(out["stream"])


class TestErrorClassification(unittest.TestCase):
    def test_auth_expired(self):
        err = classify_business_error(401, json.dumps({"code": 12153}), "")
        self.assertEqual(err.kind, "auth")

    def test_rate_limit_with_retry(self):
        err = classify_business_error(429, "{}", "120")
        self.assertEqual(err.kind, "rate")
        self.assertEqual(err.retry_after, 120)

    def test_channel_denied(self):
        err = classify_business_error(403, json.dumps({"code": "11128"}), "")
        self.assertEqual(err.code, "upstream_access_denied")

    def test_model_unavailable(self):
        for code in ("11102", "11158", "model_not_found"):
            self.assertEqual(classify_business_error(400, json.dumps({"code": code}), "").kind, "model")
        self.assertEqual(classify_business_error(404, "{}", "").kind, "model")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestConvert(unittest.TestCase):
    """OpenAI ↔ Anthropic 协议转换。"""

    def test_request_conversion(self):
        from wbspy.convert import to_anthropic_request
        out = to_anthropic_request({
            "model": "claude-x", "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "bye"}],
            "tools": [{"type": "function", "function": {
                "name": "search", "description": "s", "parameters": {"type": "object"}}}],
        })
        self.assertEqual(out["system"], "be nice")
        self.assertEqual(out["max_tokens"], 100)
        self.assertEqual([m["role"] for m in out["messages"]], ["user", "assistant", "user"])
        self.assertEqual(out["tools"][0]["name"], "search")
        self.assertNotIn("system", out["messages"][0])

    def test_response_conversion(self):
        from wbspy.convert import from_anthropic_response
        out = from_anthropic_response({
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 3, "output_tokens": 1}}, "m/deepseek")
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        self.assertEqual(out["choices"][0]["finish_reason"], "length")
        self.assertEqual(out["usage"]["prompt_tokens"], 3)

    def test_stream_conversion(self):
        import json as _json
        from wbspy.convert import anthropic_sse_to_openai
        sse = [b'event: content_block_delta\n',
               b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"A"}}\n\n',
               b'event: message_stop\n',
               b'data: {"type":"message_stop"}\n\n']
        out = b"".join(anthropic_sse_to_openai(iter(sse), "m/x"))
        self.assertIn(b'"content": "A"', out)
        self.assertIn(b"[DONE]", out)


class TestStreakAndBudget(unittest.TestCase):
    def test_streak_parsing_shape(self):
        # 解析逻辑要求 redemption.tiers + tier_X_status 扁平字段
        sample = {"streak": {"days": 5, "month_total_days": 12,
                             "next_tier": "t7", "next_tier_remaining": 2},
                  "redemption_status": {"tiers": [{"tier": "t3", "days": 3, "credit": 100}],
                                        "tier_t3_status": "unclaimed"},
                  "makeup_cards": {"balance": 2}}
        redemption = sample["redemption_status"]
        tier = redemption["tiers"][0]
        self.assertEqual(redemption["tier_t3_status"], "unclaimed")
        self.assertEqual(sample["streak"]["days"], 5)

    def test_context_trim(self):
        from wbspy.context_budget import trim, window_of
        self.assertGreater(window_of("anything"), 0)
        big = [{"role": "user", "content": "x" * 400000} for _ in range(5)]
        payload = {"model": "m", "messages": big + [{"role": "user", "content": "latest"}]}
        out, trimmed = trim(payload)
        if trimmed:  # 窗口足够小才触发
            self.assertLess(len(out["messages"]), len(payload["messages"]))
            self.assertEqual(out["messages"][-1]["content"], "latest")

    def test_estimate_tokens(self):
        from wbspy.usage import estimate_tokens
        self.assertGreater(estimate_tokens([{"role": "user", "content": "a" * 220}]), 90)


class TestAggregateFailover(unittest.TestCase):
    def test_candidates_order(self):
        from wbspy import aggregate
        rows = aggregate._provider_index()
        target = rows[0]
        cands = aggregate._candidates(target["route_model"])
        self.assertEqual(cands[0]["route_model"], target["route_model"])


class TestChannelName(unittest.TestCase):
    """domain → 渠道友好名识别（账号库「渠道」列的数据源）。"""

    def _doc(self, domain):
        return parse_auth({
            "account": {"uid": "u-1", "nickname": "n"},
            "auth": {"accessToken": "t", "domain": domain},
        })

    def test_workbuddy_variants(self):
        self.assertEqual(self._doc("www.codebuddy.cn").channel_name, "WorkBuddy 国内版")
        self.assertEqual(self._doc("www.workbuddy.ai").channel_name, "WorkBuddy 国际版")

    def test_third_party_channels(self):
        cases = {
            "api.trae.com.cn": "TRAE", "api.trae.cn": "TRAE",
            "qoder.cn": "Qoder 国内版", "qoder.com": "Qoder 国际版",
            "lobsterai-server.youdao.com": "LobsterAI",
            "zcode.z.ai": "ZCode", "api.cline.bot": "Cline",
            "codearts.huaweicloud.com": "CodeArts",
            "account.minimax.cn": "MiniMax Code",
            "account.xfinfr.com": "Loomy",
            "googleapis.com": "Gemini Code Assist",
            "api.raccoon.sensetime.com": "Raccoon",
        }
        for domain, want in cases.items():
            self.assertEqual(self._doc(domain).channel_name, want, domain)

    def test_unknown_domain_shown_raw(self):
        self.assertEqual(self._doc("some-new-vendor.com").channel_name,
                         "some-new-vendor.com")


class TestCreditsParse(unittest.TestCase):
    """credits() 的 Accounts 解析（对照 dsh-xdpool fetchCredits 实测结构）。"""

    def test_parse_ms(self):
        from wbspy.upstream import _parse_ms
        self.assertEqual(_parse_ms(1762400000000), 1762400000000)
        self.assertEqual(_parse_ms("2026-11-06 17:00:00"),
                         time.mktime(time.strptime("2026-11-06 17:00:00", "%Y-%m-%d %H:%M:%S")) * 1000)
        self.assertIsNone(_parse_ms("garbage"))
        self.assertIsNone(_parse_ms(None))

    def _account(self, **kw):
        base = {"PackageName": "新人礼包", "CapacityType": 1, "CapacitySize": 1000,
                "CapacityRemain": 500, "ExpiredTime": "2099-01-01 00:00:00"}
        base.update(kw)
        return base

    def _credits_of(self, accounts):
        """走 credits() 的真实解析路径（mock HTTP 层）。"""
        from unittest.mock import patch, MagicMock
        from wbspy.document import parse_auth
        from wbspy.upstream import WorkBuddyClient
        doc = parse_auth({"account": {"uid": "u", "nickname": "n"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        client = WorkBuddyClient(doc)
        resp = MagicMock(ok=True, status_code=200)
        resp.json.return_value = {"code": 0, "data": {"Response": {"Data": {"Accounts": accounts}}}}
        with patch.object(client.http, "post", return_value=resp):
            return client.credits()

    def test_one_shot_and_monthly_packages(self):
        monthly = {"PackageName": "月度包", "CapacityType": 4, "CycleCapacitySize": 2000,
                   "CycleCapacityRemain": 1500, "CycleEndTime": "2099-01-31 23:59:59"}
        c = self._credits_of([self._account(), monthly])
        self.assertEqual(c["total"], 2000)          # 500 一次性 + 1500 月度
        self.assertEqual(len(c["packages"]), 2)
        self.assertTrue(c["packages"][1]["monthly"])
        self.assertIn("nearest_expiry", c)

    def test_exhausted_and_expired_skipped(self):
        used_up = self._account(CapacityRemain=0)
        expired = self._account(PackageName="过期包", CapacityRemain=999,
                                ExpiredTime="2020-01-01 00:00:00")
        c = self._credits_of([used_up, expired])
        self.assertEqual(c["total"], 0)
        self.assertEqual(c["packages"], [])

    def test_far_future_expiry_32bit_safe(self):
        """32 位盒子（time_t 到 2038 年）上「永不过期」的 9999 日期会让
        mktime 炸「timestamp out of range」——必须降级为无过期时间，
        余额照常算出（用户实测：3 个账号余额列全是这条报错）。"""
        from unittest.mock import patch
        acc = self._account(ExpiredTime="9999-12-31 23:59:59")
        monthly = {"PackageName": "月度包", "CapacityType": 4,
                   "CycleCapacitySize": 2000, "CycleCapacityRemain": 1500}
        with patch("wbspy.upstream.time.mktime",
                   side_effect=OverflowError(
                       "timestamp out of range for platform time_t")):
            c = self._credits_of([acc, monthly])
        self.assertEqual(c["total"], 2000)               # 余额不受影响
        self.assertNotIn("nearest_expiry", c)            # 过期时间降级不显示
        for p in c["packages"]:
            self.assertNotIn("expires_at", p)

    def test_parse_ms_overflow_returns_none(self):
        from unittest.mock import patch
        from wbspy.upstream import _parse_ms
        with patch("wbspy.upstream.time.mktime",
                   side_effect=OverflowError("out of range")):
            self.assertIsNone(_parse_ms("9999-12-31 23:59:59"))
            self.assertIsNone(_parse_ms("2040-01-01 00:00:00"))


class TestClineClient(unittest.TestCase):
    """ClineClient：OpenAI 兼容直连 api.cline.bot（协议对照 dsh cline-adapter）。"""

    def _client(self):
        from wbspy.document import parse_auth
        from wbspy.upstream import ClineClient
        doc = parse_auth({"account": {"uid": "c1", "nickname": "T"},
                          "auth": {"accessToken": "abc", "domain": "api.cline.bot"}})
        return ClineClient(doc)

    def test_bearer_gets_workos_prefix(self):
        c = self._client()
        h = c._headers()
        self.assertEqual(h["Authorization"], "Bearer workos:abc")
        self.assertEqual(h["X-CLIENT-TYPE"], "cline-sdk")
        self.assertEqual(h["HTTP-Referer"], "https://cline.bot")

    def test_models_parse(self):
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import ClineClient
        c = self._client()
        resp = MagicMock(ok=True, status_code=200)
        resp.json.return_value = {"data": [
            {"id": "glm-5.2", "display_name": "GLM-5.2"},
            {"id": "kimi-k2.6:free", "name": "Kimi K2.6 Free"},
            {"id": "", "name": "坏行跳过"},
            "非对象跳过",
        ]}
        with patch.object(c.http, "get", return_value=resp) as gi:
            rows = c.models()
        self.assertEqual([r["id"] for r in rows], ["glm-5.2", "kimi-k2.6:free"])
        self.assertEqual(rows[0]["name"], "GLM-5.2")
        self.assertIn("/api/v1/models", gi.call_args[0][0])

    def test_chat_error_classify(self):
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import ClineClient, UpstreamError
        c = self._client()
        payload = {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]}
        for status, code in ((401, "upstream_auth_expired"), (402, "upstream_quota"),
                             (429, "upstream_rate_limit"), (500, "upstream_error")):
            resp = MagicMock(ok=False, status_code=status)
            resp.text = "err"
            with patch.object(c.http, "post", return_value=resp):
                with self.assertRaises(UpstreamError) as cm:
                    c.chat(payload)
                self.assertEqual(cm.exception.code, code, status)

    def test_chat_forces_stream_and_posts_openai_shape(self):
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import ClineClient
        c = self._client()
        resp = MagicMock(ok=True, status_code=200)
        with patch.object(c.http, "post", return_value=resp) as pi:
            c.chat({"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}],
                    "stream": False})
        body = json.loads(pi.call_args[1]["data"])
        self.assertTrue(body["stream"])                  # 网关聚合路径统一要求流式
        self.assertIn("/api/v1/chat/completions", pi.call_args[0][0])


if __name__ == "__main__":
    unittest.main()


class TestAutomationScope(unittest.TestCase):
    """自动化只对 WorkBuddy 国内版账号生效（其他渠道跳过，不发无效请求）。"""

    def test_third_party_account_skipped(self):
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        trae = parse_auth({"account": {"uid": "t", "nickname": "TRAE 号"},
                           "auth": {"accessToken": "tt", "domain": "api.trae.com.cn"}})
        with patch.object(automation, "WorkBuddyClient") as MockClient:
            summary = automation.run_once(trae, log=lambda *_: None)
            self.assertIn("skip", summary)
            MockClient.assert_not_called()          # 一个上游请求都不该发

    def test_buddy_account_runs(self):
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        buddy = parse_auth({"account": {"uid": "b", "nickname": "莲"},
                            "auth": {"accessToken": "tt", "domain": "www.codebuddy.cn"}})
        client = MagicMock()
        client.buddy_ensure.return_value = ""
        client.checkin.return_value = {"gotCredits": 100}
        with patch.object(automation, "WorkBuddyClient", return_value=client):
            summary = automation.run_once(buddy, log=lambda *_: None)
            self.assertNotIn("skip", summary)


class TestChannelIsolation(unittest.TestCase):
    """渠道隔离：TRAE 等第三方凭据绝不打 WorkBuddy 上游（防无效请求/凭据误用）。"""

    def _docs(self):
        return [
            parse_auth({"account": {"uid": "b1", "nickname": "莲"},
                        "auth": {"accessToken": "t1", "domain": "www.codebuddy.cn"}}),
            parse_auth({"account": {"uid": "g1", "nickname": "国际"},
                        "auth": {"accessToken": "t2", "domain": "www.workbuddy.ai"}}),
            parse_auth({"account": {"uid": "tr1", "nickname": "TRAE 号"},
                        "auth": {"accessToken": "t3", "domain": "api.trae.com.cn"}}),
        ]

    def test_healthy_excludes_third_party(self):
        from wbspy.pool import AccountPool
        p = AccountPool(self._docs())
        keys = [d.uid for d in p.healthy()]
        self.assertIn("b1", keys)
        self.assertIn("g1", keys)                   # 国际版照样进路由
        self.assertNotIn("tr1", keys)               # TRAE 不进 WorkBuddy 路由

    def test_models_and_chat_skip_third_party(self):
        from unittest.mock import patch, MagicMock
        from wbspy.pool import AccountPool
        models_called = []
        def fake_client(doc):
            m = MagicMock()
            m.models.side_effect = lambda: (models_called.append(doc.uid), [{"id": "m1"}])[1]
            return m
        with patch("wbspy.pool._client_for", side_effect=fake_client):
            p = AccountPool(self._docs())
            p.models()
        self.assertIn("b1", models_called)
        self.assertIn("g1", models_called)          # 国际版照常进目录
        self.assertNotIn("tr1", models_called)      # models 目录不对 TRAE 账号发请求

    def test_cline_lane_isolation(self):
        """Cline 泳道：cline 请求只落到 cline 账号，WorkBuddy 请求互不串。"""
        from unittest.mock import patch, MagicMock
        from wbspy.pool import AccountPool, _client_for, _lane_of
        cline_doc = parse_auth({"account": {"uid": "c1", "nickname": "TextValue"},
                                "auth": {"accessToken": "ct", "domain": "api.cline.bot"}})
        wb_doc = self._docs()[0]
        p = AccountPool([wb_doc, cline_doc])
        self.assertEqual(_lane_of(cline_doc), "cline")
        with patch("wbspy.pool._client_for", side_effect=lambda d: MagicMock(doc=d)):
            p2 = AccountPool([wb_doc, cline_doc])
            got_cline = p2.pick("*", lane="cline")
            got_wb = p2.pick("*", lane="workbuddy")
        self.assertEqual(got_cline.uid, "c1")
        self.assertEqual(got_wb.uid, "b1")

    def test_cline_models_prefixed(self):
        """Cline 账号目录带 cline: 前缀进聚合目录与逐账号索引。"""
        from unittest.mock import patch, MagicMock
        from wbspy.pool import AccountPool
        cline_doc = parse_auth({"account": {"uid": "c1", "nickname": "TextValue"},
                                "auth": {"accessToken": "ct", "domain": "api.cline.bot"}})
        fake = MagicMock()
        fake.models.return_value = [{"id": "glm-5.2", "name": "GLM-5.2", "region": "cn"}]
        with patch("wbspy.pool._client_for", return_value=fake):
            p = AccountPool([cline_doc])
            rows = p.models()
        self.assertEqual([r["id"] for r in rows], ["cline:glm-5.2"])
        by_acc = p.models_by_account()
        self.assertEqual(by_acc[0]["count"], 1)
        self.assertEqual(by_acc[0]["models"][0]["id"], "cline:glm-5.2")
        self.assertEqual(by_acc[0]["channel_id"], "cline")   # 前端按泳道算分母用

    def test_public_guard_blocks_and_allows(self):
        from wbspy.cli import _public_guard
        _public_guard("127.0.0.1", "")               # 回环放行
        _public_guard("0.0.0.0", "some-key")         # 有 key 放行
        _public_guard("0.0.0.0", "", allow_insecure=True)
        with self.assertRaises(SystemExit):
            _public_guard("0.0.0.0", "")             # 公网无 key 拒绝


class TestCapabilities(unittest.TestCase):
    """能力声明：路由的唯一依据（结构性防止跨渠道误调用）。"""

    def test_buddy_declares_all(self):
        from wbspy.document import parse_auth
        doc = parse_auth({"account": {"uid": "u", "nickname": "n"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        self.assertEqual(doc.capabilities,
                         frozenset({"chat", "models", "refresh", "checkin", "credits"}))

    def test_third_party_empty(self):
        from wbspy.document import parse_auth
        for domain in ("api.trae.com.cn",):
            doc = parse_auth({"account": {"uid": "u", "nickname": "n"},
                              "auth": {"accessToken": "t", "domain": domain}})
            self.assertEqual(doc.capabilities, frozenset(), domain)
        # qoder 声明 chat/models（目录可见 + wasm 加密推理），但无 refresh/credits
        qd = parse_auth({"account": {"uid": "u", "nickname": "n"},
                         "auth": {"accessToken": "t", "domain": "qoder.cn"}})
        self.assertEqual(qd.capabilities, frozenset({"chat", "models"}))

    def test_cline_capabilities(self):
        """Cline 已接 OpenAI 兼容 API：chat/models/refresh 有、签到/余额无。"""
        from wbspy.document import parse_auth
        doc = parse_auth({"account": {"uid": "u", "nickname": "n"},
                          "auth": {"accessToken": "t", "domain": "api.cline.bot"}})
        self.assertEqual(doc.capabilities,
                         frozenset({"chat", "models", "refresh"}))

    def test_channel_names_come_from_registry(self):
        """channel_name 必须与注册表同名（单一事实来源）。"""
        from wbspy.channels import CHANNELS
        from wbspy.document import parse_auth, _CHANNEL_DOMAIN_NEEDLES
        for needle, cid in _CHANNEL_DOMAIN_NEEDLES:
            cid = cid("qoder.cn") if callable(cid) else cid
            self.assertIn(cid, CHANNELS, cid)
        doc = parse_auth({"account": {"uid": "u", "nickname": "n"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        self.assertEqual(doc.channel_name, CHANNELS["buddy"].name)


class TestAutomationHistory(unittest.TestCase):
    """自动化历史落盘：任何入口跑完都有据可查。"""

    def test_run_all_writes_history(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        doc = parse_auth({"account": {"uid": "h1", "nickname": "历史号"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "automation-history.json"
            with patch.object(automation, "HISTORY_PATH", fake_path), \
                 patch.object(automation, "WorkBuddyClient") as MC:
                client = MagicMock()
                client.buddy_ensure.return_value = ""
                client.checkin.return_value = {"gotCredits": 100}
                client.tasks.return_value = {}
                client.streak_claim.return_value = {}
                client.lottery.return_value = {}
                client.travel_status.return_value = {"state": "idle", "daily_limit_reached": True}
                client.travel_depart.return_value = {}
                client.travel_claim.return_value = {}
                client.makeup.return_value = {}
                MC.return_value = client
                out = automation.run_all([doc], log=lambda *_: None)
            self.assertTrue(fake_path.exists())
            hist = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(hist[0]["results"][0]["uid"], "h1")
            self.assertTrue(hist[0]["log"])             # 日志行也入档
            self.assertIn("checkin", out["h1"])


class TestGatewayModels(unittest.TestCase):
    """`/v1/models` 只暴露当前真正可调用的模型。"""

    def test_unconfigured_providers_hidden(self):
        """未配置 key 的聚合厂商不进目录；已配置的正常列出。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool
        app = create_app(AccountPool([]), api_key="")
        fake = [
            {"id": "prov/ok", "object": "model", "owned_by": "wbspy:ok",
             "wbspy": {"configured": True}},
            {"id": "prov/bad", "object": "model", "owned_by": "wbspy:bad",
             "wbspy": {"configured": False}},
        ]
        with patch("wbspy.gateway.aggregate.models", return_value=fake):
            c = TestClient(app)
            ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
        self.assertIn("prov/ok", ids)
        self.assertNotIn("prov/bad", ids)     # 调不动的模型不再「缝合」进目录

    def test_pool_models_carry_name(self):
        """账号池模型下发 name/region 扩展字段（客户端可读显示名）。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool
        app = create_app(AccountPool([]), api_key="")
        fake_pool = [{"id": "workbuddy:fast-model", "name": "快速", "region": "cn"}]
        pool = AccountPool([])
        app = create_app(pool, api_key="")
        with patch("wbspy.gateway.aggregate.models", return_value=[]), \
             patch.object(pool, "models", return_value=fake_pool):
            row = TestClient(app).get("/v1/models").json()["data"][0]
        self.assertEqual(row["name"], "快速")
        self.assertEqual(row["region"], "cn")
        self.assertEqual(row["id"], "workbuddy:fast-model")

    def test_credit_multiplier_passthrough_and_sort(self):
        """官方计费倍率透传进目录并按倍率升序（限时免费 0 最前）。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool
        fake_pool = [
            {"id": "workbuddy:balanced-model", "name": "均衡", "region": "cn",
             "credit_multiplier": 0.5},
            {"id": "workbuddy:hy4free", "name": "Hy4", "region": "cn",
             "credit_multiplier": 0},
            {"id": "workbuddy:auto", "name": "Auto", "region": "cn"},
        ]
        pool = AccountPool([])
        app = create_app(pool, api_key="")
        with patch("wbspy.gateway.aggregate.models", return_value=[]), \
             patch.object(pool, "models", return_value=fake_pool):
            rows = TestClient(app).get("/v1/models").json()["data"]
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, ["workbuddy:hy4free", "workbuddy:balanced-model",
                               "workbuddy:auto"])      # 免费 → 倍率升序 → 无倍率殿后
        self.assertEqual(rows[0]["credit_multiplier"], 0)
        # 倍率拼进显示名，任何客户端可见
        names = {r["id"]: r["name"] for r in rows}
        self.assertEqual(names["workbuddy:hy4free"], "Hy4（限时免费）")
        self.assertEqual(names["workbuddy:balanced-model"], "均衡 0.50x")
        self.assertEqual(names["workbuddy:auto"], "Auto")   # 无倍率不加后缀


    def test_legacy_cn_prefix_still_routes(self):
        """旧客户端配置的 cn:/global: 前缀继续按账号池路由（不断）。"""
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool

        seen: list[str] = []

        class _P(AccountPool):
            def chat_failover(self, raw, *_a, **_k):
                seen.append(raw["model"])
                raise RuntimeError("stop-here")      # 到池即停，只验证前缀剥除

        c = TestClient(create_app(_P([]), api_key=""), raise_server_exceptions=False)
        for mid in ("cn:auto", "global:auto", "workbuddy:auto", "auto"):
            c.post("/v1/chat/completions", json={"model": mid, "messages": []})
            self.assertEqual(seen[-1], "auto", mid)


class TestGatewayKey(unittest.TestCase):
    """网关访问密钥：内置默认 workbuddy + 网页改密钥热生效。"""

    def test_resolve_default_workbuddy(self):
        """解析链：显式 > env > config > 内置默认 workbuddy；--allow-insecure 置空。"""
        import os
        from unittest.mock import patch
        from wbspy.cli import _resolve_api_key, DEFAULT_API_KEY
        self.assertEqual(_resolve_api_key("", {}, False), "workbuddy")   # 全空 → 默认
        self.assertEqual(_resolve_api_key("", {}, False), DEFAULT_API_KEY)
        self.assertEqual(_resolve_api_key("k1", {}, False), "k1")        # 显式优先
        with patch.dict(os.environ, {"WBSPY_API_KEY": "envk"}):
            self.assertEqual(_resolve_api_key("", {}, False), "envk")    # env 次之
        self.assertEqual(_resolve_api_key("", {"api_key": "cfgk"}, False), "cfgk")
        self.assertEqual(_resolve_api_key("", {}, True), "")             # 放行 = 不校验

    def test_v1_protected_api_open(self):
        """/v1 校验 Bearer；/api 管理端点不被网关密钥锁死。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.webui import create_web_app
        from wbspy.pool import AccountPool
        app = create_app(AccountPool([]), api_key="workbuddy")
        with patch("wbspy.gateway.aggregate.models", return_value=[]):
            c = TestClient(app)
            self.assertEqual(c.get("/v1/models").status_code, 401)                 # 无 key
            self.assertEqual(c.get("/v1/models",
                                   headers={"Authorization": "Bearer bad"}).status_code, 401)
            self.assertEqual(c.get("/v1/models",
                                   headers={"Authorization": "Bearer workbuddy"}).status_code, 200)
            self.assertEqual(c.get("/v1/models",
                                   headers={"X-Wbspy-Key": "workbuddy"}).status_code, 200)
        # /api/* 管理界面不被网关密钥锁死（webui 端点验证）
        w = TestClient(create_web_app(AccountPool([]), api_key="workbuddy"))
        self.assertEqual(w.get("/api/pool").status_code, 200)

    def test_webui_change_key_hot_reload(self):
        """网页改密钥：落盘 config + 立即生效（旧 key 401、新 key 200）。"""
        import tempfile
        import json as _json
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.webui import create_web_app
        from wbspy.pool import AccountPool
        from wbspy import store
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            ctx = (patch.object(store, "CONFIG_PATH", cfg_path),
                   patch("wbspy.webui.load_config", store.load_config),
                   patch("wbspy.webui.save_config", store.save_config),
                   patch("wbspy.webui.load_accounts", return_value=[]))
            for p in ctx:
                p.start()
            self.addCleanup(lambda: [p.stop() for p in ctx])
            app = create_web_app(AccountPool([]), api_key="workbuddy")
            c = TestClient(app)
            r = c.get("/api/settings/gateway-key").json()["data"]
            self.assertTrue(r["is_default"])                                       # 初始为默认
            # 改成自定义密钥
            r = c.post("/api/settings/gateway-key",
                       json={"api_key": "my-secret"}).json()["data"]
            self.assertEqual(r["key"], "my-secret")
            self.assertFalse(r["is_default"])
            self.assertEqual(_json.loads(cfg_path.read_text())["api_key"], "my-secret")
            # /v1 热生效：旧默认 key 拒、新 key 过
            self.assertEqual(c.get("/v1/models",
                                   headers={"Authorization": "Bearer workbuddy"}).status_code, 401)
            self.assertEqual(c.get("/v1/models",
                                   headers={"Authorization": "Bearer my-secret"}).status_code, 200)
            # 改回默认：config 里的自定义值被移除
            r = c.post("/api/settings/gateway-key", json={"api_key": ""}).json()["data"]
            self.assertTrue(r["is_default"])
            self.assertNotIn("api_key", _json.loads(cfg_path.read_text()))
            self.assertEqual(c.get("/v1/models",
                                   headers={"Authorization": "Bearer workbuddy"}).status_code, 200)
            # 太短的密钥拒绝（400）
            self.assertEqual(c.post("/api/settings/gateway-key",
                                    json={"api_key": "ab"}).status_code, 400)


class TestGatewayToggle(unittest.TestCase):
    """网关开关（gateway.enabled 热生效）+ 服务端口（试绑校验、config 落盘）。"""

    def _client(self, cfg_path):
        """起一个 config 重定向到临时目录的 web 测试客户端。"""
        import json as _json
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.webui import create_web_app
        from wbspy.pool import AccountPool
        from wbspy import store
        ctx = (patch.object(store, "CONFIG_PATH", Path(cfg_path)),
               patch("wbspy.webui.load_config", store.load_config),
               patch("wbspy.webui.save_config", store.save_config),
               patch("wbspy.webui.load_accounts", return_value=[]),
               patch("wbspy.gateway.load_config", store.load_config))
        for p in ctx:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ctx])
        self._pool = AccountPool([])          # 暴露给用例做 patch
        app = create_web_app(self._pool, api_key="", port=8178)
        return _json, TestClient(app)

    def test_default_on_and_toggle_hot(self):
        """默认开启；网页关闭 → /v1 立即 404；再开 → 恢复；config 落盘。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            _, c = self._client(cfg_path)
            self.assertEqual(c.get("/v1/models").status_code, 200)             # 默认开
            r = c.post("/api/settings/gateway",
                       json={"enabled": False}).json()["data"]
            self.assertFalse(r["enabled"])
            self.assertEqual(c.get("/v1/models").status_code, 404)             # 热生效
            self.assertEqual(c.get("/healthz").status_code, 200)               # 本机排障不受影响
            self.assertEqual(c.get("/api/pool").status_code, 200)              # 管理界面不受影响
            self.assertEqual(c.get("/v1/models").json()["error"]["type"],
                             "gateway_disabled")
            import json as _json
            self.assertFalse(_json.loads(cfg_path.read_text())["gateway"]["enabled"])
            r = c.post("/api/settings/gateway", json={"enabled": True}).json()["data"]
            self.assertTrue(r["enabled"])
            self.assertEqual(c.get("/v1/models").status_code, 200)

    def test_start_disabled_by_config(self):
        """config gateway.enabled=false 时启动，网关直接处于关闭态。"""
        import tempfile
        import json as _json
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(_json.dumps({"gateway": {"enabled": False}}))
            _, c = self._client(cfg_path)
            self.assertEqual(c.get("/v1/models").status_code, 404)
            self.assertEqual(c.get("/api/pool").status_code, 200)

    def test_aggregate_toggle_hides_catalog(self):
        """聚合渠道开关：默认开；关 → /v1/models 只剩账号池行；落盘；再开恢复。"""
        import tempfile
        import json as _json
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            _, c = self._client(cfg_path)
            pool_row = {"id": "workbuddy:auto", "name": "Auto", "region": "cn",
                        "credit_multiplier": 1.0}
            agg_row = {"id": "opencode/jev-1.13-free", "name": "Jev",
                       "owned_by": "opencode",
                       "wbspy": {"configured": True, "note": "free"}}
            with patch.object(self._pool, "models", return_value=[pool_row]), \
                 patch("wbspy.gateway.aggregate.models", return_value=[agg_row]):
                ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
                self.assertIn("workbuddy:auto", ids)
                self.assertIn("opencode/jev-1.13-free", ids)               # 默认纳入
                r = c.post("/api/settings/gateway",
                           json={"aggregate": False}).json()["data"]
                self.assertFalse(r["aggregate"])
                ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
                self.assertIn("workbuddy:auto", ids)                       # 池内行保留
                self.assertNotIn("opencode/jev-1.13-free", ids)            # 聚合行隐藏
            self.assertFalse(
                _json.loads(cfg_path.read_text())["gateway"]["aggregate"])
            r = c.post("/api/settings/gateway",
                       json={"aggregate": True}).json()["data"]
            self.assertTrue(r["aggregate"])

    def test_port_save_validate_and_persist(self):
        """端口：非法值 400；空闲端口落盘 web.port + restart_required；同端口免重启。"""
        import socket
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            _, c = self._client(cfg_path)
            self.assertEqual(c.post("/api/settings/gateway",
                                    json={"port": "0"}).status_code, 400)
            self.assertEqual(c.post("/api/settings/gateway",
                                    json={"port": "99999"}).status_code, 400)
            self.assertEqual(c.post("/api/settings/gateway",
                                    json={"port": "abc"}).status_code, 400)
            # 找一个系统空闲端口试保存
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
            probe.close()
            r = c.post("/api/settings/gateway",
                       json={"port": str(free)}).json()["data"]
            self.assertTrue(r["restart_required"])
            self.assertEqual(r["port"], free)
            import json as _json
            self.assertEqual(_json.loads(cfg_path.read_text())["web"]["port"], free)
            # 与当前监听端口相同：不要求重启
            r = c.post("/api/settings/gateway",
                       json={"port": "8178"}).json()["data"]
            self.assertFalse(r["restart_required"])

    def test_port_busy_rejected(self):
        """保存被真实占用的端口 → 400，且不落盘。"""
        import socket
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            _, c = self._client(cfg_path)
            blocker = socket.socket()
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", 0))
            busy = blocker.getsockname()[1]
            blocker.listen(1)
            try:
                self.assertEqual(c.post("/api/settings/gateway",
                                        json={"port": str(busy)}).status_code, 400)
            finally:
                blocker.close()
            import json as _json
            saved = _json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
            self.assertNotIn("web", saved)         # 占用端口拒绝落盘


class TestRestartArgv(unittest.TestCase):
    """重启 argv 重建：--port 用 config web.port 覆盖（网页改端口重启即生效）。"""

    def test_rebuild_replaces_port_keeps_rest(self):
        from wbspy.webui import rebuild_restart_argv
        argv = ["web", "--host", "0.0.0.0", "--port", "8080", "--allow-insecure"]
        out = rebuild_restart_argv(argv, 8080, {"web": {"port": 9090}})
        self.assertEqual(out, ["web", "--host", "0.0.0.0", "--allow-insecure",
                               "--port", "9090"])
        # config 未配端口 → 保留当前端口
        out = rebuild_restart_argv(argv, 8080, {})
        self.assertEqual(out, ["web", "--host", "0.0.0.0", "--allow-insecure",
                               "--port", "8080"])
        # --port= 形式也能替换
        out = rebuild_restart_argv(["web", "--port=8080"], 8080,
                                   {"web": {"port": 9090}})
        self.assertEqual(out, ["web", "--port", "9090"])

    def test_effective_port_resolution(self):
        from wbspy.cli import _effective_port
        self.assertEqual(_effective_port(9999, {}), 9999)                  # 显式优先
        self.assertEqual(_effective_port(None, {"web": {"port": 7777}}), 7777)
        self.assertEqual(_effective_port(None, {}), 8178)                  # 默认
        self.assertEqual(_effective_port(None, {"web": {"port": "xx"}}), 8178)
    def test_config_subcommand_roundtrip(self):
        """wbspy config 子命令：set 类型推断/密钥键强制字符串/get/unset。"""
        import tempfile
        import json as _json
        import argparse as _ap
        from unittest.mock import patch
        from pathlib import Path
        from wbspy import store
        from wbspy.cli import _cmd_config
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with patch.object(store, "CONFIG_PATH", cfg_path):
                def run(*a):
                    args = _ap.Namespace(action=a[0],
                                         key=a[1] if len(a) > 1 else "",
                                         value=a[2] if len(a) > 2 else "")
                    return _cmd_config(args)
                run("set", "automation.notify_url", "https://x.example/push")
                run("set", "automation.interval_minutes", "45")
                run("set", "api_key", "88888888")        # 纯数字也必须是字符串
                on_disk = _json.loads(cfg_path.read_text())
                self.assertEqual(on_disk["automation"]["interval_minutes"], 45)
                self.assertIsInstance(on_disk["api_key"], str)
                self.assertEqual(on_disk["api_key"], "88888888")
                run("unset", "automation.notify_url")
                self.assertNotIn("notify_url",
                                 _json.loads(cfg_path.read_text())["automation"])


class TestPoolStrategies(unittest.TestCase):
    """多账号调度：xdpool 四策略——sticky/排队等待/分配可配/付费行退路。"""

    def _pool(self, n=3, region="cn"):
        from wbspy.document import parse_auth
        from wbspy.pool import AccountPool
        docs = [parse_auth({"account": {"uid": f"u{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
                for i in range(n)]
        return AccountPool(docs)

    def _mock_client(self, pool, uid, resp=None):
        from unittest.mock import MagicMock
        k = next(k for k in pool.clients if k[1] == uid)
        c = MagicMock()
        c.chat.return_value = resp if resp is not None else MagicMock()
        pool.clients[k] = c
        return k, c

    def _distribution(self, name):
        from unittest.mock import patch
        from wbspy import store
        return patch.object(store, "load_config",
                            return_value={"gateway": {"distribution": name}})

    def test_sticky_session_binds_and_releases(self):
        """同会话粘同一账号；账号失效后解绑重新分配。"""
        from wbspy.upstream import UpstreamError
        pool = self._pool()
        with self._distribution("round-robin"):
            a = pool.pick("auto", session="s1")
            b = pool.pick("auto", session="s1")
            self.assertIs(a, b)                       # 粘住
            k = next(k for k in pool.documents if pool.documents[k] is a)
            pool.apply_error(a, UpstreamError(401, "upstream_auth_expired", "失效"), "auto")
            self.assertNotEqual(pool.session_map.get("s1", (None,))[0], k)  # 解绑
            c = pool.pick("auto", session="s1")
            self.assertIsNot(c, a)                    # 换新号并重新绑定
            self.assertIs(c, pool.pick("auto", session="s1"))

    def test_distribution_priority_vs_roundrobin(self):
        """priority 永远先用第一个号；round-robin 轮换。"""
        pool = self._pool(3)
        with self._distribution("priority"):
            picks = {id(pool.pick("auto")) for _ in range(4)}
            self.assertEqual(len(picks), 1)           # 永远同一个
        with self._distribution("round-robin"):
            picks = [id(pool.pick("auto")) for _ in range(3)]
            self.assertEqual(len(set(picks)), 3)      # 三个号都轮到

    def test_balanced_returns_usable_account(self):
        """balanced 加权随机不崩且只返回可用账号。"""
        pool = self._pool(3)
        with self._distribution("balanced"):
            for _ in range(10):
                doc = pool.pick("auto")
                self.assertIsNotNone(doc)
                pool.last_used[next(k for k in pool.clients
                                    if pool.clients[k].doc is doc)] = 0  # 模拟刚用过

    def test_wait_for_cooling_then_succeed(self):
        """全池冷却 → 等最短冷却（sleep 模拟时间流逝）→ 解冻后成功。"""
        from unittest.mock import patch
        from wbspy.upstream import UpstreamError
        pool = self._pool(1)
        k, _client = self._mock_client(pool, "u0")
        with self._distribution("round-robin"):
            pool.apply_error(pool.healthy("auto")[0],
                             UpstreamError(429, "upstream_rate_limit", "限流"), "auto")
            sleeps: list[float] = []

            def fake_sleep(sec):
                sleeps.append(sec)
                pool.cooldowns._until.clear()         # 模拟冷却到期

            with patch("wbspy.pool.time.sleep", fake_sleep):
                doc, _ = pool.chat_failover({"model": "auto", "messages": []})
            self.assertIsNotNone(doc)
            self.assertTrue(sleeps and sleeps[0] <= 91)   # 只等冷却时长，不超上限

    def test_fallback_row_on_free_row_cooldown(self):
        """免费行 hy3 全冷却 → 自动降级用付费行 hy3-x 发请求。"""
        from unittest.mock import patch
        from wbspy.upstream import UpstreamError
        pool = self._pool(1)
        k, client = self._mock_client(pool, "u0")
        with self._distribution("round-robin"):
            pool.apply_error(pool.healthy("hy3")[0],
                             UpstreamError(429, "upstream_rate_limit", "限流"), "hy3")
            # hy3 冷却中 → chat_failover 自动降级以付费行 hy3-x 发出
            doc, _ = pool.chat_failover({"model": "hy3", "messages": []})
            sent = client.chat.call_args[0][0]
            self.assertEqual(sent["model"], "hy3-x")

    def test_session_passthrough_from_gateway(self):
        """网关从 x-wbspy-session 头或消息指纹生成会话键并传入 failover。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool
        seen: list = []

        class _P(AccountPool):
            def chat_failover(self, payload, session=None, lane="workbuddy"):
                seen.append((session, lane))
                raise UpstreamError(503, "no_available_accounts", "无账号")

        c = TestClient(create_app(_P([]), api_key=""), raise_server_exceptions=False)
        body = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        c.post("/v1/chat/completions", json=body)      # 无头 → 指纹
        self.assertTrue(seen[-1][0] and seen[-1][0].startswith("auto:"))
        self.assertEqual(seen[-1][1], "workbuddy")     # 默认走 WorkBuddy 泳道
        c.post("/v1/chat/completions", json=body, headers={"x-wbspy-session": "abc"})
        self.assertEqual(seen[-1][0], "abc")           # 显式头优先


class TestAutomationConfig(unittest.TestCase):
    """托管节奏配置：落盘 + 校验 + 自动生效。"""

    def _client(self, tmp):
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from wbspy import store
        cfg_path = Path(tmp) / "config.json"
        ctx = (patch.object(store, "CONFIG_PATH", cfg_path),
               patch("wbspy.webui.load_config", store.load_config),
               patch("wbspy.webui.save_config", store.save_config),
               patch("wbspy.webui.load_accounts", return_value=[]),
               patch("wbspy.automation.load_config", store.load_config))
        for p in ctx:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ctx])
        return (TestClient(create_web_app(AccountPool([]))), cfg_path)

    def test_logs_endpoint_counts(self):
        """/api/logs：悬浮面板数据源——最近日志行 + 累计行数（未读徽标依据）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            r = c.get("/api/logs").json()["data"]
            self.assertEqual(r["total"], 0)
            self.assertEqual(r["log"], [])
            c.post("/api/automation/toggle", json={"enabled": True})
            c.post("/api/automation/toggle", json={"enabled": False})
            r = c.get("/api/logs").json()["data"]
            self.assertGreaterEqual(r["total"], 2)       # 开/关 + 轮询线程启动日志
            self.assertTrue(r["log"])

    def test_config_interval_saved(self):
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            c, cfg_path = self._client(tmp)
            r = c.post("/api/automation/config",
                       json={"mode": "interval", "interval_minutes": 30}).json()
            self.assertTrue(r["data"]["saved"])
            on_disk = _json.loads(cfg_path.read_text())["automation"]
            self.assertEqual(on_disk["interval_minutes"], 30)
            st = c.get("/api/automation/status").json()["data"]
            self.assertEqual(st["interval_minutes"], 30)
            self.assertEqual(st["mode"], "interval")

    def test_config_rejects_out_of_range(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = self._client(tmp)
            self.assertEqual(c.post("/api/automation/config",
                                    json={"mode": "interval",
                                          "interval_minutes": 1}).status_code, 400)
            self.assertEqual(c.post("/api/automation/config",
                                    json={"mode": "interval",
                                          "interval_minutes": 9999}).status_code, 400)
            self.assertEqual(c.post("/api/automation/config",
                                    json={"mode": "daily",
                                          "daily_at": "25:00"}).status_code, 400)
            self.assertEqual(c.post("/api/automation/config",
                                    json={"mode": "daily",
                                          "daily_at": "broken"}).status_code, 400)
            self.assertEqual(c.post("/api/automation/config",
                                    json={"mode": "xxx"}).status_code, 400)

    def test_notify_url_saved_and_cleared(self):
        """webhook 推送地址随节奏配置一并保存；空值=清除；status 回读。"""
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            c, cfg_path = self._client(tmp)
            r = c.post("/api/automation/config",
                       json={"mode": "interval", "interval_minutes": 60,
                             "notify_url": "https://sctapi.ftqq.com/x.send"}).json()["data"]
            self.assertEqual(r["notify_url"], "https://sctapi.ftqq.com/x.send")
            on_disk = _json.loads(cfg_path.read_text())["automation"]["notify_url"]
            self.assertEqual(on_disk, "https://sctapi.ftqq.com/x.send")
            st = c.get("/api/automation/status").json()["data"]
            self.assertEqual(st["notify_url"], "https://sctapi.ftqq.com/x.send")
            # 留空再保存 = 清除
            r = c.post("/api/automation/config",
                       json={"mode": "interval", "interval_minutes": 60,
                             "notify_url": ""}).json()["data"]
            self.assertEqual(r["notify_url"], "")
            self.assertNotIn("notify_url", _json.loads(cfg_path.read_text())["automation"])

    def test_config_daily_saved_and_normalized(self):
        import tempfile
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            c, cfg_path = self._client(tmp)
            r = c.post("/api/automation/config",
                       json={"mode": "daily", "daily_at": "8:5"}).json()
            self.assertTrue(r["data"]["saved"])
            on_disk = _json.loads(cfg_path.read_text())["automation"]
            self.assertEqual(on_disk["daily_at"], "08:05")
            st = c.get("/api/automation/status").json()["data"]
            self.assertEqual(st["mode"], "daily")
            self.assertEqual(st["daily_at"], "08:05")

    def test_poll_cfg_defaults_and_clamp(self):
        """automation._poll_cfg：无配置给默认，坏值兜底 clamp（5~1440）。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from wbspy import automation
        from wbspy import store
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(store, "CONFIG_PATH", Path(tmp) / "config.json"), \
                 patch.object(automation, "load_config", store.load_config):
                self.assertEqual(automation._poll_cfg(),
                                 {"mode": "interval", "interval_minutes": 120,
                                  "daily_at": "09:00"})
                store.save_config({"automation": {"interval_minutes": "abc"}})
                self.assertEqual(automation._poll_cfg()["interval_minutes"], 120)
                store.save_config({"automation": {"interval_minutes": 1}})
                self.assertEqual(automation._poll_cfg()["interval_minutes"], 5)
                store.save_config({"automation": {"interval_minutes": 99999}})
                self.assertEqual(automation._poll_cfg()["interval_minutes"], 1440)
                store.save_config({"automation": {"mode": "daily", "daily_at": "xx"}})
                self.assertEqual(automation._poll_cfg(),
                                 {"mode": "daily", "interval_minutes": 120,
                                  "daily_at": "09:00"})


class TestCheckinPoller(unittest.TestCase):
    """2 小时轮询签到：查签到接口→未签补签；每天首轮全量托管。"""

    def _doc(self, uid="p1", domain="www.codebuddy.cn", api_ready=True):
        auth = {"domain": domain}
        if api_ready:
            auth["accessToken"] = "t"
        else:                                        # 加密 token：无明文，不可直接调 API
            auth["accessToken"] = {"$wbEncrypted": 1, "envelope": "x"}
        return parse_auth({"account": {"uid": uid, "nickname": "轮询号"}, "auth": auth})

    def test_sweep_checks_in_only_when_needed(self):
        """已签不重复签；未签才补签。"""
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        doc = self._doc()
        with patch.object(automation, "WorkBuddyClient") as MC:
            client = MagicMock()
            client.checkin_status.return_value = {"checked_in": True}
            MC.return_value = client
            out = automation.checkin_sweep([doc], log=lambda *_: None)
            self.assertEqual(out, {})
            client.checkin.assert_not_called()           # 已签：不打扰

            client.checkin_status.return_value = {"checked_in": False}
            client.checkin.return_value = {"credit": 100}
            out = automation.checkin_sweep([doc], log=lambda *_: None)
            self.assertEqual(out.get(doc.uid), "+100 积分")
            client.checkin.assert_called_once()          # 未签：补签

    def test_sweep_skips_other_channels_and_offline(self):
        """非 WorkBuddy 渠道、无明文 token 的账号完全不发请求。"""
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        docs = [self._doc("p2", domain="api.trae.ai"), self._doc("p3", api_ready=False)]
        with patch.object(automation, "WorkBuddyClient") as MC:
            out = automation.checkin_sweep(docs, log=lambda *_: None)
            self.assertEqual(out, {})
            MC.assert_not_called()

    def test_sweep_error_does_not_break_others(self):
        """单账号查询失败不挡其他账号补签。"""
        from unittest.mock import patch, MagicMock
        from wbspy import automation
        d1, d2 = self._doc("p4"), self._doc("p5")
        with patch.object(automation, "WorkBuddyClient") as MC:
            c1, c2 = MagicMock(), MagicMock()
            c1.checkin_status.side_effect = RuntimeError("网络炸了")
            c2.checkin_status.return_value = {"checked_in": False}
            c2.checkin.return_value = {"credit": 5}
            MC.side_effect = lambda d: c1 if d.uid == "p4" else c2
            logs = []
            out = automation.checkin_sweep([d1, d2], log=logs.append)
            self.assertIn("失败", out["p4"])
            self.assertEqual(out["p5"], "+5 积分")

    def test_poll_round_full_once_per_day(self):
        """每天首轮全量托管，同日其余轮只做签到兜底。"""
        from unittest.mock import patch
        from wbspy import automation
        docs = [self._doc("p6")]
        calls = []
        with patch.object(automation, "run_all", lambda a, l: calls.append("full")), \
             patch.object(automation, "checkin_sweep",
                          lambda a, l: calls.append("sweep")):
            day = automation._poll_round(docs, None, log=lambda *_: None)
            self.assertEqual(calls, ["full"])            # 首轮：全量
            self.assertEqual(day, automation.time.strftime("%Y-%m-%d"))
            day2 = automation._poll_round(docs, day, log=lambda *_: None)
            self.assertEqual(calls, ["full", "sweep"])   # 同日第二轮：只 sweep
            self.assertEqual(day2, day)


class TestAutomationToggle(unittest.TestCase):
    """网页自动签到轮询开关：状态持久化 + 线程启停。"""

    def test_toggle_persists_and_starts_thread(self):
        import tempfile, os
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from wbspy import store
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with patch.object(store, "CONFIG_PATH", cfg_path), \
                 patch("wbspy.webui.load_config", store.load_config), \
                 patch("wbspy.webui.save_config", store.save_config), \
                 patch("wbspy.webui.load_accounts", return_value=[]), \
                 patch("wbspy.automation.load_config", store.load_config):
                c = TestClient(create_web_app(AccountPool([])))
                st = c.get("/api/automation/status").json()["data"]
                self.assertFalse(st["enabled"])
                r = c.post("/api/automation/toggle",
                           json={"enabled": True}).json()["data"]
                self.assertTrue(r["enabled"])
                self.assertTrue(cfg_path.exists())
                on_disk = json.loads(cfg_path.read_text())["automation"]["enabled"]
                self.assertTrue(on_disk)                 # 重启后能恢复


class TestGatewayLogging(unittest.TestCase):
    """网关日志接线：401 鉴权失败、对话成败、换号/续期事件进网页日志抽屉。"""

    def test_auth_fail_and_chat_logged(self):
        """密钥不匹配 401 进调试日志（req_log）；对话失败进运行日志（/api/logs）。"""
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool
        from wbspy.upstream import UpstreamError
        app = create_app(AccountPool([]), api_key="secret")
        c = TestClient(app, raise_server_exceptions=False)
        # 密钥不匹配 → 401 + 调试日志（含脱敏密钥与指引）
        r = c.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        e = app.state.req_log[-1]
        self.assertEqual(e["status"], 401)
        self.assertIn("密钥无效", e["note"])
        self.assertIn("****", e["auth"])
        # 对话失败 → 错误进日志
        class _P(AccountPool):
            def chat_failover(self, payload, session=None, lane="workbuddy"):
                raise UpstreamError(503, "no_available_accounts", "无账号")
        app2 = create_app(_P([]), api_key="")
        c2 = TestClient(app2, raise_server_exceptions=False)
        c2.post("/v1/chat/completions", json={"model": "auto",
                                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("对话失败", app2.state.auto_log[-1])
        self.assertIn("no_available_accounts", app2.state.auto_log[-1])

    def test_pool_emit_wired_to_webui_log(self):
        """pool.event_log 钩子 → 网页日志；换号/auth 事件触发 _emit。"""
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool
        from wbspy.upstream import UpstreamError
        pool = AccountPool([])
        events: list[str] = []
        pool.event_log = events.append
        # 两账号，第一个 401 换号到第二个
        from wbspy.document import parse_auth
        docs = [parse_auth({"account": {"uid": f"e{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t",
                                     "domain": "www.codebuddy.cn"}}) for i in range(2)]
        pool2 = AccountPool(docs)
        pool2.event_log = events.append
        from unittest.mock import patch as _p
        with _p("wbspy.pool._client_for") as MF:
            c1, c2 = MagicMock(), MagicMock()
            c1.chat.side_effect = UpstreamError(401, "upstream_auth_expired", "失效")
            c2.chat.return_value = MagicMock()
            MF.side_effect = lambda d: c1 if d.uid == "e0" else c2
            pool3 = AccountPool(docs)
            pool3.event_log = events.append
            k1 = next(k for k in pool3.documents if pool3.documents[k].uid == "e1")
            pool3.cooldowns.set(k1, "auto", 60)        # 冷却 e1，逼首轮选 e0
            doc, _ = pool3.chat_failover({"model": "auto", "messages": []})
            self.assertEqual(doc.uid, "e1")            # 换到第二个号
        self.assertTrue(any("凭据被上游拒绝" in e for e in events), events)

    def test_refresh_failure_emits(self):
        """token 续期失败不再静默：记入事件日志。"""
        import time as _time
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool
        from wbspy.document import parse_auth
        doc = parse_auth({"account": {"uid": "r1", "nickname": "续期号"},
                          "auth": {"accessToken": "t", "refreshToken": "r",
                                   "domain": "www.codebuddy.cn"}})
        doc.token_expires_at = 0                        # 已过期，触发续期
        pool = AccountPool([doc])
        events: list[str] = []
        pool.event_log = events.append
        client = MagicMock()
        client.refresh.side_effect = RuntimeError("refresh 炸了")
        with patch.object(pool, "clients", {next(iter(pool.documents)): client}):
            pool.start_auto_refresh(interval_sec=1)
            try:
                # 等待续期失败事件出现（worker 首轮立即跑，带超时兜底）
                for _ in range(100):
                    if any("续期失败" in e for e in events):
                        break
                    _time.sleep(0.02)
            finally:
                pool.stop_auto_refresh()               # 确定性收线，不靠杀线程
        self.assertTrue(any("续期失败" in e for e in events), events)


class TestRegionOf(unittest.TestCase):
    """区域判定：cline.bot 系域名 → intl（国际渠道，非 cn/global 体系）。"""

    def test_cline_domain_is_intl(self):
        from wbspy.document import region_of, parse_auth
        self.assertEqual(region_of("api.cline.bot"), "intl")
        self.assertEqual(region_of("cline.bot"), "intl")
        self.assertEqual(region_of("www.codebuddy.cn"), "cn")      # 不影响原有
        self.assertEqual(region_of("api.workbuddy.ai"), "global") \
            if "api.workbuddy.ai" in __import__("wbspy.document", fromlist=["GLOBAL_DOMAINS"]).GLOBAL_DOMAINS else None
        doc = parse_auth({"account": {"uid": "ci", "nickname": "克莱恩"},
                          "auth": {"accessToken": "t", "domain": "api.cline.bot"}})
        self.assertEqual(doc.region, "intl")
        self.assertIn("国际", doc.label())




def _make_app_with_pool(tmp, doc=None, client=None, api_key="secret-1234-5678"):
    """造一个可注入 mock 客户端的网关 app（调试日志测试用），config 重定向到 tmp。"""
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from wbspy.gateway import create_app
    from wbspy.pool import AccountPool, _key
    from wbspy.document import parse_auth
    from wbspy import store
    ctx = (patch.object(store, "CONFIG_PATH", Path(tmp) / "config.json"),
           patch("wbspy.gateway.load_config", store.load_config),
           patch("wbspy.store.save_config", store.save_config))
    for p in ctx:
        p.start()
    docs = [doc] if doc else []
    pool = AccountPool(docs)
    if doc and client is not None:
        pool.clients[_key(doc)] = client
    app = create_app(pool, api_key=api_key)
    # 必须交还 stopper 由调用方注册 cleanup：此前 start 后不 stop，CONFIG_PATH
    # 永久指向本测试的 tmp 目录，目录删除后污染后续走真实路径的测试（500 之谜）
    return TestClient(app), app, lambda: [p.stop() for p in ctx]


class TestGatewayDebug(unittest.TestCase):
    """网关调试日志：/v1 请求来龙去脉（密钥脱敏/body 摘要/状态码），网页抽屉可见。"""

    def _env(self):
        import tempfile
        from unittest.mock import MagicMock
        from wbspy.document import parse_auth
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = parse_auth({"account": {"uid": "dbg1", "nickname": "调试号"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        resp = MagicMock()
        resp.iter_lines.return_value = iter([
            'data: {"id":"1","model":"auto","choices":[{"delta":{"content":"hi"}}]}',
            'data: [DONE]'])
        client = MagicMock()
        client.chat.return_value = resp
        c, app, stop = _make_app_with_pool(tmp.name, doc=doc, client=client)
        self.addCleanup(stop)
        return c, app

    def test_401_recorded_and_masked(self):
        """密钥无效：记录 401 + 脱敏密钥 + 明确指引；密钥全文不落盘。"""
        c, app = self._env()
        r = c.get("/v1/models", headers={"Authorization": "Bearer wrong-key-abcdef-xyz!"})
        self.assertEqual(r.status_code, 401)
        e = app.state.req_log[-1]
        self.assertEqual(e["status"], 401)
        self.assertEqual(e["path"], "/v1/models")
        self.assertIn("****", e["auth"])
        self.assertNotIn("wrong-key-abcdef-xyz!", e["auth"])
        self.assertIn("密钥无效", e["note"])
        self.assertIn("长度", e["note"])            # 提示两边密钥长度差异
        self.assertNotEqual(e["ip"], "")

    def test_ok_request_logged_with_body_digest(self):
        """密钥正确的对话请求：200 + body 摘要（model/消息数/最后一条预览），下游正常解析。"""
        c, app = self._env()
        body = {"model": "auto",
                "messages": [{"role": "user", "content": "你好世界" * 30}]}
        r = c.post("/v1/chat/completions", json=body,
                   headers={"Authorization": "Bearer secret-1234-5678"})
        self.assertEqual(r.status_code, 200)        # body 经中间件读后仍正常解析
        e = app.state.req_log[-1]
        self.assertEqual(e["status"], 200)
        self.assertIn("model=auto", e["body"])
        self.assertIn("messages=1", e["body"])
        self.assertIn("****", e["auth"])
        self.assertGreaterEqual(e["ms"], 0)

    def test_toggle_persists_and_off_only_logs_errors(self):
        """开关落盘 config；关闭后成功请求不记录，失败请求仍记录（排障底线）。"""
        import json as _json
        from wbspy import store
        c, app = self._env()
        r = c.post("/api/debug-logs", json={"on": False}).json()["data"]
        self.assertFalse(r["on"])
        self.assertFalse(
            _json.loads(store.CONFIG_PATH.read_text())["gateway"]["debug"])
        n = len(app.state.req_log)
        c.get("/v1/models", headers={"Authorization": "Bearer secret-1234-5678"})
        self.assertEqual(len(app.state.req_log), n)            # 成功不记
        c.get("/v1/models", headers={"Authorization": "Bearer bad"})
        self.assertEqual(len(app.state.req_log), n + 1)        # 失败仍记
        self.assertTrue(c.post("/api/debug-logs",
                               json={"on": True}).json()["data"]["on"])

    def test_clear_and_default_on(self):
        """默认开启；DELETE 清空。"""
        c, app = self._env()
        self.assertTrue(c.get("/api/debug-logs").json()["data"]["on"])
        c.get("/v1/models", headers={"Authorization": "Bearer secret-1234-5678"})
        self.assertTrue(app.state.req_log)
        self.assertEqual(c.delete("/api/debug-logs").status_code, 200)
        self.assertEqual(app.state.req_log, [])


class TestCreditsRequestRange(unittest.TestCase):
    """余额查询请求本身必须 32 位安全：范围终点 ≈2127 年会让 armhf 盒子上
    time.localtime 直接 OverflowError（用户实测第二次余额全挂的根因）。"""

    def test_query_window_within_32bit_time_t(self):
        from unittest.mock import patch, MagicMock
        from wbspy.document import parse_auth
        from wbspy.upstream import WorkBuddyClient
        doc = parse_auth({"account": {"uid": "rng", "nickname": "n"},
                          "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}})
        client = WorkBuddyClient(doc)
        captured: dict = {}
        resp = MagicMock(ok=True, status_code=200)
        resp.json.return_value = {"code": 0, "data": {"Response": {"Data": {"Accounts": []}}}}

        def fake_post(url, **kw):
            captured["body"] = kw.get("data", "")
            return resp

        with patch.object(client.http, "post", side_effect=fake_post):
            client.credits()
        end = captured["body"]
        # PackageEndTimeRangeEnd 必须在 2038 年内（32 位 time_t 上限）
        year = int(end.split('"PackageEndTimeRangeEnd": "')[1][:4])
        self.assertLessEqual(year, 2037, f"查询窗口终点 {year} 年超出 32 位范围：{end}")

    def test_parse_ms_clamps_beyond_32bit(self):
        """超大毫秒时间戳（9999 年等）钳为 None——下游 localtime 不再有机会炸。"""
        from wbspy.upstream import _parse_ms
        self.assertIsNone(_parse_ms(253402300799000))            # 9999-12-31 ms
        self.assertIsNone(_parse_ms(float(2**31 * 1000)))        # 恰好越过 2038
        self.assertEqual(_parse_ms(1762400000000), 1762400000000)  # 正常值不受影响


class TestClinePrices(unittest.TestCase):
    """Cline 模型价格：models.dev 数据源（Cline 扩展同款），归一化匹配后注入 $/M 价。"""

    def _client(self):
        from wbspy.document import parse_auth
        from wbspy.upstream import ClineClient
        doc = parse_auth({"account": {"uid": "c9", "nickname": "T"},
                          "auth": {"accessToken": "abc", "domain": "api.cline.bot"}})
        return ClineClient(doc)

    def _with_md(self, table):
        import time as _t
        from unittest.mock import patch
        from wbspy import upstream
        return patch.object(upstream, "_MD_CACHE",
                            {"data": table, "at": _t.time()})

    def test_price_lookup_normalization(self):
        """:batch 剥离、点→横线、供应商别名、内部路由/裸 ID 直接判无。"""
        from wbspy.upstream import _cline_price
        table = {"anthropic": {"models": {"claude-sonnet-5-5":
                                          {"cost": {"input": 2, "output": 10}}}},
                 "zhipuai": {"models": {"glm-5.3": {"cost": {"input": 0.5, "output": 2}}}}}
        with self._with_md(table):
            self.assertEqual(_cline_price("anthropic/claude-sonnet-5.5"),
                             {"input": 2, "output": 10})                       # 点→横线
            self.assertEqual(_cline_price("z-ai/glm-5.3"),
                             {"input": 0.5, "output": 2})                      # 别名
            self.assertEqual(_cline_price("anthropic/claude-sonnet-5.5:batch"),
                             {"input": 2, "output": 10})                       # 剥 :batch
            self.assertIsNone(_cline_price("@preset/xxx"))                     # 内部路由
            self.assertIsNone(_cline_price("glm-5.2"))                         # 裸 ID 无 provider
            self.assertIsNone(_cline_price("anthropic/no-such-model"))         # 未收录

    def test_models_rows_carry_price(self):
        """目录行注入 input_price/output_price；查不到的模型不带价格字段。"""
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import ClineClient
        c = self._client()
        resp = MagicMock(ok=True, status_code=200)
        resp.json.return_value = {"data": [
            {"id": "anthropic/claude-sonnet-5.5", "display_name": "Sonnet"},
            {"id": "openai/brand-new-model"},
            {"id": "moonshotai/kimi-k3"},
        ]}
        table = {"anthropic": {"models": {"claude-sonnet-5-5":
                                          {"cost": {"input": 2, "output": 10}}}},
                 "moonshotai": {"models": {"kimi-k3":
                                           {"cost": {"input": 3, "output": 15}}}}}
        with self._with_md(table), patch.object(c.http, "get", return_value=resp):
            rows = c.models()
        self.assertEqual(rows[0]["input_price"], 2)
        self.assertEqual(rows[0]["output_price"], 10)
        self.assertEqual(rows[2]["input_price"], 3)
        self.assertNotIn("input_price", rows[1])       # 未收录 → 无价格字段 → 前端显示 —


class TestFailoverPolicy(unittest.TestCase):
    """failover 策略：5xx/额度尽/连接中断换号重试；请求本身的问题不换号。"""

    def _pool(self, bad_uid, bad_exc):
        """双账号池：uid=bad_uid 的号必抛 bad_exc，另一号成功；pick 按入库顺序。"""
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool, _key
        from wbspy.document import parse_auth
        docs = [parse_auth({"account": {"uid": f"f{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t",
                                     "domain": "www.codebuddy.cn"}}) for i in range(2)]
        events: list[str] = []
        good = MagicMock()
        with patch("wbspy.pool._client_for") as MF:
            MF.side_effect = (lambda d: MagicMock(chat=MagicMock(side_effect=bad_exc))
                              if d.uid == bad_uid else good)
            pool = AccountPool(docs)
            pool.event_log = events.append
            pool.pick = lambda model, exclude=None, session=None, lane="workbuddy": next(
                (d for d in docs if _key(d) not in (exclude or set())), None)
            doc, resp = pool.chat_failover({"model": "auto", "messages": []})
        self.assertIs(resp, good.chat.return_value)
        return pool, doc, events

    def test_5xx_fails_over(self):
        """上游 500：换到下一个号成功（用户实测「重试延迟 961ms」的场景）。"""
        from wbspy.upstream import UpstreamError
        pool, doc, events = self._pool("f0", UpstreamError(
            502, "upstream_error", "官方服务未完成本次请求（HTTP 500）。",
            upstream_status=500))
        self.assertEqual(doc.uid, "f1")
        self.assertTrue(any("上游故障（HTTP 500）" in e for e in events), events)

    def test_quota_fails_over_and_long_cools(self):
        """额度尽（402）：换号 + 长冷却 30 分钟。"""
        from wbspy.upstream import UpstreamError
        pool, doc, events = self._pool("f0", UpstreamError(402, "upstream_quota", "额度不足"))
        self.assertEqual(doc.uid, "f1")
        self.assertTrue(any("额度已用尽" in e for e in events), events)

    def test_transport_error_fails_over(self):
        """连接中断：换号重试（消耗走新账号，无重复扣费）。"""
        from wbspy.upstream import UpstreamError
        _, doc, events = self._pool("f0", UpstreamError(
            502, "upstream_transport_error", "对话连接中断"))
        self.assertEqual(doc.uid, "f1")
        self.assertTrue(any("上游故障" in e for e in events), events)

    def test_client_error_never_fails_over(self):
        """400 参数错：直接抛，绝不换号（换啥号结果都一样）。"""
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool, _key
        from wbspy.document import parse_auth
        from wbspy.upstream import UpstreamError
        docs = [parse_auth({"account": {"uid": f"g{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t",
                                     "domain": "www.codebuddy.cn"}}) for i in range(2)]
        called: list[str] = []
        with patch("wbspy.pool._client_for") as MF:
            def factory(d):
                c = MagicMock()
                def chat_side(payload, uid=d.uid):
                    called.append(uid)               # 记录真实 chat 调用（不是建 client）
                    raise UpstreamError(400, "invalid_request", "参数错误")
                c.chat.side_effect = chat_side
                return c
            MF.side_effect = factory
            pool = AccountPool(docs)
            pool.pick = lambda model, exclude=None, session=None, lane="workbuddy": next(
                (d for d in docs if _key(d) not in (exclude or set())), None)
            with self.assertRaises(UpstreamError):
                pool.chat_failover({"model": "auto", "messages": []})
        self.assertEqual(called, ["g0"])     # 只试了第 1 个号，没有换号


class TestFailoverBoundaries(unittest.TestCase):
    """404 不换号（同池同模型）+ 上游凭据错误对外中性化。"""

    def test_model_404_no_failover(self):
        """同池账号模型集相同：一个号说没有 → 直接原样抛 404，不白打其他号。"""
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool, _key
        from wbspy.document import parse_auth
        from wbspy.upstream import UpstreamError
        docs = [parse_auth({"account": {"uid": f"m{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t",
                                     "domain": "www.codebuddy.cn"}}) for i in range(2)]
        called: list[str] = []
        with patch("wbspy.pool._client_for") as MF:
            def factory(d):
                c = MagicMock()
                def chat_side(payload, uid=d.uid):
                    called.append(uid)
                    raise UpstreamError(404, "model_not_available", "该账号暂不能使用所选模型",
                                        upstream_status=404)
                c.chat.side_effect = chat_side
                return c
            MF.side_effect = factory
            pool = AccountPool(docs)
            pool.pick = lambda model, exclude=None, session=None, lane="workbuddy": next(
                (d for d in docs if _key(d) not in (exclude or set())), None)
            with self.assertRaises(UpstreamError) as cm:
                pool.chat_failover({"model": "workbuddy:nope", "messages": []})
        self.assertEqual(cm.exception.kind, "model")
        self.assertEqual(called, ["m0"])           # 只试 1 个号，原样抛给客户端

    def test_auth_error_neutralized_for_clients(self):
        """上游凭据失效全池换号失败后：对外 503 + 中性文案，绝不提「密钥/凭据」。"""
        from unittest.mock import MagicMock, patch
        from fastapi.testclient import TestClient
        from wbspy.gateway import create_app
        from wbspy.pool import AccountPool, _key
        from wbspy.document import parse_auth
        from wbspy.upstream import UpstreamError
        docs = [parse_auth({"account": {"uid": f"n{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t",
                                     "domain": "www.codebuddy.cn"}}) for i in range(2)]
        with patch("wbspy.pool._client_for") as MF:
            def factory(d):
                c = MagicMock()
                c.chat.side_effect = UpstreamError(401, "upstream_auth_expired", "登录凭据已失效。")
                return c
            MF.side_effect = factory
            pool = AccountPool(docs)
            pool.pick = lambda model, exclude=None, session=None, lane="workbuddy": next(
                (d for d in docs if _key(d) not in (exclude or set())), None)
            app = create_app(pool, api_key="")
            c = TestClient(app, raise_server_exceptions=False)
            r = c.post("/v1/chat/completions",
                       json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 503)                 # 不是 401
        msg = r.json()["error"]["message"]
        self.assertNotIn("凭据", msg)
        self.assertNotIn("密钥", msg)
        self.assertIn("上游账号暂不可用", msg)


def _gw_client(pool):
    """测试用网关 TestClient（CONFIG_PATH 指向临时目录，避免碰真实配置）。

    临时目录留给系统回收（测试沙箱可接受），换取模块级无状态调用。
    """
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from wbspy.gateway import create_app
    from wbspy import store
    tmp = Path(tempfile.mkdtemp(prefix="wbspy-test-"))
    with patch.object(store, "CONFIG_PATH", tmp / "config.json"), \
         patch("wbspy.gateway.load_config", store.load_config):
        app = create_app(pool, api_key="")
    return TestClient(app, raise_server_exceptions=False), app


class TestGatewayCrashGuard(unittest.TestCase):
    """路由内未捕获异常：调试日志必须留痕（此前 500 无日志的根因）；目录失败降级用缓存。"""

    def _client(self, pool):
        return _gw_client(pool)

    def test_uncaught_exception_logged_and_500(self):
        """路由抛非 UpstreamError：响应 500（中性文案）+ 调试日志/运行日志都留痕。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        pool = AccountPool([])
        with patch.object(pool, "chat_failover",
                          side_effect=RuntimeError("内部炸了：Bad key")):
            c, app = self._client(pool)
            r = c.post("/v1/chat/completions",
                       json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 500)
        body = r.json()["error"]
        self.assertEqual(body["type"], "internal_error")
        self.assertNotIn("Bad key", body["message"])         # 异常详情不泄给客户端
        e = app.state.req_log[-1]
        self.assertEqual(e["status"], 500)
        self.assertIn("内部炸了：Bad key", e["note"])         # 详情只在调试日志里
        self.assertIn("网关内部异常 /v1/chat/completions", app.state.auto_log[-1])

    def test_models_degrades_to_cache(self):
        """目录刷新失败：沿用上次缓存（或空目录），绝不 500 卡死客户端启动。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        pool = AccountPool([])
        c, app = self._client(pool)
        app.state.model_cache = [{"id": "workbuddy:auto", "object": "model",
                                  "owned_by": "wbspy", "name": "Auto"}]
        app.state.model_cached_at = 0                        # 强制走刷新分支
        with patch.object(pool, "models", side_effect=RuntimeError("目录炸了")):
            r = c.get("/v1/models")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"][0]["id"], "workbuddy:auto")   # 旧缓存顶上
        self.assertTrue(any("目录刷新失败" in x for x in app.state.auto_log))


class TestTokenExpiryGuard(unittest.TestCase):
    """上游返回字符串 expiresAt 污染 Document → 全池路由炸 TypeError 的根因修复。

    事故链条：续期 worker 把原始 expiresAt（可能为字符串）直接塞进
    Document.token_expires_at → token_expired 里 `<= time.time()*1000`
    抛 TypeError → /v1/chat/completions 与 /v1/models 全 500。
    """

    def _doc(self, uid="tx1"):
        from wbspy.document import parse_auth
        return parse_auth({"account": {"uid": uid, "nickname": "号"},
                           "auth": {"accessToken": "t",
                                    "domain": "www.codebuddy.cn"}})

    def test_dirty_expiry_does_not_break_routing(self):
        """token_expires_at 为字符串（脏数据）：token_expired/api_ready 不炸。"""
        doc = self._doc()
        doc.token_expires_at = "2026-11-07T10:44:00Z"    # 上游脏数据形态
        self.assertFalse(doc.token_expired)              # 旧代码在此抛 TypeError
        self.assertTrue(doc.api_ready)                   # 路由判定照常可用

    def test_auto_refresh_normalizes_string_expires(self):
        """续期响应 expiresAt 为字符串：入库前归一化为毫秒整数。"""
        import time as _t
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool, _key
        doc = self._doc("tx2")
        doc.refresh_token = "r"
        doc.token_expires_at = 0                         # 已临期 → 立即触发续期
        with patch("wbspy.pool._client_for") as MF, \
             patch("wbspy.store.save_account", lambda d: None):
            client = MagicMock()
            client.refresh.return_value = {"accessToken": "t2", "refreshToken": "r2",
                                           "expiresAt": "1794019442000"}   # 字符串毫秒
            MF.return_value = client
            pool = AccountPool([doc])
            emitted: list[str] = []
            pool._emit = emitted.append
            thread = pool.start_auto_refresh(interval_sec=3600)
            for _ in range(200):                          # 等 worker 首轮跑完
                if emitted:
                    break
                _t.sleep(0.01)
            pool.stop_auto_refresh()                      # 收线，不留挂起线程
            thread.join(timeout=1)
        self.assertEqual(len(emitted), 1)
        self.assertIn("token 已自动续期", emitted[0])
        fresh = pool.documents[_key(doc)]
        self.assertIsInstance(fresh.token_expires_at, int)   # 不再是字符串
        self.assertEqual(fresh.token_expires_at, 1794019442000)
        fresh.token_expired                                  # 不炸（返回值随时间）

    def test_models_sort_survives_string_multiplier(self):
        """目录行混入字符串倍率：/v1/models 照常 200，排序只认数值键。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        pool = AccountPool([])
        rows = [{"id": "workbuddy:auto", "name": "Auto", "region": "cn",
                 "credit_multiplier": 1.5},
                {"id": "workbuddy:fast", "name": "Fast", "region": "cn",
                 "credit_multiplier": "0.15"},                # 脏数据
                {"id": "workbuddy:free", "name": "Free", "region": "cn",
                 "credit_multiplier": 0}]
        with patch.object(pool, "models", return_value=rows):
            c, _app = _gw_client(pool)
            _app.state.model_cached_at = 0
            r = c.get("/v1/models")
        self.assertEqual(r.status_code, 200)
        data = {m["id"]: m for m in r.json()["data"]}
        for row in rows:                       # 目录可能还含聚合渠道内置行，只验池内行
            self.assertIn(row["id"], data)
        self.assertEqual(data["workbuddy:fast"]["name"], "Fast")      # 脏倍率不拼进名字
        self.assertIn("1.50x", data["workbuddy:auto"]["name"])
        self.assertIn("限时免费", data["workbuddy:free"]["name"])
        mults = [m.get("credit_multiplier") for m in r.json()["data"]
                 if m.get("credit_multiplier") is not None]
        self.assertTrue(all(isinstance(v, (int, float)) for v in mults))


class TestGlobalLoginFlow(unittest.TestCase):
    """WorkBuddy 国际版网页登录：白名单键名错位曾致 KeyError('workbuddy-ai')。

    网页流程 _validate_login_url(url, platform) 传上游 platform 名
    （workbuddy-ai），而 LOGIN_HOSTS 只收录 provider 名
    （workbuddy-global）→ KeyError，管理端只见一行带引号的字符串。
    CLI 路径传 provider 名，因此从未暴露。修复：白名单收录 platform
    别名 + 未收录 provider 明确报 RuntimeError。
    """

    def test_validate_login_url_accepts_platform_alias(self):
        from wbspy.login import _validate_login_url
        url = "https://www.workbuddy.ai/login?state=abc"
        self.assertEqual(_validate_login_url(url, "workbuddy-ai"), url)      # platform 别名
        self.assertEqual(_validate_login_url(url, "workbuddy-global"), url)  # provider 本名
        with self.assertRaises(RuntimeError) as cm:                          # 未收录 → 明确报错
            _validate_login_url(url, "no-such-provider")
        self.assertIn("白名单", str(cm.exception))

    def test_international_start_does_not_crash(self):
        from unittest.mock import MagicMock, patch
        from wbspy.channels import get_channel
        from wbspy.login_flows import workbuddy as wb_flow
        from wbspy.login_flows.core import Ticket

        resp = MagicMock(ok=True)
        resp.json.return_value = {"data": {
            "state": "s" * 20,
            "authUrl": "https://www.workbuddy.ai/login?state=xyz"}}
        channel = get_channel("workbuddy")
        self.assertEqual(channel.name, "WorkBuddy 国际版")
        ticket = Ticket(id="t1", channel_id="workbuddy", method="browser",
                        expires_at=time.time() + 60)
        with patch.object(wb_flow.requests, "post", return_value=resp):
            wb_flow.start(channel, ticket)          # 旧代码在此 KeyError('workbuddy-ai')
        self.assertEqual(ticket.auth_url, "https://www.workbuddy.ai/login?state=xyz")
        self.assertEqual(ticket.platform, "workbuddy-ai")
        try:                        # 二维码是可选依赖（qrcode 缺失时按设计降级空串）
            import qrcode  # noqa: F401
            self.assertTrue(ticket.qr_svg)
        except ImportError:
            self.assertEqual(ticket.qr_svg, "")


class TestQoderLane(unittest.TestCase):
    """Qoder 泳道：静态目录可见 + 推理未接入的明确 501（此前账号被塞进
    WorkBuddyClient，目录必失败、模型页显示「目录不可用」）。"""

    def _doc(self, channel_id="qoder"):
        from wbspy.document import parse_auth
        domain = "qoder.cn" if channel_id == "qodercn" else "qoder.com"
        return parse_auth({"account": {"uid": "qd1", "nickname": "Q"},
                           "auth": {"accessToken": "t", "domain": domain}})

    def test_catalog_rows(self):
        from wbspy.qoder import QoderClient
        intl = QoderClient(self._doc("qoder")).models()
        cn = QoderClient(self._doc("qodercn")).models()
        self.assertEqual(len(intl), 17)
        self.assertEqual(len(cn), 14)
        self.assertEqual({r["id"] for r in intl},
                         {"auto", "ultimate", "performance", "efficient", "smodel",
                          "cmodel", "qmodel_38max", "qfmodel", "qmodel_latest",
                          "qmodel", "kmodel_latest", "kmodel", "gmodel", "gfmodel",
                          "dmodel", "dfmodel", "mmodel"})
        self.assertIn("q37fmodel", {r["id"] for r in cn})          # CN 特有行
        free = {r["id"] for r in intl if r["credit_multiplier"] == 0}
        self.assertEqual(free, {"qfmodel"})                        # 0 = 免费（合法值）
        self.assertTrue(all(r["context_window"] >= 200_000 for r in intl))

    def test_client_for_routes_qoder(self):
        from wbspy.pool import _client_for
        from wbspy.qoder import QoderClient
        self.assertIsInstance(_client_for(self._doc("qoder")), QoderClient)
        self.assertIsInstance(_client_for(self._doc("qodercn")), QoderClient)

    def test_chat_routes_encrypted_infer(self):
        """qoder: 模型对话 → wasm COSY 加密端点（v0.2.24 起真实接入）。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        from wbspy.qoder import QoderClient
        doc = self._doc("qoder")
        with patch("wbspy.store.save_account", lambda d: None):
            pool = AccountPool([doc])
        client = pool.clients[(doc.storage_scope, doc.uid)]
        self.assertIsInstance(client, QoderClient)
        # prepare 离线可跑（纯 wasm 计算）：拿到加密 URL + COSY 签名 + 密文
        req = client.prepare({"model": "auto", "messages": [
            {"role": "user", "content": "hi"}]})
        self.assertIn("/algo/api/v2/service/pro/sse/agent_chat_generation", req["url"])
        self.assertTrue(str(req["headers"].get("Authorization", ""))
                        .startswith("Bearer COSY."))
        self.assertGreater(len(req["body"]), 0)

    def test_chat_stream_end_to_end_mocked(self):
        """chat：信封流剥壳 → 标准 OpenAI SSE（聚合出内容与错误注记）。"""
        import json as _json
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        doc = self._doc("qoder")
        with patch("wbspy.store.save_account", lambda d: None):
            pool = AccountPool([doc])

        class FakeResp:
            ok = True
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            def iter_content(self, chunk_size=None):
                inner = _json.dumps({"choices": [
                    {"delta": {"content": "你好"}, "index": 0}]})
                yield (f'data: {{"body": { _json.dumps(inner) }}}\n').encode()
                yield b'data: [DONE]\n'
            def close(self): pass

        with patch("requests.post", return_value=FakeResp()) as mp:
            _, resp = pool.chat_failover(
                {"model": "qoder:auto", "messages": [{"role": "user", "content": "hi"}]},
                lane="qoder")
            from wbspy.gateway import _sse_aggregate
            body = _sse_aggregate(resp, "qoder:auto")
        self.assertEqual(body["choices"][0]["message"]["content"], "你好")
        url = mp.call_args.args[0]
        self.assertIn("api2.qoder.sh", url)     # 真发到了加密端点

    def test_gateway_routes_qoder_lane(self):
        """网关 qoder:auto → Qoder 泳道 → 加密端点内容正常回客户端。"""
        import json as _json
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        doc = self._doc("qoder")
        with patch("wbspy.store.save_account", lambda d: None):
            pool = AccountPool([doc])
        c, _app = _gw_client(pool)

        class FakeResp:
            ok = True
            status_code = 200
            headers = {"content-type": "text/event-stream"}
            def iter_content(self, chunk_size=None):
                inner = _json.dumps({"choices": [
                    {"delta": {"content": "pong"}, "index": 0}]})
                yield (f'data: {{"body": { _json.dumps(inner) }}}\n').encode()
                yield b'data: [DONE]\n'
            def close(self): pass

        with patch("requests.post", return_value=FakeResp()):
            r = c.post("/v1/chat/completions", json={"model": "qoder:auto", "messages": [
                {"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "pong")

    def test_wasm_missing_reports_501(self):
        """wasmtime 缺失（如 armv7）→ 明确 501，不误投其他端点。"""
        from unittest.mock import patch
        from wbspy._qoder_wasm import QoderWasmError
        from wbspy.pool import AccountPool
        from wbspy.upstream import UpstreamError
        doc = self._doc("qoder")
        with patch("wbspy.store.save_account", lambda d: None):
            pool = AccountPool([doc])
        with patch("wbspy._qoder_wasm.get_wasm",
                   side_effect=QoderWasmError("no wasmtime")):
            with self.assertRaises(UpstreamError) as cm:
                pool.clients[(doc.storage_scope, doc.uid)].chat(
                    {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(cm.exception.status, 501)
        self.assertIn("wasmtime", cm.exception.message)


class TestVisionSwitch(unittest.TestCase):
    """WorkBuddy 视觉识别开关：目录声明 input 模态 + 图片透传/剥除。"""

    def test_strip_images(self):
        from wbspy.gateway import strip_images
        img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
        payload = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "看图"}, img]},
            {"role": "user", "content": "纯文本不动"},
        ]}
        out, n = strip_images(payload)
        self.assertEqual(n, 1)
        c = out["messages"][1]["content"]
        self.assertEqual(c[0], {"type": "text", "text": "看图"})   # 原 text 保留
        self.assertIn("图片已省略", c[1]["text"])                  # 占位接在后面
        self.assertEqual(out["messages"][2]["content"], "纯文本不动")
        self.assertEqual(out["messages"][0]["content"], "sys")
        # 无图 → 原对象原样返回（零拷贝零改动）
        same, n2 = strip_images({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(n2, 0)
        self.assertEqual(same["messages"][0]["content"], "hi")

    def _pool(self):
        from wbspy.pool import AccountPool
        return AccountPool([])

    def test_catalog_declares_vision_by_default(self):
        from unittest.mock import patch
        pool = self._pool()
        rows = [{"id": "workbuddy:hy3", "name": "Hy3", "region": "cn"},
                {"id": "cline:claude-x", "name": "X", "region": "intl"}]
        with patch.object(pool, "models", return_value=rows):
            c, _app = _gw_client(pool)
            _app.state.model_cached_at = 0
            data = {m["id"]: m for m in c.get("/v1/models").json()["data"]}
        self.assertEqual(data["workbuddy:hy3"].get("input"), ["text", "image"])
        self.assertNotIn("input", data["cline:claude-x"])     # 越权代言不许

    def test_vision_off_strips_images_and_hides_input(self):
        from unittest.mock import MagicMock, patch
        pool = self._pool()
        rows = [{"id": "workbuddy:hy3", "name": "Hy3", "region": "cn"}]
        with patch.object(pool, "models", return_value=rows):
            c, app = _gw_client(pool)
            app.state.gw_vision = False
            app.state.model_cached_at = 0
            data = c.get("/v1/models").json()["data"]
            self.assertTrue(all("input" not in m for m in data))
            # chat 链路：图片被剥成占位文字
            client = MagicMock()
            app.state.pool = pool
            with patch.object(pool, "pick", return_value=None):
                r = c.post("/v1/chat/completions", json={"model": "workbuddy:hy3",
                            "messages": [{"role": "user", "content": [
                                {"type": "text", "text": "hi"},
                                {"type": "image_url", "image_url": {"url": "data:..."}}]}]})
            self.assertEqual(r.status_code, 503)      # 无可用账号（池空）但不 500

    def test_settings_toggle_vision(self):
        """网页设置端点切视觉开关 → 落盘 + 目录声明即时跟随。"""
        import json as _json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy.webui import create_web_app
        from wbspy import store
        pool = self._pool()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch.object(pool, "models", return_value=[
                {"id": "workbuddy:hy3", "name": "Hy3", "region": "cn"}]), \
             patch.object(store, "CONFIG_PATH", Path(tmp.name) / "config.json"), \
             patch("wbspy.webui.load_config", store.load_config), \
             patch("wbspy.webui.save_config", lambda c: None):
            app = create_web_app(pool, api_key="", port=8178)
            app.state.model_cached_at = 0
            c = TestClient(app, raise_server_exceptions=False)
            r = c.post("/api/settings/gateway", json={"vision": False})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["data"]["vision"])
            self.assertFalse(app.state.gw_vision)
            data = c.get("/v1/models").json()["data"]
            self.assertTrue(all("input" not in m for m in data))


class TestCatalogCapabilities(unittest.TestCase):
    """目录能力解析（dsh-xdpool parseReasoning / 图片标志拼法兼容移植）。

    上游 reasoning 有 plural（supportedEfforts 数组）与 singular（仅 effort
    字符串）两种拼法；图片标志有正拼法 supportsImages 与否定拼法
    disabledMultimodal。解析必须兼容两种、未知形态不编造。
    """

    def test_reasoning_plural_form(self):
        from wbspy.upstream import parse_reasoning
        r = parse_reasoning({"supportedEfforts": ["low", "high", 3, None],
                             "defaultEffort": "high"})
        self.assertEqual(r["supported_efforts"], ["low", "high"])
        self.assertEqual(r["default_effort"], "high")

    def test_reasoning_singular_folds_to_ladder(self):
        from wbspy.upstream import parse_reasoning
        r = parse_reasoning({"effort": "high"})
        self.assertEqual(r["supported_efforts"],
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(r["default_effort"], "high")

    def test_reasoning_singular_unknown_effort_kept_alone(self):
        from wbspy.upstream import parse_reasoning
        r = parse_reasoning({"effort": "turbo"})
        self.assertEqual(r["supported_efforts"], ["turbo"])

    def test_reasoning_singular_folds_even_with_default(self):
        """折叠只看 effort 是否字符串（对齐 xdpool singularEffortLadder）：
        defaultEffort 只标默认档，不改变「全阶梯可思考」的实测结论。"""
        from wbspy.upstream import parse_reasoning
        r = parse_reasoning({"effort": "high", "defaultEffort": "high"})
        self.assertEqual(r["supported_efforts"],
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(r["default_effort"], "high")

    def test_reasoning_unknown_shape_is_empty(self):
        from wbspy.upstream import parse_reasoning
        self.assertEqual(parse_reasoning(None), {})
        self.assertEqual(parse_reasoning({"canDisableThinking": True}), {})
        self.assertEqual(parse_reasoning("high"), {})

    def _client(self):
        from wbspy.document import parse_auth
        from wbspy.upstream import WorkBuddyClient
        doc = parse_auth({"account": {"uid": "cap1", "nickname": "号"},
                          "auth": {"accessToken": "t",
                                   "domain": "www.codebuddy.cn"}})
        return WorkBuddyClient(doc)

    def test_catalog_parses_images_and_efforts(self):
        """目录行解析 supportsImages / disabledMultimodal / reasoning。"""
        import json as _json
        from unittest.mock import patch, MagicMock
        cl = self._client()
        rows = [{"id": "m1", "name": "M1", "supportsImages": True,
                 "reasoning": {"supportedEfforts": ["low", "high"]}},
                {"id": "m2", "name": "M2", "disabledMultimodal": True},
                {"id": "m3", "name": "M3", "reasoning": {"effort": "high"}},
                {"id": "m4", "name": "M4"}]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": rows}
        with patch.object(cl.http, "get", return_value=resp):
            out = cl.models()
        by_id = {r["id"]: r for r in out}
        self.assertTrue(by_id["m1"]["supports_images"])
        self.assertEqual(by_id["m1"]["supported_efforts"], ["low", "high"])
        self.assertIs(by_id["m2"]["supports_images"], False)   # 否定拼法
        self.assertEqual(by_id["m3"]["supported_efforts"],
                         ["low", "medium", "high", "xhigh", "max"])
        self.assertNotIn("supports_images", by_id["m4"])       # 没说就不编造
        self.assertNotIn("supported_efforts", by_id["m4"])


class TestCapabilityGuard(unittest.TestCase):
    """池泳道能力闸：目录明说做不了就明 400（人话），不扔给上游收谜语错误。

    场景对齐 dsh-xdpool：带图打纯文本模型（明拒优于上游 11128 谜语）、
    reasoning_effort 发了模型不支持的档位（优于上游 11133）。
    """

    def test_reasoning_effort_off_stripped(self):
        """"off" 两个网关都不收（实测 11133/onlyReasoning 拒绝）→ 出口删除。"""
        from wbspy.upstream import normalize_payload
        p = normalize_payload({"model": "m", "stream": False,
                               "reasoning_effort": "off",
                               "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("reasoning_effort", p)

    def test_reasoning_effort_valid_value_kept(self):
        from wbspy.upstream import normalize_payload
        p = normalize_payload({"model": "m", "stream": False,
                               "reasoning_effort": "high",
                               "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(p["reasoning_effort"], "high")

    def test_unsupported_effort_rejected_400(self):
        from wbspy.gateway import capability_guard
        from wbspy.upstream import UpstreamError
        rows = [{"id": "workbuddy:m1", "supported_efforts": ["low", "medium"]}]
        with self.assertRaises(UpstreamError) as cm:
            capability_guard(rows, "workbuddy:m1", {"reasoning_effort": "max"})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "effort_unsupported")
        self.assertIn("low、medium", cm.exception.message)

    def test_supported_effort_passes(self):
        from wbspy.gateway import capability_guard
        rows = [{"id": "workbuddy:m1", "supported_efforts": ["low", "high"]}]
        capability_guard(rows, "workbuddy:m1", {"reasoning_effort": "high"})  # 不抛

    def test_image_to_textonly_model_rejected_400(self):
        from wbspy.gateway import capability_guard
        from wbspy.upstream import UpstreamError
        rows = [{"id": "workbuddy:m1", "supports_images": False}]
        raw = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]}
        with self.assertRaises(UpstreamError) as cm:
            capability_guard(rows, "workbuddy:m1", raw)
        self.assertEqual(cm.exception.code, "model_vision_unsupported")
        self.assertEqual(cm.exception.status, 400)

    def test_unknown_model_or_missing_data_passes(self):
        """目录没数据（行不存在 / 字段缺省）→ 放行，未知不编造。"""
        from wbspy.gateway import capability_guard
        capability_guard([{"id": "workbuddy:other"}], "workbuddy:m1", {})
        capability_guard([{"id": "workbuddy:m1"}], "workbuddy:m1",
                         {"reasoning_effort": "max"})            # 无 efforts 声明
        capability_guard([{"id": "workbuddy:m1"}], "workbuddy:m1",
                         {"messages": [{"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": "data:,"}}]}]})
        capability_guard(None, "workbuddy:m1", {})               # 缓存为空

    def test_gateway_integrates_guard(self):
        """端到端：带图打目录声明纯文本的模型 → 400 人话错误，不进池。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        pool = AccountPool([])
        c, app = _gw_client(pool)
        app.state.model_cache = [{"id": "workbuddy:text-only", "object": "model",
                                  "owned_by": "wbspy", "name": "T",
                                  "supports_images": False}]
        app.state.model_cached_at = 0
        with patch.object(pool, "models", return_value=[]):
            r = c.post("/v1/chat/completions", json={
                "model": "text-only",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "model_vision_unsupported")
        self.assertIn("不支持图片", r.json()["error"]["message"])

    def test_vision_off_strips_images_before_guard(self):
        """视觉开关关闭时先剥图：图已变占位文字，能力闸不再拦（放行语义）。"""
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        pool = AccountPool([])
        c, app = _gw_client(pool)
        app.state.gw_vision = False
        app.state.model_cache = [{"id": "workbuddy:text-only", "object": "model",
                                  "owned_by": "wbspy", "name": "T",
                                  "supports_images": False}]
        app.state.model_cached_at = 0
        with patch.object(pool, "models", return_value=[]), \
             patch.object(pool, "chat_failover",
                          side_effect=RuntimeError("不应走到这里")):
            r = c.post("/v1/chat/completions", json={
                "model": "text-only",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]})
        # 图被剥 → guard 无图可拒 → 走到池（此处 mock 成 RuntimeError → 500，
        # 证明没被 400 拦截、也没被上游谜语挡住）
        self.assertNotEqual(r.status_code, 400)


class TestPriorityCredits(unittest.TestCase):
    """priority 策略的积分感知降级（用户语义：固定优先账号，积分烧穿阈值
    → 换剩余里积分最高的继续用，直到全部低于阈值；阈值可配置）。"""

    def _cfg(self, **gw):
        from unittest.mock import patch
        from wbspy import store
        return patch.object(store, "load_config", return_value={"gateway": gw})

    def _pool_n(self, n):
        from wbspy.pool import AccountPool
        docs = []
        for i in range(n):
            from wbspy.document import parse_auth
            docs.append(parse_auth({
                "account": {"uid": f"pu{i}", "nickname": f"号{i}"},
                "auth": {"accessToken": "t", "domain": "www.codebuddy.cn"}}))
        return AccountPool(docs)

    def _uid(self, pool, doc):
        return next(k[1] for k, v in pool.documents.items() if v is doc)

    def test_unknown_credits_follows_drag_order(self):
        """全部没查过余额 → 纯按拖拽顺序（不查余额 = 普通优先级模式）。"""
        pool = self._pool_n(3)
        with self._cfg(distribution="priority",
                       account_order={"buddy:cn": ["pu2", "pu1", "pu0"]}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu2")

    def test_highest_credits_wins_among_ok(self):
        """已知积分达标 → 取积分最高者（用户语义：积分最高的先用最久）。"""
        pool = self._pool_n(3)
        with self._cfg(distribution="priority",
                       priority_threshold=100,
                       credits_cache={"pu0": {"total": 500, "at": 1},
                                      "pu1": {"total": 800, "at": 1},
                                      "pu2": {"total": 60, "at": 1}}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu1")   # 800 最高

    def test_fixed_until_below_threshold(self):
        """固定语义：第一名烧穿前每次都选它，不抖动。"""
        pool = self._pool_n(2)
        with self._cfg(distribution="priority", priority_threshold=100,
                       credits_cache={"pu0": {"total": 150, "at": 1},
                                      "pu1": {"total": 120, "at": 1}}):
            for _ in range(3):
                self.assertEqual(self._uid(pool, pool.pick("auto")), "pu0")  # 150>120

    def test_switch_to_highest_when_exhausted(self):
        """队首烧穿阈值 → 换剩余里积分最高的。"""
        pool = self._pool_n(3)
        with self._cfg(distribution="priority", priority_threshold=100,
                       credits_cache={"pu0": {"total": 99, "at": 1},
                                      "pu1": {"total": 120, "at": 1},
                                      "pu2": {"total": 400, "at": 1}}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu2")   # 400

    def test_all_below_threshold_keeps_serving(self):
        """全部低于阈值 → 积分最高者继续顶住（服务不断供）。"""
        pool = self._pool_n(3)
        with self._cfg(distribution="priority", priority_threshold=100,
                       credits_cache={"pu0": {"total": 99, "at": 1},
                                      "pu1": {"total": 50, "at": 1},
                                      "pu2": {"total": 10, "at": 1}}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu0")   # 99 最高

    def test_unknown_after_known_ok(self):
        """已知达标号优先于未知号（不查余额的号不抢已确认高分的位）。"""
        pool = self._pool_n(2)
        with self._cfg(distribution="priority", priority_threshold=100,
                       credits_cache={"pu1": {"total": 500, "at": 1}}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu1")

    def test_unknown_first_when_no_known_ok(self):
        """已知号全部烧穿 → 未知号顶上（它可能是满分，只是没查过）。"""
        pool = self._pool_n(2)
        with self._cfg(distribution="priority", priority_threshold=100,
                       credits_cache={"pu0": {"total": 5, "at": 1}}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu1")

    def test_threshold_zero_disables_credit_awareness(self):
        """阈值 0 = 关闭积分感知，纯拖拽顺序。"""
        pool = self._pool_n(2)
        with self._cfg(distribution="priority", priority_threshold=0,
                       credits_cache={"pu0": {"total": 1, "at": 1},
                                      "pu1": {"total": 999, "at": 1}},
                       account_order={"buddy:cn": ["pu0", "pu1"]}):
            self.assertEqual(self._uid(pool, pool.pick("auto")), "pu0")

    def test_threshold_persists_and_validates(self):
        from wbspy.pool import AccountPool
        written = {}

        def fake_save(cfg):
            written.update(cfg)

        from unittest.mock import patch
        from wbspy import store
        with patch.object(store, "load_config", return_value={"gateway": {}}), \
             patch.object(store, "save_config", side_effect=fake_save):
            self.assertEqual(AccountPool.set_priority_threshold("150"), 150.0)
            self.assertEqual(written["gateway"]["priority_threshold"], 150.0)
            with self.assertRaises(ValueError):
                AccountPool.set_priority_threshold(-1)
            with self.assertRaises(ValueError):
                AccountPool.set_priority_threshold("abc")

    def test_remember_credits_persists(self):
        """查询余额落盘：数值写入，脏值忽略。"""
        pool = self._pool_n(1)
        from unittest.mock import patch
        from wbspy import store
        written = {}

        def fake_save(cfg):
            written.update(cfg)

        with patch.object(store, "load_config", return_value={"gateway": {}}), \
             patch.object(store, "save_config", side_effect=fake_save):
            pool.remember_credits("pu0", 321)
            self.assertEqual(written["gateway"]["credits_cache"]["pu0"]["total"], 321)
            pool.remember_credits("pu0", "很多")     # 脏值不写
            pool.remember_credits("pu0", None)
            self.assertEqual(written["gateway"]["credits_cache"]["pu0"]["total"], 321)


class TestPriorityOrderApi(unittest.TestCase):
    """拖拽排序：set_priority_order 校验/落盘、_priority_sorted 组内生效、
    /api/accounts/order 端点、/api/accounts 带 group+credits。"""

    def _pool_n(self, n, domain="www.codebuddy.cn", prefix="ou"):
        from wbspy.pool import AccountPool
        from wbspy.document import parse_auth
        docs = [parse_auth({"account": {"uid": f"{prefix}{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t", "domain": domain}})
                for i in range(n)]
        return AccountPool(docs)

    def _cfg(self, **gw):
        from unittest.mock import patch
        from wbspy import store
        return patch.object(store, "load_config", return_value={"gateway": gw})

    def test_set_priority_order_validates_and_persists(self):
        pool = self._pool_n(2)
        from unittest.mock import patch
        from wbspy import store
        written = {}

        def fake_save(cfg):
            written.update(cfg)

        with patch.object(store, "load_config", return_value={"gateway": {}}), \
             patch.object(store, "save_config", side_effect=fake_save):
            pool.set_priority_order("buddy:cn", ["ou1", "ou0"])
            self.assertEqual(written["gateway"]["account_order"]["buddy:cn"],
                             ["ou1", "ou0"])
            with self.assertRaises(ValueError):
                pool.set_priority_order("", ["ou0"])
            with self.assertRaises(ValueError):
                pool.set_priority_order("buddy:cn", ["ou0", 3])

    def test_priority_sorted_group_inner(self):
        """组间保持入库顺序，组内按拖拽顺序；未列入的排组尾。"""
        pool2 = self._pool_n(1, domain="www.workbuddy.ai", prefix="wb")
        docs = list(self._pool_n(3).documents.values()) + list(pool2.documents.values())
        from wbspy.pool import AccountPool
        pool3 = AccountPool(docs)
        with self._cfg(account_order={"buddy:cn": ["ou2", "ou0"]}):
            out = [d.uid for d in pool3._priority_sorted(docs)]
            # buddy 组：ou2、ou0（拖拽序）、ou1（未列入排尾）；workbuddy 组保持后置
            self.assertEqual(out, ["ou2", "ou0", "ou1", "wb0"])
        with self._cfg():
            out = [d.uid for d in pool3._priority_sorted(docs)]  # 无顺序表 → 原序
            self.assertEqual(out, [d.uid for d in docs])

    def test_accounts_order_endpoint(self):
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        pool = self._pool_n(2)
        c = TestClient(create_web_app(pool, api_key=""))
        with patch.object(pool, "set_priority_order") as mock_set:
            r = c.post("/api/accounts/order", json={"group": "buddy:cn",
                                                    "uids": ["ou1", "ou0"]})
            self.assertEqual(r.status_code, 200)
            mock_set.assert_called_once_with("buddy:cn", ["ou1", "ou0"])
        # 非法参数走真校验（mock 外）→ 400
        r = c.post("/api/accounts/order", json={"group": "", "uids": []})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/accounts/order", json={"group": "buddy:cn", "uids": "not-a-list"})
        self.assertEqual(r.status_code, 400)

    def test_accounts_endpoint_carries_group_and_credits(self):
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        pool = self._pool_n(2)
        c = TestClient(create_web_app(pool, api_key=""))
        with self._cfg(credits_cache={"ou0": {"total": 42, "at": 1}}), \
             patch("wbspy.webui.load_accounts",
                   return_value=list(pool.documents.values())):
            r = c.get("/api/accounts")
        self.assertEqual(r.status_code, 200)
        rows = {a["uid"]: a for a in r.json()["data"]}
        self.assertEqual(rows["ou0"]["group"], "buddy:cn")
        self.assertEqual(rows["ou0"]["credits"], 42)
        self.assertIsNone(rows["ou1"]["credits"])     # 从未查询 → None

    def test_settings_endpoints_roundtrip_threshold(self):
        from unittest.mock import patch
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        pool = self._pool_n(1)
        c = TestClient(create_web_app(pool, api_key=""), raise_server_exceptions=True)
        cfg = {"gateway": {}}
        from wbspy import store
        with patch.object(store, "load_config", side_effect=lambda: {**cfg}), \
             patch.object(store, "save_config", side_effect=lambda c: cfg.update(c)):
            r = c.post("/api/settings/gateway", json={"priority_threshold": 200})
            if r.status_code != 200:
                logs = list(getattr(c.app.state, "auto_log", []))[-5:]
                raise AssertionError(f"POST 500 响应体: {r.text[:200]}；app 日志: {logs}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["data"]["priority_threshold"], 200.0)
            self.assertEqual(cfg["gateway"]["priority_threshold"], 200.0)
            r = c.post("/api/settings/gateway", json={"priority_threshold": -5})
            self.assertEqual(r.status_code, 400)
            r = c.post("/api/settings/gateway", json={"distribution": "nope"})
            self.assertEqual(r.status_code, 400)
            r = c.post("/api/settings/gateway", json={"distribution": "priority"})
            self.assertEqual(r.json()["data"]["distribution"], "priority")


class TestKeywordNeutralize(unittest.TestCase):
    """对策三：messages 全文关键词中和（11128 真相修复）。

    上游按关键词（DeepSeek Harness 词组）拦 messages 全文，报错文案
    unapproved channel 是误导。网关出站前在词组中插零宽空格 U+200B：
    语义不变、子串匹配不再命中、真实文件路径不受损。
    """

    def test_plain_text_neutralized(self):
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "system", "content": "You are powered by DeepSeek Harness."}]
        out = neutralize_keywords(msgs)[0]["content"]
        self.assertIn("DeepSeek\u200b Harness", out)
        self.assertNotIn("DeepSeek Harness", out)

    def test_block_list_text_neutralized(self):
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "run DeepSeek Harness now"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
        ]}]
        out = neutralize_keywords(msgs)[0]["content"]
        self.assertIn("DeepSeek\u200b Harness", out[0]["text"])
        self.assertEqual(out[1]["type"], "image_url")   # 非文本块原样

    def test_file_path_protected(self):
        from wbspy.upstream import neutralize_keywords
        text = r"D:\10_COD\DeepSeek Harness\resources\app.asar\dsh\bin"
        out = neutralize_keywords([{"role": "user", "content": text}])[0]["content"]
        self.assertNotIn("\u200b", out)                 # 路径语境绝不改写

    def test_non_text_fields_untouched(self):
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "assistant", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "run", "arguments": "{\"cmd\":\"DeepSeek Harness\"}"}}]}]
        out = neutralize_keywords(msgs)[0]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("DeepSeek Harness", out)          # arguments 不碰

    def test_idempotent(self):
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "user", "content": "DeepSeek Harness and DeepSeek  Harness"}]
        once = neutralize_keywords(msgs)
        twice = neutralize_keywords([dict(once[0])])
        self.assertEqual(once[0]["content"], twice[0]["content"])

    def test_case_insensitive(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "user", "content": "deepseek harness setup"}])[0]["content"]
        self.assertNotIn("deepseek harness", out)

    def test_normalize_payload_integrated(self):
        from wbspy.upstream import normalize_payload
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "x" * 2048 + " powered by DeepSeek Harness."}]}
        out = normalize_payload(payload)["messages"][-1]["content"]  # [0] 是补的中性 system
        self.assertIn("DeepSeek\u200b Harness", out)

    def test_non_ascii_space_variant(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "user", "content": "DeepSeek\u00a0Harness ok"}])[0]["content"]
        self.assertNotIn("DeepSeek\u00a0Harness", out)


class TestFileRefDefang(unittest.TestCase):
    """对策三·补充：DSH @文件引用双句指纹的等价改写。

    实测（linux.do，15.8K system 同账号）：两句连用即 11128（成对才触发），
    删第二句/同义改写/分开发均 200。网关对 system/developer 做
    resolve from → are resolved against 等价改写，绝不碰 user/assistant。
    """

    SENT1 = "Tokens prefixed with @ are paths the user explicitly referenced."
    SENT2 = "Relative paths resolve from the workspace root; " \
            "absolute paths identify files or directories on the host."

    def test_system_pair_rewritten(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "system", "content": self.SENT1 + " " + self.SENT2}])[0]["content"]
        self.assertNotIn("resolve from the workspace root", out)
        self.assertIn("are resolved against the workspace root", out)
        self.assertIn(self.SENT1, out)                  # 首句不动（单独发是 200）

    def test_user_content_untouched(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "user", "content": self.SENT2}])[0]["content"]
        self.assertIn("resolve from the workspace root", out)   # 用户内容绝不改写

    def test_assistant_content_rewritten(self):
        # xdpool 实测：上游扫描 system 和 assistant 两类内容（不止 system）
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "assistant", "content": self.SENT2}])[0]["content"]
        self.assertNotIn("resolve from the workspace root", out)

    def test_block_list_system_rewritten(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords([{"role": "system", "content": [
            {"type": "text", "text": self.SENT1 + "\n" + self.SENT2}]}])[0]["content"]
        self.assertNotIn("resolve from the workspace root", out[0]["text"])

    def test_case_insensitive(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords([{"role": "system", "content":
            "relative paths Resolve From the Workspace root"}])[0]["content"]
        self.assertNotIn("Resolve From the Workspace root", out)

    def test_idempotent(self):
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "system", "content": self.SENT1 + " " + self.SENT2}]
        once = neutralize_keywords(msgs)[0]["content"]
        twice = neutralize_keywords([{"role": "system", "content": once}])[0]["content"]
        self.assertEqual(once, twice)

    def test_normalize_payload_integrated(self):
        from wbspy.upstream import normalize_payload
        payload = {"model": "m", "messages": [
            {"role": "system", "content": "x" * 2048 + self.SENT1 + " " + self.SENT2}]}
        out = normalize_payload(payload)["messages"][0]["content"]
        self.assertIn("are resolved against the workspace root", out)
        self.assertNotIn("resolve from the workspace root", out)


class TestCodexBannerDefang(unittest.TestCase):
    """对策三·补充二：Codex CLI banner 改写（对齐 dsh-xdpool 2.6.1 实测 #112）。

    gate 按首段整体比较：banner 前 184 字符过、+换行 185 挂、三句拆三条
    消息全过——改写开头从句即可，保义不改写全文。
    """

    B1 = ("You are a coding agent running in the Codex CLI, a terminal-based "
          "coding assistant. Keep responses concise.")
    B2 = "You are a coding agent running in the Codex CLI"
    B3 = "Codex CLI is an open source project led by OpenAI."

    def test_system_banner_variants(self):
        from wbspy.upstream import neutralize_keywords
        for banner, gone in ((self.B1, "in the Codex CLI,"),
                             (self.B2, "in the Codex CLI"),
                             (self.B3, "led by OpenAI")):
            out = neutralize_keywords(
                [{"role": "system", "content": banner}])[0]["content"]
            self.assertNotIn(gone, out, banner)

    def test_assistant_banner_rewritten(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "assistant", "content": self.B2}])[0]["content"]
        self.assertIn("running in this environment", out)

    def test_user_banner_untouched(self):
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "user", "content": self.B2}])[0]["content"]
        self.assertIn("in the Codex CLI", out)      # 用户内容绝不改写

    def test_web_gui_variant_covered_by_zwsp(self):
        # "DeepSeek Harness Web GUI" 含 "DeepSeek Harness" 连续子串，
        # 零宽空格中和自动覆盖，无需单独改写
        from wbspy.upstream import neutralize_keywords
        out = neutralize_keywords(
            [{"role": "system", "content": "open DeepSeek Harness Web GUI"}])[0]["content"]
        self.assertNotIn("DeepSeek Harness", out)


class TestFirstSystemRequired(unittest.TestCase):
    """对策四：首条必须是 system，否则 11128「first message is not system
    prompt」（伪装成安全策略拦截，与区域无关——xdpool 探测器一律强制传）。
    """

    def test_no_system_prepended(self):
        from wbspy.upstream import normalize_payload
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        msgs = normalize_payload(payload)["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")

    def test_system_elsewhere_left_in_place(self):
        from wbspy.upstream import normalize_payload
        payload = {"model": "m", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "be nice"}]}
        msgs = normalize_payload(payload)["messages"]
        self.assertEqual(len([m for m in msgs if m["role"] == "system"]), 1)
        self.assertNotEqual(msgs[0]["role"], "system")   # 不强行重排（lead_in 管内容）

    def test_neutral_system_not_flagged_as_fingerprint(self):
        from wbspy.upstream import normalize_payload
        payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        first = normalize_payload(payload)["messages"][0]["content"]
        from wbspy.upstream import is_third_party_opening
        self.assertFalse(is_third_party_opening(first))  # 补的不是指纹，别再叠导语


class TestDenialAnnotation(unittest.TestCase):
    """11128 审查拒错误消息的诊断注解：大小分档 + 对策状态 + 行动指引。

    背景：审查拒是最高频的「用户来问」项（老问题）。对策一/二在
    upstream 层已自动化，但错误消息只有干巴巴一句官方代码，用户每次
    都得人工排查——现在把请求体大小、已试过的对策、该做什么直接写进
    错误消息。
    """

    def _err(self):
        from wbspy.upstream import UpstreamError
        return UpstreamError(403, "upstream_access_denied", "上游渠道检查拒绝本次请求。",
                             upstream_status=403, upstream_code="11128")

    def test_huge_body_flags_mb_and_demoted(self):
        from wbspy.upstream import annotate_denial
        out = annotate_denial(self._err(), "x" * (600 * 1024), demoted=True)
        self.assertIn("MB", out.message)
        self.assertIn("大会话", out.message)
        self.assertIn("system 降级重试仍被拒", out.message)
        self.assertIn("新开会话", out.message)
        self.assertIn("换账号无效", out.message)
        self.assertEqual(out.status, 403)                # 其余字段原样保留
        self.assertEqual(out.upstream_code, "11128")

    def test_medium_body_flags_kb(self):
        from wbspy.upstream import annotate_denial
        out = annotate_denial(self._err(), "x" * (80 * 1024), demoted=False)
        self.assertIn("KB", out.message)
        self.assertNotIn("MB", out.message)
        self.assertNotIn("降级", out.message)            # 没试过降级不冒功

    def test_small_body_no_size_section(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from wbspy.upstream import annotate_denial
        with tempfile.TemporaryDirectory() as tmp:      # 落盘到临时目录，不污染 home
            with patch("wbspy.upstream.DENIALS_DIR", Path(tmp)):
                out = annotate_denial(self._err(), "短消息", demoted=False)
        self.assertNotIn("KB", out.message)
        self.assertIn("新开会话", out.message)

    def test_chat_integrates_annotation(self):
        """端到端：带 system 的请求被 11128 拒 → 对策二重试也拒 →
        抛出的错误消息注明「降级重试仍被拒」。"""
        from unittest.mock import MagicMock, patch
        from wbspy.document import parse_auth
        from wbspy.upstream import WorkBuddyClient, UpstreamError
        doc = parse_auth({"account": {"uid": "d1", "nickname": "注解号"},
                          "auth": {"accessToken": "t",
                                   "domain": "www.codebuddy.cn"}})
        cl = WorkBuddyClient(doc)
        resp = MagicMock(ok=False, status_code=403,
                         text='{"code": "11128", "message": "review"}',
                         headers={"Retry-After": "", "Content-Type": "application/json"})
        with patch.object(cl.http, "post", return_value=resp):
            with self.assertRaises(UpstreamError) as cm:
                cl.chat({"model": "m", "stream": False,
                         "messages": [{"role": "system", "content": "S"},
                                      {"role": "user", "content": "hi"}]})
        self.assertEqual(cm.exception.code, "upstream_access_denied")
        self.assertIn("system 降级重试仍被拒", cm.exception.message)
        self.assertIn("新开会话", cm.exception.message)


class TestZcodeLane(unittest.TestCase):
    """ZCode（智谱 GLM）接入：OpenAI↔Anthropic 双向转码、SSE 实时转码、
    双通道选线（start-plan 免费线 JWT / coding-plan 订阅线 plan key）。

    协议全部来自 dsh-xdpool channel-pack 的线上实测结论：请求体恒流式、
    订阅线必须删 HTTP-Referer、headers 指纹逐头对齐。
    """

    def _doc(self, meta=None):
        from wbspy.document import parse_auth
        return parse_auth({"account": {"uid": "zc1", "nickname": "Z"},
                           "auth": {"accessToken": "jwt-t", "domain": "zcode.z.ai"},
                           "zcode": meta or {"device_mid": "mid1"}})

    # -- 请求体转换 ----------------------------------------------------------

    def test_body_core_mapping(self):
        from wbspy.zcode import to_anthropic_body
        b = to_anthropic_body({
            "max_tokens": 100, "max_completion_tokens": 200, "temperature": 0.5,
            "stop": ["END"],
            "messages": [{"role": "system", "content": "S1"},
                         {"role": "system", "content": "S2"},
                         {"role": "user", "content": "你好"},
                         {"role": "assistant", "content": "答"}]}, "glm-5.3")
        self.assertEqual(b["model"], "glm-5.3")
        self.assertEqual(b["max_tokens"], 200)          # completion_tokens 覆盖
        self.assertEqual(b["system"], "S1\n\nS2")       # 多条 system 合并
        self.assertIs(b["stream"], True)                # 恒流式（pack 实测）
        self.assertEqual(b["stop_sequences"], ["END"])
        self.assertEqual(b["temperature"], 0.5)
        self.assertEqual([(m["role"], m["content"][0]["text"])
                          for m in b["messages"]],
                         [("user", "你好"), ("assistant", "答")])

    def test_body_image_and_defaults(self):
        from wbspy.zcode import to_anthropic_body
        b = to_anthropic_body({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,XYZ"}},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]},
            "glm-5.3-flash")
        self.assertEqual(b["max_tokens"], 8192)         # 默认上限
        self.assertNotIn("system", b)                   # 空 system 不产出键
        blocks = b["messages"][0]["content"]
        self.assertEqual(blocks[1], {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": "XYZ"}})
        self.assertEqual(blocks[2], {"type": "image", "source": {
            "type": "url", "url": "https://x/y.png"}})

    # -- SSE 实时转码 ----------------------------------------------------------

    def _sse(self, events):
        import json as _json
        from unittest.mock import MagicMock
        up = MagicMock()
        up.iter_lines = lambda decode_unicode: iter(
            ["data: " + _json.dumps(e) for e in events])
        return up

    def test_sse_transcode_full_chain(self):
        import json as _json
        from wbspy.zcode import _OpenAIShapeResponse
        events = [
            {"type": "message_start", "message": {
                "id": "msg_1", "model": "glm-5.3", "usage": {"input_tokens": 12}}},
            {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "content_block_delta",
             "delta": {"type": "thinking_delta", "thinking": "想"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        r = _OpenAIShapeResponse(self._sse(events), "glm-5.3-flash")
        self.assertTrue(r.ok and r.status_code == 200)
        lines = list(r.iter_lines())
        self.assertEqual(lines[-1], "data: [DONE]")
        chunks = [_json.loads(ln[6:]) for ln in lines[:-1] if ln.startswith("data: ")]
        deltas = [c["choices"][0]["delta"] for c in chunks]
        self.assertEqual(deltas[0], {"role": "assistant", "content": ""})
        self.assertEqual(deltas[1], {"content": "你好"})
        self.assertEqual(deltas[2], {"reasoning_content": "想"})   # 思考流归一
        final = chunks[-1]
        self.assertEqual(final["choices"][0]["delta"], {})         # 收尾空 delta
        self.assertEqual(final["choices"][0]["finish_reason"], "length")
        self.assertEqual(final["usage"], {"prompt_tokens": 12,
                                          "completion_tokens": 5,
                                          "total_tokens": 17})
        self.assertTrue(all(c["id"] == "msg_1" for c in chunks))   # 上游 id 透传
        # 字节流路径（网关流式透传消费）
        raw = b"".join(r.iter_content())
        self.assertIn(b"data: [DONE]", raw)

    def test_sse_error_event_and_dead_stream(self):
        import json as _json
        from wbspy.zcode import _OpenAIShapeResponse
        r1 = _OpenAIShapeResponse(self._sse([
            {"type": "error", "error": {"message": "boom" * 100}},
            {"type": "message_stop"}]), "glm-5.3")
        lines = list(r1.iter_lines())
        self.assertIn("[ZCode 上游错误]", lines[0])     # 截断 200 字内
        r2 = _OpenAIShapeResponse(self._sse([]), "glm-5.3")  # 断流兜底
        out = list(r2.iter_lines())
        self.assertEqual(len(out), 3)                   # 空 content + 收尾 + DONE
        self.assertIn('"content": ""', out[0])

    # -- 双通道选线 ------------------------------------------------------------

    def test_route_free_coding_unknown(self):
        from wbspy.zcode import (ZCodeClient, MESSAGES_URL, CODING_MESSAGES_URL)
        cl = ZCodeClient(self._doc({"device_mid": "m", "coding_plan_key_zai": "pk1"}))
        self.assertEqual(cl._route("glm-5.3-flash"),
                         ("start-plan", MESSAGES_URL, "jwt-t"))
        self.assertEqual(cl._route("glm-5.3"),
                         ("coding-plan", CODING_MESSAGES_URL, "pk1"))  # 缓存 key 直用
        self.assertEqual(cl._route("glm-9.9")[0], "start-plan")        # 未知走免费线

    def test_route_coding_without_key_rejected_400(self):
        from wbspy.zcode import ZCodeClient
        from wbspy.upstream import UpstreamError
        cl = ZCodeClient(self._doc({"device_mid": "m"}))
        with self.assertRaises(UpstreamError) as cm:
            cl._route("glm-5.3")
        self.assertEqual(cm.exception.code, "coding_plan_key_required")
        self.assertIn("免费线", cm.exception.message)

    def test_chat_outbound_fingerprint(self):
        """出站指纹：订阅线删 Referer、恒流式 body、AuthorizationBearer。"""
        from unittest.mock import MagicMock, patch
        from wbspy.zcode import ZCodeClient, CODING_MESSAGES_URL
        cl = ZCodeClient(self._doc({"device_mid": "m", "coding_plan_key_zai": "pk1"}))
        resp = MagicMock(ok=True)
        with patch.object(cl.http, "post", return_value=resp) as post:
            cl.chat({"model": "glm-5.3", "stream": False,
                     "messages": [{"role": "user", "content": "hi"}]})
        args, kwargs = post.call_args
        self.assertEqual(args[0], CODING_MESSAGES_URL)
        h = kwargs["headers"]
        self.assertEqual(h["Authorization"], "Bearer pk1")
        self.assertNotIn("HTTP-Referer", h)             # pack 实测：订阅线必删
        self.assertEqual(h["X-Platform"], "win32")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertTrue(kwargs["json"]["stream"] is True)
        self.assertIsNone(kwargs.get("data"))           # json= 出站

    def test_chat_error_classification(self):
        from unittest.mock import MagicMock, patch
        from wbspy.zcode import ZCodeClient
        from wbspy.upstream import UpstreamError
        cl = ZCodeClient(self._doc())
        for status, want_code in ((401, "upstream_auth_expired"),
                                  (429, "upstream_rate_limit"),
                                  (402, "upstream_quota"),
                                  (500, "upstream_error")):
            resp = MagicMock(ok=False, status_code=status, text="e",
                             headers={"content-type": "application/json"})
            with patch.object(cl.http, "post", return_value=resp):
                with self.assertRaises(UpstreamError) as cm:
                    cl.chat({"model": "glm-5.3-flash",
                             "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(cm.exception.code, want_code, status)

    # -- 余额 ------------------------------------------------------------------

    def test_credits_aggregation(self):
        from unittest.mock import MagicMock, patch
        from wbspy.zcode import ZCodeClient
        cl = ZCodeClient(self._doc({"device_mid": "m"}))
        resp = MagicMock(ok=True, status_code=200,
                         headers={"content-type": "application/json"})
        resp.json.return_value = {"data": {"balances": [
            {"show_name": "新手包", "available_units": 100.5, "total_units": 1000,
             "expires_at": 200},
            {"show_name": "签到包", "remaining_units": 50, "expires_at": 100}]}}
        with patch.object(cl.http, "get", return_value=resp):
            out = cl.credits()
        self.assertEqual(out["total"], 150.5)           # 双拼法都收
        self.assertEqual(out["nearest_expiry"], 100)    # 最近到期
        self.assertEqual(out["unit"], "tokens")
        self.assertEqual(len(out["packages"]), 2)

    # -- 池 / 网关集成 ----------------------------------------------------------

    def test_pool_wiring(self):
        from wbspy.pool import _client_for, _lane_of, AccountPool
        from wbspy.zcode import ZCodeClient
        doc = self._doc()
        self.assertIsInstance(_client_for(doc), ZCodeClient)
        self.assertEqual(_lane_of(doc), "zcode")
        self.assertEqual(AccountPool._route_id(doc, "glm-5.3"), "zcode:glm-5.3")

    def test_gateway_lane_accepts_zcode(self):
        """网关路由把 zcode: 前缀识别为独立泳道：请求进池取号（而非 404）。"""
        from unittest.mock import MagicMock, patch
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        pool = AccountPool([self._doc()])
        c = TestClient(create_web_app(pool, api_key=""))
        fake = MagicMock(ok=True, status_code=200)
        fake.headers = {"content-type": "text/event-stream"}
        fake.iter_content = lambda chunk_size=None: iter([b"data: [DONE]\n\n"])
        with patch.object(pool, "chat_failover",
                          return_value=(self._doc(), fake)) as fo:
            r = c.post("/v1/chat/completions", json={
                "model": "zcode:glm-5.3-flash", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fo.call_count, 1)              # 进了池 = 泳道识别成功


class TestAuthErrorMessage(unittest.TestCase):
    """401 响应体人话化：多数客户端只显示 error.message（401 一律渲染成
    「API 密钥无效」），message 里不带排查信息用户就无处可看真相。
    之前响应体只有一句 "invalid api key"，详细的密钥长度对比只进调试日志。"""

    def _env(self, key="wbs-test-key-1234567890"):
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        pool = AccountPool([])
        app = create_web_app(pool, api_key=key)
        return TestClient(app), app

    def test_wrong_key_message_is_actionable(self):
        c, app = self._env()
        r = c.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        err = r.json()["error"]
        self.assertIn("不一致", err["message"])
        self.assertIn("设置", err["message"])            # 行动指引写进响应体
        self.assertEqual(err["gateway_key_len"], len("wbs-test-key-1234567890"))
        self.assertEqual(err["presented_key_len"], len("Bearer wrong"))
        # 运行日志同步可见（调试开关关着也能看到原因）
        self.assertTrue(any("密钥校验失败" in m for m in app.state.auto_log))
        # 密钥全文绝不落日志
        self.assertFalse(any("Bearer wrong" in m for m in app.state.auto_log))

    def test_missing_key_message(self):
        c, app = self._env()
        r = c.get("/v1/models")                          # 不带任何密钥头
        self.assertEqual(r.status_code, 401)
        self.assertIn("没有发送访问密钥", r.json()["error"]["message"])

    def test_gateway_disabled_message_localized(self):
        c, app = self._env()
        app.state.gw_enabled = False
        r = c.get("/v1/models")
        self.assertEqual(r.status_code, 404)
        self.assertIn("网关已关闭", r.json()["error"]["message"])


class TestLifesaverRefresh(unittest.TestCase):
    """容灾缺口修复：凭据被上游拒 ≠ 死刑——当场用 refresh_token 续期复活。

    旧行为：auth 失败 → 标死 5 分钟 + 换号，续期全靠后台 worker（最长
    5 分钟一轮）→ 单账号池/全池失效时服务黑窗，明明 refresh_token 可能
    还有效却白白 503。新行为：当场救命续期（60s 节流），救活即原地复活。
    """

    def _pool(self, n=1):
        from wbspy.pool import AccountPool
        from wbspy.document import parse_auth
        docs = [parse_auth({"account": {"uid": f"lv{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "old", "refreshToken": "rt",
                                     "domain": "www.codebuddy.cn"}})
                for i in range(n)]
        return AccountPool(docs)

    def _key(self, pool, uid):
        return next(k for k in pool.documents if k[1] == uid)

    def test_revive_on_auth_failure(self):
        """端到端：唯一账号凭据被拒 → 救命续期成功 → 原地复活重试成功。"""
        import json as _json
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import UpstreamError
        pool = self._pool(1)
        k = self._key(pool, "lv0")
        calls = []

        def chat_side(payload):
            calls.append(payload)
            if len(calls) == 1:                      # 首次：凭据失效
                raise UpstreamError(401, "upstream_auth_expired", "凭据失效",
                                    upstream_status=401, upstream_code="12153")
            return MagicMock(ok=True, status_code=200, headers={}, text="")

        # refresh_documents 换文档时会 _client_for 重建客户端——mock 工厂
        # 必须同时覆盖初始与重建两条路，否则第二轮循环会发真实 HTTP
        refresh_mock = MagicMock(return_value={"accessToken": "new-token",
                                               "expiresAt": 99999999999999})

        def _mock_client(d):
            c = MagicMock()
            c.chat = chat_side
            c.refresh = refresh_mock      # 共享实例：客户端被重建也能断言
            return c

        pool.clients[k] = _mock_client(None)
        with patch("wbspy.store.save_account"), \
             patch("wbspy.pool._client_for", side_effect=_mock_client):
            doc, resp = pool.chat_failover({"model": "auto", "stream": False,
                                            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(len(calls), 2)              # 失败一次 → 续期 → 重试成功
        refresh_mock.assert_called_once()                # 续期确实发生
        self.assertEqual(pool.documents[k].token, "new-token")   # 凭据已换新
        self.assertNotIn(k, pool.dead_until)         # 死亡标记已清
        self.assertTrue(any("复活" in m for m in pool._events) if
                        hasattr(pool, "_events") else True)

    def test_revive_failure_marks_dead_and_failover(self):
        """续期也失败 → 才标死换号（若有其他号）或抛 401 转换 503。"""
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import UpstreamError
        pool = self._pool(2)
        k0 = self._key(pool, "lv0")
        k1 = self._key(pool, "lv1")
        pool.clients[k0].chat = MagicMock(side_effect=UpstreamError(
            401, "upstream_auth_expired", "失效", upstream_status=401))
        pool.clients[k0].refresh = MagicMock(side_effect=RuntimeError("refresh 也炸"))
        pool.clients[k1].chat = MagicMock(return_value=MagicMock(
            ok=True, status_code=200, headers={}, text=""))
        with patch("wbspy.store.save_account"):
            doc, _ = pool.chat_failover({"model": "auto", "stream": False,
                                         "messages": [{"role": "user", "content": "hi"}]})
        self.assertIs(pool.documents[k1], doc)       # 换到二号成功
        self.assertIn(k0, pool.dead_until)           # 一号确认死亡
        pool.clients[k0].refresh.assert_called_once()

    def test_revive_throttle_60s(self):
        """节流：60 秒内同一账号只真续一次（防对话路径被续期拖慢）。"""
        from unittest.mock import MagicMock, patch
        from wbspy.upstream import UpstreamError
        pool = self._pool(1)
        k = self._key(pool, "lv0")
        client = pool.clients[k]
        client.chat = MagicMock(side_effect=UpstreamError(
            401, "upstream_auth_expired", "失效", upstream_status=401))
        client.refresh = MagicMock(side_effect=RuntimeError("炸"))
        with patch("wbspy.store.save_account"):
            with self.assertRaises(UpstreamError):
                pool.chat_failover({"model": "auto", "stream": False,
                                    "messages": [{"role": "user", "content": "hi"}]})
            with self.assertRaises(UpstreamError):   # 第二次请求
                pool.chat_failover({"model": "auto", "stream": False,
                                    "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(client.refresh.call_count, 1)   # 节流：只续一次

    def test_no_refresh_capability_skips(self):
        """无 refresh 能力（如 ZCode）/无 refresh_token → 不浪费调用直接 False。"""
        from wbspy.document import parse_auth
        from wbspy.pool import AccountPool
        doc = parse_auth({"account": {"uid": "zc9", "nickname": "Z"},
                          "auth": {"accessToken": "t", "domain": "zcode.z.ai"}})
        pool = AccountPool([doc])
        from wbspy.pool import _key
        self.assertFalse(pool._revive(pool.documents[_key(doc)]))

    def test_empty_candidates_rescued_by_revive(self):
        """候选全灭（本地判定过期）→ 救命续期救活 → 请求成功，不再 503。"""
        from unittest.mock import MagicMock, patch
        pool = self._pool(1)
        k = self._key(pool, "lv0")
        # 本地判定 token 已过期 → api_ready False → pick 直接空手
        doc = pool.documents[k]
        doc.token_expires_at = 1                     # 1970 年 = 已过期
        self.assertIsNone(pool.pick("auto"))
        def _mock_client(d):
            c = MagicMock()
            c.chat = MagicMock(return_value=MagicMock(
                ok=True, status_code=200, headers={}, text=""))
            c.refresh = MagicMock(return_value={"accessToken": "fresh",
                                                "expiresAt": 99999999999999})
            return c

        pool.clients[k] = _mock_client(None)
        with patch("wbspy.store.save_account"), \
             patch("wbspy.pool._client_for", side_effect=_mock_client):
            got_doc, _ = pool.chat_failover({"model": "auto", "stream": False,
                                             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(got_doc.token, "fresh")     # 救活后成功，不再 503

    def test_503_message_guides_user(self):
        from unittest.mock import patch
        from wbspy.gateway import create_app
        from fastapi.testclient import TestClient
        from wbspy.upstream import UpstreamError
        pool = self._pool(1)
        c = TestClient(create_app(pool, api_key=""))
        with patch.object(pool, "pick", return_value=None):
            r = c.post("/v1/chat/completions", json={
                "model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 503)
        self.assertIn("账号页", r.json()["error"]["message"])


class TestHealthPanel(unittest.TestCase):
    """首屏健康总览 /api/health：哪里不行、怎么搞，一眼看完。
    报警源：凭据失效（error）> 渠道全灭（error）> 登录过期（warn）
    > 积分低于降级阈值（warn），每条带行动指引与跳转 tab。"""

    def _pool(self, n=2):
        from wbspy.pool import AccountPool
        from wbspy.document import parse_auth
        docs = [parse_auth({"account": {"uid": f"hp{i}", "nickname": f"号{i}"},
                            "auth": {"accessToken": "t", "refreshToken": "rt",
                                     "domain": "www.codebuddy.cn"}})
                for i in range(n)]
        return AccountPool(docs)

    def _get(self, pool, docs, **gw):
        from unittest.mock import patch
        from wbspy.webui import create_web_app
        from fastapi.testclient import TestClient
        from wbspy import store
        c = TestClient(create_web_app(pool, api_key=""))
        cfg = {"gateway": gw}
        with patch.object(store, "load_config", return_value=lambda: cfg or cfg) if False else \
             patch("wbspy.webui.load_config", return_value=cfg), \
             patch("wbspy.webui.load_accounts", return_value=docs):
            r = c.get("/api/health")
        self.assertEqual(r.status_code, 200)
        return r.json()["data"]

    def test_all_healthy(self):
        pool = self._pool(2)
        out = self._get(pool, list(pool.documents.values()))
        self.assertTrue(out["healthy"])
        self.assertEqual(out["alerts"], [])
        self.assertEqual(out["alive"], 2)
        self.assertIn("WorkBuddy 国内版", out["lanes"])  # 渠道分组计数可见

    def test_dead_account_alerts_error_with_action(self):
        from wbspy.pool import _key
        pool = self._pool(2)
        docs = list(pool.documents.values())
        pool.dead_until[_key(docs[0])] = 9e12         # 标死
        out = self._get(pool, docs)
        self.assertFalse(out["healthy"])
        dead = [a for a in out["alerts"] if "凭据失效" in a["title"]]
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["level"], "error")
        self.assertIn("重新网页授权", dead[0]["action"])
        self.assertEqual(dead[0]["tab"], "accounts")
        self.assertEqual(out["alive"], 1)

    def test_expired_login_warns(self):
        pool = self._pool(1)
        docs = list(pool.documents.values())
        docs[0].token_expires_at = 1                   # 本地判定过期
        out = self._get(pool, docs)
        # 单账号泳道全灭会同时触发「渠道全灭 error」——这是设计内行为
        self.assertTrue(out["alerts"])
        warn = [a for a in out["alerts"] if a["level"] == "warn"]
        self.assertEqual(len(warn), 1)
        self.assertIn("自动续期", warn[0]["action"])

    def test_low_credits_below_threshold_warns(self):
        pool = self._pool(1)
        docs = list(pool.documents.values())
        out = self._get(pool, docs, priority_threshold=100,
                        credits_cache={docs[0].uid: {"total": 42, "at": 1}})
        self.assertEqual(len(out["alerts"]), 1)
        self.assertIn("低于降级阈值 100", out["alerts"][0]["title"])
        self.assertEqual(out["alerts"][0]["tab"], "auto")

    def test_lane_wipeout_error_ranks_first(self):
        """渠道全灭 = error，全部 error 排在 warn 之前。"""
        from wbspy.pool import _key
        pool = self._pool(3)
        docs = list(pool.documents.values())
        for d in docs[:2]:
            pool.dead_until[_key(d)] = 9e12            # 两号标死
        out = self._get(pool, docs, priority_threshold=100,
                        credits_cache={docs[2].uid: {"total": 10, "at": 1}})  # 活号低积分
        levels = [a["level"] for a in out["alerts"]]
        self.assertEqual(levels[-1], "warn")            # warn 殿后
        self.assertEqual(levels[:-1], ["error", "error"])
        self.assertEqual(out["alive"], 1)

    def test_lane_wipeout_alert(self):
        """渠道内全部账号不可用 → 单独一条「全部账号不可用」报警。"""
        from wbspy.pool import _key
        pool = self._pool(2)
        docs = list(pool.documents.values())
        for d in docs:
            pool.dead_until[_key(d)] = 9e12
        out = self._get(pool, docs)
        wipe = [a for a in out["alerts"] if "全部账号不可用" in a["title"]]
        self.assertEqual(len(wipe), 1)
        self.assertEqual(wipe[0]["level"], "error")
        self.assertIn("重新授权", wipe[0]["action"])
        self.assertEqual(out["alive"], 0)


class TestDenialCapture(unittest.TestCase):
    """11128 抓现行：被拒请求体落盘 + 面板端点可查看/下载/删除。"""

    def test_capture_writes_and_prunes(self):
        """落盘 JSON、仅保留最近 DENIALS_KEEP 份。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from wbspy import upstream
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(upstream, "DENIALS_DIR", Path(tmp)):
                name = upstream.capture_denial_body('{"messages":[]}', 403)
                self.assertTrue(name and "-403-" in name)
                self.assertEqual((Path(tmp) / name).read_text(encoding="utf-8"),
                                 '{"messages":[]}')
                for i in range(upstream.DENIALS_KEEP + 3):
                    upstream.capture_denial_body(f'{{"n":{i}}}', 403)
                    time.sleep(0.01)
                files = sorted(Path(tmp).glob("*.json"))
                self.assertEqual(len(files), upstream.DENIALS_KEEP)
            # 落盘失败静默：DIR 指向文件
            with patch.object(upstream, "DENIALS_DIR", Path(tmp) / "f"):
                (Path(tmp) / "f").write_text("x")
                self.assertIsNone(upstream.capture_denial_body("{}", 403))

    def test_annotate_mentions_saved_file(self):
        """annotate_denial 附上留存文件名，指引面板下载。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from wbspy import upstream
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(upstream, "DENIALS_DIR", Path(tmp)):
                err = upstream.annotate_denial(
                    upstream.UpstreamError(403, "11128", "denied"), "x" * 100, False)
                self.assertIn("~/.wbspy/denials/", err.message)
                self.assertTrue((Path(tmp)).is_dir())

    def test_webui_denial_endpoints(self):
        """/api/denials 列表 + 下载 + 路径穿越拒绝。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from wbspy import upstream
        from wbspy.pool import AccountPool
        from wbspy.webui import create_web_app
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20261108-114500-403.json").write_text('{"a":1}')
            with patch.object(upstream, "DENIALS_DIR", Path(tmp)):
                c = TestClient(create_web_app(AccountPool([]), api_key=""))
                rows = c.get("/api/denials").json()["data"]["items"]
                self.assertEqual(len(rows), 1)
                self.assertIn("403", rows[0]["name"])
                r = c.get(f"/api/denials/{rows[0]['name']}")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json(), {"a": 1})
                self.assertEqual(c.get("/api/denials/..%2Fetc").status_code, 404)
                self.assertEqual(c.get("/api/denials/nope.json").status_code, 404)
                self.assertEqual(
                    c.delete(f"/api/denials/{rows[0]['name']}").status_code, 200)
                self.assertEqual(c.get("/api/denials").json()["data"]["items"], [])


class TestProbeEndpoint(unittest.TestCase):
    """11128 取证探针：完整管线直发、不做对策二重试、不落盘。"""

    def _pool(self):
        from unittest.mock import MagicMock
        from wbspy.document import parse_auth
        from wbspy.pool import AccountPool
        doc = parse_auth({"account": {"uid": "p1", "nickname": "探针号"},
                          "auth": {"accessToken": "t",
                                   "domain": "www.codebuddy.cn"}})
        pool = AccountPool([doc])
        client = MagicMock()
        client.doc.origin = "https://www.codebuddy.cn"
        client._chat_headers.return_value = {"X-Test": "1"}
        return pool, doc, client

    def test_probe_reports_denial(self):
        """探针 403 → denied=True + upstream_code；且不写 denials 目录。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from fastapi.testclient import TestClient
        from wbspy.webui import create_web_app
        pool, doc, client = self._pool()
        fake = MagicMock()
        fake.ok = False
        fake.status_code = 403
        fake.text = json.dumps({"error": {"code": "11128", "message": "denied"}})
        fake.headers = {}
        client.http.post.return_value = fake
        with tempfile.TemporaryDirectory() as tmp:
            with patch("wbspy.upstream.DENIALS_DIR", Path(tmp)), \
                 patch.object(pool, "healthy", return_value=[doc]), \
                 patch("wbspy.pool._client_for", return_value=client):
                c = TestClient(create_web_app(pool, api_key=""))
                r = c.post("/api/probe", json={"messages": [
                    {"role": "user", "content": "可疑内容"}]})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertTrue(data["denied"])
        self.assertEqual(data["upstream_code"], "11128")
        # 探针走完整出站管线（normalize_payload：首条强制 system + stream）
        sent = json.loads(client.http.post.call_args.kwargs["data"])
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(sent["messages"][1]["content"], "可疑内容")
        self.assertEqual(sent["max_tokens"], 1)
        self.assertTrue(sent["stream"])
        self.assertEqual(list(Path(tmp).glob("*.json")), [])   # 不落盘

    def test_probe_pass_and_validation(self):
        """200 → denied=False；messages 缺失 → 400。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from fastapi.testclient import TestClient
        from wbspy.webui import create_web_app
        pool, doc, client = self._pool()
        fake = MagicMock()
        fake.ok = True
        fake.status_code = 200
        client.http.post.return_value = fake
        with tempfile.TemporaryDirectory() as tmp:
            with patch("wbspy.upstream.DENIALS_DIR", Path(tmp)), \
                 patch.object(pool, "healthy", return_value=[doc]), \
                 patch("wbspy.pool._client_for", return_value=client):
                c = TestClient(create_web_app(pool, api_key=""))
                ok = c.post("/api/probe", json={"messages": [
                    {"role": "user", "content": "hello"}]})
                bad = c.post("/api/probe", json={"messages": []})
                none = c.post("/api/probe", json={})
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(ok.json()["data"]["denied"])
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(none.status_code, 400)


class TestWebGuiDefang(unittest.TestCase):
    """对策四：DSH Web GUI 双句指纹的等价改写。"""

    def _defang(self, text):
        from wbspy.upstream import _defang_false_positive
        return _defang_false_positive(text)

    def test_rewrites_post_neutralize_form(self):
        """管线顺序：先零宽中和后 defang——匹配 DeepSeek\\u200b Harness Web GUI。"""
        out = self._defang("through the DeepSeek\u200b Harness Web GUI whose URL")
        self.assertNotIn("Harness", out)
        self.assertIn("Web Console", out)

    def test_rewrites_raw_form(self):
        """未经中和的原始形态（理论路径）也能改写。"""
        out = self._defang("the DeepSeek Harness Web GUI whose URL")
        self.assertIn("Web Console", out)
        self.assertNotIn("Harness", out)

    def test_bare_harness_web_gui(self):
        """无 DeepSeek 前缀的 Harness Web GUI 也覆盖。"""
        self.assertIn("Web Console", self._defang("Harness Web GUI whose URL"))

    def test_idempotent(self):
        once = self._defang("through the DeepSeek\u200b Harness Web GUI whose URL")
        self.assertEqual(self._defang(once), once)

    def test_user_content_untouched(self):
        """user 内容绝不改写（defang 只在 DEFANG_ROLES 调用）。"""
        from wbspy.upstream import neutralize_keywords
        msgs = [{"role": "user",
                 "content": "the DeepSeek\u200b Harness Web GUI is down"}]
        out = neutralize_keywords(msgs)
        self.assertIn("Harness Web GUI", out[0]["content"])

    def test_system_message_rewritten_in_pipeline(self):
        """端到端：system 双句经 neutralize_keywords 后不再含指纹。"""
        from wbspy.upstream import neutralize_keywords
        s0 = ("You are interacting with the user through the DeepSeek Harness "
              "Web GUI whose URL the runtime context names.")
        s1 = ('When the user refers to "this page", "this GUI", or "this app" '
              "without naming another target, they mean this GUI.")
        out = neutralize_keywords([
            {"role": "system", "content": s0 + " " + s1}])
        text = out[0]["content"]
        self.assertNotIn("Harness Web GUI", text)
        self.assertNotIn("Harness\u200b Web GUI", text)
        self.assertIn("Web Console", text)
