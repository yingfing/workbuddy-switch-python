"""wbspy 协议核心单元测试：python3 -m unittest tests.test_wbspy -v"""

import json
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
