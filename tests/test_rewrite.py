"""Offline request-contract tests; no credentials or paid APIs are used."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI, RateLimitError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import rewrite


class RewriteTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.clients = []
        self.status = 200
        self.content = "  rewritten script  "
        self.finish_reason = "stop"
        self.patch = patch.object(rewrite, "OpenAI", side_effect=self.make_client)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.close_clients)

    def close_clients(self):
        for client in self.clients:
            client.close()

    def make_client(self, **kwargs):
        client = OpenAI(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(self.respond))
        )
        self.clients.append(client)
        return client

    def respond(self, request):
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": self.requests[-1]["model"],
            "choices": [{"index": 0, "finish_reason": self.finish_reason,
                         "message": {"role": "assistant", "content": self.content}}],
        })

    def run_copy(self, model="deepseek-flash", base_url="https://api.deepseek.com/v1"):
        return rewrite.rewrite_copy("source script", "bakery", "", {"llm": {
            "api_key": "offline-test-only", "base_url": base_url, "model": model,
        }})

    def test_official_flash_disables_thinking_and_returns_only_copy(self):
        self.assertEqual(self.run_copy(), "rewritten script")
        self.assertEqual(self.requests[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_content", self.requests[0])

    def test_missing_model_uses_current_default(self):
        self.run_copy(model="")
        self.assertEqual(self.requests[0]["model"], "deepseek-flash")

    def test_official_pro_keeps_selected_model_and_disables_thinking(self):
        self.run_copy(model="deepseek-v4-pro")
        self.assertEqual(self.requests[0]["model"], "deepseek-v4-pro")
        self.assertEqual(self.requests[0]["thinking"], {"type": "disabled"})

    def test_custom_endpoint_is_not_given_vendor_specific_parameters(self):
        self.run_copy(model="custom-model", base_url="https://example.invalid/v1")
        self.assertEqual(self.requests[0]["model"], "custom-model")
        self.assertNotIn("thinking", self.requests[0])

    def test_similar_domain_does_not_receive_deepseek_parameters(self):
        self.run_copy(base_url="https://api.deepseek.com.example.invalid/v1")
        self.assertNotIn("thinking", self.requests[0])

    def test_truncated_copy_cannot_be_used_as_a_finished_script(self):
        self.finish_reason = "length"
        with self.assertRaisesRegex(RuntimeError, "截断"):
            self.run_copy()

    def test_empty_copy_fails(self):
        self.content = "   "
        with self.assertRaisesRegex(RuntimeError, "空文案"):
            self.run_copy()

    def test_unverified_generation_speed_is_blocked_before_voice(self):
        for claim in ("上传你的视频，几分钟出一条。", "只需十分钟就能生成一百条视频。"):
            self.content = claim
            with self.subTest(claim=claim), self.assertRaisesRegex(RuntimeError, "承诺"):
                self.run_copy()

    def test_plain_process_description_does_not_require_a_claim(self):
        self.content = "上传本人视频和录音，检查口播稿，再生成配音和口型。"
        self.assertEqual(self.run_copy(), self.content)

    def test_explicit_non_guarantee_is_not_mistaken_for_a_promise(self):
        self.content = "工具不保证涨粉，也不能保证盈利，更不承诺几分钟出一条。"
        self.assertEqual(self.run_copy(), self.content)

    def test_prompt_separates_source_from_business_facts_and_does_not_pad_time(self):
        self.run_copy()
        prompt = self.requests[0]["messages"][0]["content"]
        self.assertIn("不是用户产品的事实", prompt)
        self.assertIn("不凑时长", prompt)
        self.assertNotIn("时长与原文相近", prompt)

    def test_missing_credentials_does_not_send_a_request(self):
        with self.assertRaises(RuntimeError):
            rewrite.rewrite_copy("source", "bakery", "", {"llm": {}})
        self.assertEqual(self.requests, [])

    def test_rate_limit_does_not_automatically_repeat_generation(self):
        self.status = 429
        with self.assertRaises(RateLimitError):
            self.run_copy()
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
