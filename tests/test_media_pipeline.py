"""Real FFmpeg fixtures and fake generators; not perceptual quality evidence."""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import config
import db
import pipeline
from services import media
from services.videoretalk import PendingTask


class MediaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.fixture = Path(cls.tmp.name) / "fixture.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=24000", "-t", "3",
            "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(cls.fixture),
        ], check=True, capture_output=True, timeout=30)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_FILE", self.root / "app.db"))
        self.stack.enter_context(patch.object(pipeline, "OUTPUTS_DIR", self.root))
        self.stack.enter_context(patch.object(pipeline, "load_config", return_value=config._defaults({})))
        self.stack.enter_context(patch.object(pipeline, "check_inputs", return_value=(str(self.fixture), "reference.wav")))
        db.init_db()
        self.uid = db.create_user("fixture-user", "fixture-password")
        db.create_job("fixture-job", self.uid, {
            "source_text": "original", "use_clone": True, "voice_ref_text": "reference", "_cost": 0,
        })

    def fake_clone(self, text, reference, ref_text, out):
        media.normalize_audio(self.fixture, out)
        return out

    def fake_lipsync(self, face, audio, out):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.fixture, out)
        return out

    def test_readable_complete_media_is_accepted_and_truncation_rejected(self):
        self.assertAlmostEqual(media.check_video(self.fixture, 3)["duration"], 3, places=1)
        with self.assertRaisesRegex(RuntimeError, "时长"):
            media.check_video(self.fixture, 10)

    def test_browser_delivery_normalizes_high_fps_video(self):
        source = Path(self.root) / "high-fps.mp4"
        output = Path(self.root) / "browser/video.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=320x240:rate=60", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=24000", "-t", "3",
            "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(source),
        ], check=True, capture_output=True, timeout=30)
        media.normalize_video_for_browser(source, output, expected_duration=3)
        video = media.probe(output, "video")["stream"]
        self.assertLessEqual(float(video["avg_frame_rate"].split("/")[0]) /
                             float(video["avg_frame_rate"].split("/")[1]), 30.01)
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertAlmostEqual(media.check_video(output, 3)["duration"], 3, places=1)

    def test_pipeline_success_is_idempotent_but_quality_remains_pending(self):
        with patch.object(pipeline.rewrite, "rewrite_copy", return_value="new script") as rewrite, \
             patch.object(pipeline.voiceclone, "clone", side_effect=self.fake_clone) as clone, \
             patch.object(pipeline.lipsync, "lipsync", side_effect=self.fake_lipsync) as lipsync:
            pipeline.run_pipeline("fixture-job")
            pipeline.run_pipeline("fixture-job")
        self.assertEqual([rewrite.call_count, clone.call_count, lipsync.call_count], [1, 1, 1])
        job = db.get_job("fixture-job")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"]["quality_review"], "pending")
        self.assertTrue(job["result"]["media_checks_passed"])
        self.assertFalse(job["result"]["placeholder"])

    def test_waiting_task_reuses_audio_and_script(self):
        with patch.object(pipeline.rewrite, "rewrite_copy", return_value="new script") as rewrite, \
             patch.object(pipeline.voiceclone, "clone", side_effect=self.fake_clone) as clone, \
             patch.object(pipeline.lipsync, "lipsync", side_effect=PendingTask("poll unavailable")):
            pipeline.run_pipeline("fixture-job")
            self.assertEqual(db.get_job("fixture-job")["status"], "waiting")
            with patch.object(pipeline.lipsync, "lipsync", side_effect=self.fake_lipsync):
                pipeline.run_pipeline("fixture-job")
        rewrite.assert_called_once()
        clone.assert_called_once()
        self.assertEqual(db.get_job("fixture-job")["status"], "done")

    def test_invalid_output_is_never_published_as_success(self):
        def invalid(face, audio, out):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"broken media")
        with patch.object(pipeline.rewrite, "rewrite_copy", return_value="new script"), \
             patch.object(pipeline.voiceclone, "clone", side_effect=self.fake_clone), \
             patch.object(pipeline.lipsync, "lipsync", side_effect=invalid), \
             contextlib.redirect_stderr(io.StringIO()):
            pipeline.run_pipeline("fixture-job")
        job = db.get_job("fixture-job")
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("video_url", job["result"])

    def test_rejected_copy_cannot_reach_paid_voice_or_lipsync(self):
        with patch.object(pipeline.rewrite, "rewrite_copy", side_effect=RuntimeError("未经核验的速度承诺")), \
             patch.object(pipeline.voiceclone, "clone") as clone, \
             patch.object(pipeline.lipsync, "lipsync") as lipsync, \
             contextlib.redirect_stderr(io.StringIO()):
            pipeline.run_pipeline("fixture-job")
        clone.assert_not_called()
        lipsync.assert_not_called()
        self.assertEqual(db.get_job("fixture-job")["status"], "failed")

    def test_unknown_asr_cannot_be_replayed_on_resume(self):
        db.update_job("fixture-job", params=json.dumps({"link": "https://v.douyin.com/example/", "_cost": 0}))
        with patch.object(pipeline.extract, "extract_from_link", side_effect=PendingTask("转写未知")) as extract, \
             patch.object(pipeline.rewrite, "rewrite_copy") as rewrite, \
             patch.object(pipeline.voiceclone, "clone") as clone:
            pipeline.run_pipeline("fixture-job")
            pipeline.run_pipeline("fixture-job")
        extract.assert_called_once()
        rewrite.assert_not_called()
        clone.assert_not_called()
        self.assertEqual(db.get_job("fixture-job")["status"], "waiting")

    def test_voice_download_resume_reuses_rewrite_before_audio_checkpoint(self):
        cfg = config._defaults({})
        cfg["voice_clone"]["provider"] = "dashscope"
        with patch.object(pipeline, "load_config", return_value=cfg), \
             patch.object(pipeline.rewrite, "rewrite_copy", return_value="new script") as rewrite, \
             patch.object(pipeline.voiceclone, "clone", side_effect=PendingTask("saved audio download pending")), \
             patch.object(pipeline.lipsync, "lipsync", side_effect=self.fake_lipsync):
            pipeline.run_pipeline("fixture-job")
            self.assertEqual(db.get_job("fixture-job")["status"], "waiting")
            with patch.object(pipeline.voiceclone, "clone", side_effect=self.fake_clone):
                pipeline.run_pipeline("fixture-job")
        rewrite.assert_called_once()
        self.assertEqual(db.get_job("fixture-job")["status"], "done")

    def test_restart_requires_resume_and_preserves_existing_results(self):
        db.update_job("fixture-job", status="running", step=5, result={"rewritten": "saved", "audio_url": "/saved.wav"})
        db.recover_interrupted_jobs()
        job = db.get_job("fixture-job")
        self.assertEqual(job["status"], "waiting")
        self.assertEqual(job["result"]["rewritten"], "saved")
        self.assertEqual(job["step"], 5)
        db.update_job("fixture-job", status="done")
        db.recover_interrupted_jobs()
        self.assertEqual(db.get_job("fixture-job")["status"], "done")

    def test_legacy_voice_without_saved_audio_is_not_replayed_after_restart(self):
        db.update_job("fixture-job", status="waiting", result={"transcript": "saved", "rewritten": "saved"})
        with patch.object(pipeline.voiceclone, "clone") as clone:
            pipeline.run_pipeline("fixture-job")
        clone.assert_not_called()
        self.assertEqual(db.get_job("fixture-job")["status"], "waiting")
