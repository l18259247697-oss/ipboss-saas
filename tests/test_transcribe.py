"""ASR contract, source provenance and paid-call recovery without network."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import transcribe
from services.videoretalk import PendingTask


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.audio = self.root / "input.wav"
        self.audio.write_bytes(b"offline-audio")
        self.state = self.root / "asr.json"
        self.requests = []
        self.status = 200
        self.timeout = False
        self.connect_error = False
        self.content = "文案生成，查看进度条。"
        self.native = False
        self.complete = True
        self.urls = []
        self.cfg = {"api_key": "offline-secret", "model": transcribe.MODEL}
        self.probe = stack.enter_context(patch.object(transcribe.media, "probe", return_value={"duration": 84.0}))
        real_client = httpx.Client
        stack.enter_context(patch.object(transcribe.httpx, "Client", side_effect=lambda **kw: real_client(
            **kw, transport=httpx.MockTransport(self.respond))))

    def respond(self, request):
        if self.connect_error:
            raise httpx.ConnectError("dns failed", request=request)
        self.requests.append(json.loads(request.content))
        self.urls.append(str(request.url))
        self.assertEqual(json.loads(self.state.read_text())["status"], "submitting")
        if self.timeout:
            raise httpx.ReadTimeout("unknown", request=request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"code": "ModelNotFound", "request_id": "rejected-request",
                                                     "message": "do not retain offline-secret or audio"})
        if self.native:
            return httpx.Response(self.status, json={
                "request_id": "native-request", "usage": {"duration": 84},
                "output": {"text": self.content, "sentence": {"sentence_end": self.complete}},
            })
        return httpx.Response(self.status, json={
            "id": "offline-request", "usage": {"seconds": 84},
            "choices": [{"finish_reason": "stop", "message": {"content": self.content}}],
        })

    def call(self):
        return transcribe.transcribe_audio(self.audio, self.state, self.cfg)

    def test_audio_only_contract_and_cached_original_transcript(self):
        result = self.call()
        self.assertEqual(result["transcript"], self.content)
        self.assertEqual(self.call(), result)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.requests[0]["messages"]), 1)
        self.assertEqual(self.requests[0]["messages"][0]["role"], "user")
        self.assertTrue(self.requests[0]["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/wav;base64,"))
        self.assertNotIn("offline-secret", self.state.read_text())
        self.assertEqual(result["request_id"], "offline-request")

    def test_audio_30_uses_native_contract_and_complete_output(self):
        self.native = True
        self.cfg["model"] = "qwen-audio-3.0-asr-flash"
        result = self.call()
        self.assertEqual(result["transcript"], self.content)
        self.assertEqual(result["request_id"], "native-request")
        self.assertTrue(self.urls[0].endswith("/api/v1/services/aigc/multimodal-generation/generation"))
        payload = self.requests[0]
        self.assertEqual(payload["parameters"], {"format": "wav", "language_hints": ["zh"]})
        self.assertEqual(len(payload["input"]["messages"]), 1)
        self.assertNotIn("asr_options", payload)
        self.assertEqual(self.call(), result)
        self.assertEqual(len(self.requests), 1)

    def test_incomplete_native_output_cannot_advance_or_resubmit(self):
        self.native = True
        self.complete = False
        self.cfg["model"] = "qwen-audio-3.0-asr-flash"
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "未识别"):
                self.call()
        self.assertEqual(len(self.requests), 1)

    def test_unknown_timeout_is_not_retried(self):
        self.timeout = True
        for _ in range(2):
            with self.assertRaises(PendingTask):
                self.call()
        self.assertEqual(len(self.requests), 1)

    def test_connection_failure_before_send_can_be_explicitly_resubmitted(self):
        self.connect_error = True
        with self.assertRaisesRegex(RuntimeError, "未连接"):
            self.call()
        self.assertEqual(json.loads(self.state.read_text())["status"], "not_sent")
        self.connect_error = False
        self.assertEqual(self.call()["status"], "done")

    def test_known_rejection_is_not_retried_in_same_task(self):
        self.status = 429
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "拒绝"):
                self.call()
        self.assertEqual(len(self.requests), 1)

    def test_rejection_keeps_diagnostic_id_without_echoed_request_data(self):
        self.status = 404
        with self.assertRaisesRegex(RuntimeError, "404"):
            self.call()
        state = json.loads(self.state.read_text())
        self.assertEqual(state["error_code"], "ModelNotFound")
        self.assertEqual(state["request_id"], "rejected-request")
        self.assertNotIn("offline-secret", self.state.read_text())

    def test_no_speech_stops_before_rewrite(self):
        self.content = ""
        with self.assertRaisesRegex(RuntimeError, "未识别"):
            self.call()
        with self.assertRaisesRegex(RuntimeError, "未识别"):
            self.call()
        self.assertEqual(len(self.requests), 1)

    def test_changed_audio_cannot_reuse_old_text(self):
        self.call()
        self.audio.write_bytes(b"different-audio")
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            self.call()
        self.assertEqual(len(self.requests), 1)

    def test_too_long_audio_is_rejected_before_request(self):
        self.probe.return_value = {"duration": 301}
        with self.assertRaisesRegex(RuntimeError, "5 分钟"):
            self.call()
        self.assertEqual(self.requests, [])

    def test_oversize_base64_is_rejected_before_request(self):
        self.audio.write_bytes(b"x" * 8_000_000)
        with self.assertRaisesRegex(RuntimeError, "10 MB"):
            self.call()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
