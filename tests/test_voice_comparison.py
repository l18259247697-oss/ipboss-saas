"""Offline audition checkpoint tests, not vendor or voice-quality acceptance."""
import json
import hashlib
import io
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_voices as cv


class VoiceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.json"
        self.output = self.root / "result.wav"
        self.reference = self.root / "ref.wav"
        self.reference.write_bytes(b"reference")
        self.headers = {"Authorization": "Bearer test-key"}
        self.model = "cosyvoice-v3.5-plus"
        self.calls = []
        self.timeout = False
        self.bad_download = False
        self.client = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.addCleanup(self.client.close)
        patcher = patch.object(cv.media, "probe", return_value={"duration": 53.0})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(cv, "verify_audio")
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, request):
        self.calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            self.assertEqual(body["model"], self.model)
            self.assertEqual(body["input"]["text"], cv.TEXT)
            self.assertEqual(body["input"]["instruction"], cv.INSTRUCTION)
            if self.timeout:
                raise httpx.ReadTimeout("unknown", request=request)
            return httpx.Response(200, json={"request_id": "request-1", "output": {"finish_reason": "stop",
                       "audio": {"url": "https://result.oss-cn-beijing.aliyuncs.com/result.wav"}},
                       "usage": {"characters": 440}})
        self.assertNotIn("authorization", request.headers)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 240)
        return httpx.Response(503 if self.bad_download else 200, content=buffer.getvalue())

    def generate(self, text=None):
        return cv.synthesize(self.client, self.headers, self.model, "voice-1", text or cv.TEXT, self.output, self.state)

    def test_completed_call_and_reentry_do_not_repeat_paid_post(self):
        self.assertEqual(self.generate()["status"], "complete")
        self.generate()
        self.assertEqual(len([r for r in self.calls if r.method == "POST"]), 1)

    def test_flash31_uses_new_model_and_does_not_misprice_character_usage(self):
        self.model = cv.FLASH31_MODEL
        result = self.generate()
        self.assertEqual(result["cost_plan"]["billing_unit"], "tokens")
        self.assertIsNone(result["list_price_cny_from_usage"])
        self.generate()
        self.assertEqual(len([r for r in self.calls if r.method == "POST"]), 1)

    def test_token_allowance_is_not_reported_as_a_vendor_cap(self):
        plan = cv.cost_plan(cv.FLASH31_MODEL, cv.TEXT)
        self.assertFalse(plan["provider_enforced_cap"])
        self.assertAlmostEqual(plan["planned_cny"], (2000 * 1.5 + 30000 * 12) / 1000000)
        self.assertAlmostEqual(cv.usage_cost(cv.FLASH31_MODEL, {"input_tokens": 400, "output_tokens": 1800}), 0.0222)
        self.assertIsNone(cv.usage_cost(cv.FLASH31_MODEL, {"characters": 400}))

    def test_oversized_script_cannot_be_submitted(self):
        with self.assertRaisesRegex(RuntimeError, "at most 300"):
            self.generate("x" * 301)
        self.assertFalse(self.calls)

    def test_unknown_submission_blocks_paid_retry(self):
        self.timeout = True
        with self.assertRaises(httpx.ReadTimeout):
            self.generate()
        with self.assertRaisesRegex(RuntimeError, "no automatic paid retry"):
            self.generate()
        self.assertEqual(len(self.calls), 1)

    def test_download_failure_resumes_saved_result(self):
        self.bad_download = True
        with self.assertRaisesRegex(RuntimeError, "Download failed"):
            self.generate()
        self.bad_download = False
        self.generate()
        self.assertEqual(len([r for r in self.calls if r.method == "POST"]), 1)

    def test_changed_text_does_not_reuse_or_overwrite_result(self):
        self.generate()
        with self.assertRaisesRegex(RuntimeError, "inputs changed"):
            self.generate("different")
        self.assertEqual(len(self.calls), 2)

    def test_short_output_not_published(self):
        with patch.object(cv.media, "probe", return_value={"duration": 1}):
            with self.assertRaisesRegex(RuntimeError, "unexpectedly short"):
                self.generate()
        self.assertFalse(self.output.exists())

    def test_mismatched_voice_is_rejected_before_synthesis(self):
        import hashlib
        fingerprint = hashlib.sha256(self.reference.read_bytes() + self.model.encode() + self.headers["Authorization"].encode()).hexdigest()
        cv.save(self.state, {"fingerprint": fingerprint, "voice_id": "existing", "status": "created"})
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200,
                          json={"output": {"target_model": "other", "status": "OK"}}))) as client:
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                cv.enroll(client, self.headers, self.model, self.reference, self.state)

    def test_page_does_not_publish_private_response(self):
        with patch.object(cv, "OUT", self.root / "public"), patch.object(cv, "STATE", self.root / "private"):
            cv.save(cv.STATE / (self.model + ".synthesis.json"), {"status": "generated", "response": {"url": "private-signature"}})
            cv.render()
            self.assertNotIn("private-signature", (cv.OUT / "results.json").read_text())
            self.assertIn("width:100%", (cv.OUT / "index.html").read_text())

    def test_streaming_wav_fix_preserves_pcm_and_original(self):
        raw = b"RIFF" + struct.pack("<I", 0x7fffffbf) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        raw += b"data" + struct.pack("<I", 0x7fffff9b) + b"\x01\x02" * 240
        self.output.write_bytes(raw)
        original = self.root / "vendor.wav"
        self.assertTrue(cv.finalize_wav(self.output, original))
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(self.output.read_bytes()[44:], raw[44:])
        with wave.open(str(self.output)) as audio:
            self.assertEqual(audio.getnframes(), 240)
        self.assertFalse(cv.finalize_wav(self.output, original))

    def test_genuinely_truncated_wav_is_not_disguised_as_streaming(self):
        raw = b"RIFF" + struct.pack("<I", 1000) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        raw += b"data" + struct.pack("<I", 964) + b"\x01\x02" * 240
        self.output.write_bytes(raw)
        with self.assertRaisesRegex(RuntimeError, "Not the known streaming"):
            cv.finalize_wav(self.output, self.root / "vendor.wav")
        self.assertEqual(self.output.read_bytes(), raw)

    def generate_original(self, text=None):
        def respond(request):
            self.calls.append(request)
            self.assertEqual(str(request.url), cv.SF_BASE + "/audio/speech")
            self.assertEqual(json.loads(request.content), {"model": cv.SF_MODEL, "voice": "speech:existing",
                             "input": text or cv.TEXT, "response_format": "wav"})
            self.assertTrue(request.headers.get("X-Trace-Id"))
            if self.timeout:
                raise httpx.ReadTimeout("unknown", request=request)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\x00\x00" * 240)
            return httpx.Response(200, content=buffer.getvalue(), headers={"x-siliconcloud-trace-id": "vendor-id"})
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return cv.synthesize_original(client, self.headers, "speech:existing", text or cv.TEXT, self.output, self.state)

    def test_original_exact_production_payload_and_no_second_call(self):
        result = self.generate_original()
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["instruction"])
        self.assertEqual(result["request_id"], "vendor-id")
        self.assertAlmostEqual(result["list_price_cny_from_input"], 0.03195)
        self.generate_original()
        self.assertEqual(len(self.calls), 1)

    def test_original_unknown_result_blocks_retry(self):
        self.timeout = True
        with self.assertRaises(httpx.ReadTimeout):
            self.generate_original()
        with self.assertRaisesRegex(RuntimeError, "no automatic paid retry"):
            self.generate_original()
        self.assertEqual(len(self.calls), 1)

    def test_original_local_failure_resumes_binary_without_regenerating(self):
        with patch.object(cv, "verify_audio", side_effect=RuntimeError("decode failed")):
            with self.assertRaisesRegex(RuntimeError, "decode failed"):
                self.generate_original()
        self.assertEqual(cv.read(self.state)["status"], "generated")
        self.assertTrue(self.state.with_suffix(".vendor.wav").exists())
        self.assertFalse(self.output.exists())
        self.generate_original()
        self.assertEqual(len(self.calls), 1)

    def test_original_utf8_bytes_not_characters_and_not_actual_billing(self):
        plan = cv.cost_plan(cv.ORIGINAL_MODEL, "中文，A ")
        self.assertEqual(plan["input_utf8_bytes"], 11)
        self.assertAlmostEqual(plan["planned_cny"], 0.00055)
        self.assertIsNone(cv.usage_cost(cv.ORIGINAL_MODEL, {"characters": 11}))
        with self.assertRaisesRegex(RuntimeError, "at most 300"):
            self.generate_original("x" * 301)
        self.assertFalse(self.calls)

    def test_original_cache_requires_same_account_model_and_reference(self):
        config = {"provider": "siliconflow", "model": cv.SF_MODEL, "api_key": "test-key"}
        fingerprint = hashlib.sha256(self.reference.read_bytes() + cv.SF_MODEL.encode() +
                                     b"test-key" + b"original transcript").hexdigest()
        cv.save(self.reference.with_suffix(".voice.json"), {"fingerprint": fingerprint, "uri": "speech:existing"})
        self.assertEqual(cv.original_voice(config, self.reference, "original transcript", self.reference), "speech:existing")
        for change in ({"api_key": "other-key"}, {"model": "other-model"}):
            with self.assertRaises(RuntimeError):
                cv.original_voice({**config, **change}, self.reference, "original transcript", self.reference)
        with self.assertRaises(RuntimeError):
            cv.original_voice(config, self.reference, "changed transcript", self.reference)
        other = self.root / "other.wav"
        other.write_bytes(b"other sample")
        with self.assertRaises(RuntimeError):
            cv.original_voice(config, self.reference, "original transcript", other)

    def test_original_streaming_header_preserves_aigc_metadata_and_pcm(self):
        metadata = b"INFOICMT" + struct.pack("<I", 12) + b"AIGC marker\x00"
        position = 36 + 8 + len(metadata)
        raw = b"RIFF" + struct.pack("<I", 0xffffff00 + position) + b"WAVEfmt "
        raw += struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
        raw += b"LIST" + struct.pack("<I", len(metadata)) + metadata
        raw += b"data" + struct.pack("<I", 0xffffff00) + b"\x01\x02" * 240
        self.output.write_bytes(raw)
        original = self.root / "vendor.wav"
        self.assertTrue(cv.finalize_wav(self.output, original))
        fixed = self.output.read_bytes()
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(fixed[8:position + 4], raw[8:position + 4])
        self.assertEqual(fixed[position + 8:], raw[position + 8:])
        with wave.open(str(self.output)) as audio:
            self.assertEqual(audio.getnframes(), 240)

    def test_review_note_is_bound_to_the_audio_hash(self):
        with patch.object(cv, "OUT", self.root / "public"), patch.object(cv, "STATE", self.root / "private"):
            cv.save(cv.STATE / (cv.ORIGINAL_MODEL + ".synthesis.json"), {"status": "complete", "sha256": "new", "duration": 49})
            cv.save(cv.STATE / (cv.ORIGINAL_MODEL + ".review.json"), {"audio_sha256": "old", "note": "stale finding"})
            cv.OUT.mkdir()
            (cv.OUT / (cv.ORIGINAL_MODEL + ".wav")).write_bytes(b"audio")
            cv.render()
            self.assertNotIn("stale finding", (cv.OUT / "index.html").read_text())
            cv.save(cv.STATE / (cv.ORIGINAL_MODEL + ".review.json"), {"audio_sha256": "new", "note": "review <script>"})
            cv.render()
            self.assertIn("review &lt;script&gt;", (cv.OUT / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
