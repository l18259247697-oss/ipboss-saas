"""Offline HTTP contracts and recovery, not real vendor/voice-quality evidence."""
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import dashscope_voice as qv, voiceclone
from services.videoretalk import PendingTask


class QwenVoiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(qv, "STATE_DIR", self.root / "private"))
        self.reference = self.root / "reference.wav"
        with wave.open(str(self.reference), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 24000 * 3)
        self.output = self.root / "public/voice.wav"
        self.cfg = {"model": qv.MODEL, "api_key": "beijing-test-key"}
        self.actions = []
        self.fail = ""
        self.voice = None
        self.result_scheme = "https"
        self.actual_client = httpx.Client
        self.stack.enter_context(patch.object(qv.httpx, "Client", side_effect=lambda **kw:
            self.actual_client(transport=httpx.MockTransport(self.respond), **kw)))

    def respond(self, request):
        if request.url.host == "upload.oss-cn-beijing.aliyuncs.com":
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.endswith("/uploads"):
            self.assertEqual(request.url.params["model"], "voice-enrollment")
            return httpx.Response(200, json={"data": {"upload_host": "https://upload.oss-cn-beijing.aliyuncs.com",
                "upload_dir": "temp", "oss_access_key_id": "upload-only", "policy": "policy", "signature": "sig",
                "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}})
        if request.url.host == "result.oss-cn-beijing.aliyuncs.com":
            self.assertEqual(request.url.scheme, "https")
            self.actions.append("download")
            self.assertNotIn("authorization", request.headers)
            if self.fail == "download":
                return httpx.Response(503)
            raw = bytearray(self.reference.read_bytes())
            struct.pack_into("<I", raw, 4, 0x7fffffbf)
            struct.pack_into("<I", raw, 40, 0x7fffff9b)
            return httpx.Response(200, content=bytes(raw))
        body = json.loads(request.content)
        action = body["input"].get("action", "synthesize")
        self.actions.append(action)
        if action == "create_voice":
            self.assertEqual(body["input"]["target_model"], qv.MODEL)
            self.voice = qv.MODEL + "-" + body["input"]["prefix"] + "-test"
            if self.fail == "enroll_timeout":
                raise httpx.ReadTimeout("unknown", request=request)
            return httpx.Response(200, json={"output": {"voice_id": self.voice}})
        if action == "list_voice":
            return httpx.Response(200, json={"output": {"voice_list": [{"voice_id": self.voice}] if self.voice else []}})
        if action == "query_voice":
            return httpx.Response(200, json={"output": {"target_model": "wrong" if self.fail == "wrong_model" else qv.MODEL,
                                                       "status": "OK"}})
        self.assertEqual(action, "synthesize")
        self.assertEqual(body["model"], qv.MODEL)
        self.assertEqual(body["input"]["voice"], self.voice)
        self.assertEqual(body["input"]["instruction"], qv.INSTRUCTION)
        self.assertEqual(body["input"]["seed"], 928)
        if self.fail == "synthesis_timeout":
            raise httpx.ReadTimeout("unknown", request=request)
        return httpx.Response(200, json={"output": {"finish_reason": "stop",
                      "audio": {"url": self.result_scheme + "://result.oss-cn-beijing.aliyuncs.com/audio.wav"}},
                      "usage": {"characters": 24}, "request_id": "vendor-request"})

    def generate(self, text="这是一段声音克隆测试文案"):
        return qv.generate(text, self.reference, self.output, self.cfg)

    def test_complete_real_wav_and_reentry_without_paid_replay(self):
        self.generate()
        self.generate()
        self.assertEqual(self.actions.count("create_voice"), 1)
        self.assertEqual(self.actions.count("synthesize"), 1)
        self.assertEqual(self.actions.count("download"), 1)
        with wave.open(str(self.output)) as audio:
            self.assertEqual(audio.getnframes(), 72000)
        self.assertTrue(list((self.root / "private").rglob("*.vendor.wav")))

    def test_second_job_reuses_matching_voice(self):
        self.generate()
        self.output = self.root / "public/other.wav"
        self.generate()
        self.assertEqual(self.actions.count("create_voice"), 1)
        self.assertEqual(self.actions.count("synthesize"), 2)

    def test_long_valid_voice_is_preserved_for_segmented_lipsync(self):
        probe = qv.media.probe
        def duration(path, kind):
            info = probe(path, kind)
            if str(path).endswith(".pending.wav"):
                info["duration"] = 134.32
            return info
        with patch.object(qv.media, "probe", side_effect=duration):
            self.generate()
        self.assertTrue(self.output.exists())
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_vendor_http_result_is_downloaded_over_https(self):
        self.result_scheme = "http"
        self.generate()
        self.assertTrue(self.output.exists())
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_download_resume_uses_saved_response(self):
        self.fail = "download"
        with self.assertRaises(PendingTask):
            self.generate()
        self.assertFalse(self.output.exists())
        self.fail = ""
        self.generate()
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_unknown_paid_submission_never_retries(self):
        self.fail = "synthesis_timeout"
        with self.assertRaises(PendingTask):
            self.generate()
        self.fail = ""
        with self.assertRaisesRegex(PendingTask, "提交结果未知"):
            self.generate()
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_unknown_enrollment_is_reconciled_by_listing(self):
        self.fail = "enroll_timeout"
        with self.assertRaises(PendingTask):
            self.generate()
        self.fail = ""
        self.generate()
        self.assertEqual(self.actions.count("create_voice"), 1)
        self.assertEqual(self.actions.count("list_voice"), 1)
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_wrong_voice_model_stops_before_synthesis(self):
        self.fail = "wrong_model"
        with self.assertRaisesRegex(RuntimeError, "不匹配"):
            self.generate()
        self.assertNotIn("synthesize", self.actions)

    def test_changed_inputs_and_missing_completed_output_do_not_regenerate(self):
        self.generate()
        with self.assertRaisesRegex(PendingTask, "已改变"):
            self.generate("不同文案")
        self.output.unlink()
        with self.assertRaisesRegex(PendingTask, "丢失或改变"):
            self.generate()
        self.assertEqual(self.actions.count("synthesize"), 1)

    def test_adapter_uses_beijing_key_not_legacy_siliconflow_key(self):
        cfg = {"voice_clone": {"provider": "dashscope", "model": qv.MODEL, "api_key": "legacy-key"},
               "lipsync": {"provider": "dashscope", "api_key": "beijing-key"}}
        with patch.object(voiceclone, "load_config", return_value=cfg):
            self.assertEqual(voiceclone._cfg()["api_key"], "beijing-key")
            self.assertTrue(voiceclone.available())


if __name__ == "__main__":
    unittest.main()
