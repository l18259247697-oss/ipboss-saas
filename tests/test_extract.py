"""Extraction failures must identify the failed stage, not infer it from an ID."""
import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import extract


class ExtractTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        script = root / "prep.py"
        script.touch()
        stack.enter_context(patch.object(extract, "DOUYIN_PREP", script))
        stack.enter_context(patch.object(extract, "OUTPUTS_DIR", root / "outputs"))
        self.run = stack.enter_context(patch.object(extract.subprocess, "run"))
        self.cfg = stack.enter_context(patch.object(extract, "load_config", return_value={"asr": {"provider": "local"}}))

    def cloud_fixture(self):
        root = extract.OUTPUTS_DIR / "job" / "prep"
        root.mkdir(parents=True)
        video = root / "video.mp4"
        video.write_bytes(b"offline-video")
        (root / "manifest.json").write_text(json.dumps({"视频文件": str(video), "文案": {"标题": "原视频"}}))
        self.cfg.return_value = {"asr": {"provider": "dashscope", "api_key": "offline-only"}}
        self.run.return_value = subprocess.CompletedProcess([], 0, "", "")

    def test_cloud_extraction_uses_audio_not_caption_and_no_local_whisper(self):
        self.cloud_fixture()
        with patch.object(extract.media, "normalize_audio"), patch.object(extract.transcribe, "transcribe_audio", return_value={
            "transcript": "文案生成", "model": "test-asr", "request_id": "source-request",
        }) as asr:
            result = extract.extract_from_link("https://v.douyin.com/example/", "job")
        self.assertIn("--no-transcribe", self.run.call_args.args[0])
        self.assertEqual(result["transcript"], "文案生成")
        self.assertEqual(result["transcript_model"], "test-asr")
        asr.assert_called_once()

    def test_cloud_failure_cannot_fall_back_to_old_transcript(self):
        self.cloud_fixture()
        (extract.OUTPUTS_DIR / "job" / "prep" / "transcript.txt").write_text("旧的错误转写")
        with patch.object(extract.media, "normalize_audio"), patch.object(extract.transcribe, "transcribe_audio", side_effect=RuntimeError("转写拒绝")):
            with self.assertRaisesRegex(RuntimeError, "转写拒绝"):
                extract.extract_from_link("https://v.douyin.com/example/", "job")

    def failure(self, stderr):
        self.run.return_value = subprocess.CompletedProcess([], 1, "", stderr)
        with self.assertRaises(RuntimeError) as caught:
            extract.extract_from_link("https://v.douyin.com/example/", "job")
        return str(caught.exception)

    def test_detail_api_403_is_not_a_video_download_failure(self):
        message = self.failure(
            "Client error '403 Forbidden' for url "
            "'https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123&msToken=private-test'"
        )
        self.assertIn("作品详情", message)
        self.assertIn("403", message)
        self.assertNotIn("下载原链接", message)
        self.assertNotIn("private-test", message)

    def test_unrelated_id_containing_403_is_not_an_http_status(self):
        message = self.failure("video 764403123 not found; msToken=private-test")
        self.assertNotIn("HTTP 403", message)
        self.assertNotIn("private-test", message)

    def test_timeout_is_a_clear_runtime_error(self):
        self.run.side_effect = subprocess.TimeoutExpired(["prep.py"], 900)
        with self.assertRaisesRegex(RuntimeError, "超时"):
            extract.extract_from_link("https://v.douyin.com/example/", "job")

    def test_missing_dependency_has_actionable_error(self):
        self.assertIn("依赖", self.failure("ModuleNotFoundError: No module named 'f2'"))

    def test_share_text_does_not_include_trailing_punctuation(self):
        self.assertEqual(
            extract.clean_link("复制打开抖音 https://v.douyin.com/example/。 看看"),
            "https://v.douyin.com/example/",
        )


if __name__ == "__main__":
    unittest.main()
