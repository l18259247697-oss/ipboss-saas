"""Isolated workflow regressions; generators are mocked, not quality evidence."""
import contextlib
import io
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app
import config
import db
import pipeline
from fastapi.testclient import TestClient


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x01\x00" * 16000 * 3)
    return out.getvalue()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name in ("faces", "voices", "outputs"):
            (self.root / name).mkdir()
        self.cfg = config._defaults()
        self.cfg["digital_human"]["provider"] = "none"
        self.cfg["voice_clone"]["provider"] = "none"
        for obj, key, value in (
            (db, "DB_FILE", self.root / "app.db"),
            (config, "FACES_DIR", self.root / "faces"),
            (config, "VOICES_DIR", self.root / "voices"),
            (app, "FACES_DIR", self.root / "faces"),
            (app, "VOICES_DIR", self.root / "voices"),
            (app, "OUTPUTS_DIR", self.root / "outputs"),
            (pipeline, "OUTPUTS_DIR", self.root / "outputs"),
            (app, "load_config", lambda: self.cfg),
            (pipeline, "load_config", lambda: self.cfg),
        ):
            self.stack.enter_context(patch.object(obj, key, value))
        db.init_db()
        self.uid = db.create_user("flow-user", "test-password", credits=100)
        token = db.verify_login("flow-user", "test-password")
        self.client = TestClient(app.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + token}

    def test_voice_sidecars_are_not_audio(self):
        (self.root / "voices" / f"u{self.uid}.uri").write_text("old-uri")
        (self.root / "voices" / f"u{self.uid}.txt").write_text("reference")
        self.assertIsNone(config.find_user_voice(self.uid))

    def test_frontend_script_revalidates_after_updates(self):
        response = self.client.get("/app.js?v=6")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_health_and_readiness_are_safe_operator_probes(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 503)
        self.assertEqual(ready.json()["status"], "not_ready")
        self.assertIn("llm", ready.json()["checks"])

    def test_upload_and_job_input_limits_fail_before_expensive_work(self):
        with patch.object(app, "MAX_VOICE_UPLOAD_BYTES", 3):
            response = self.client.post("/api/voice", headers=self.headers,
                                        files={"file": ("sample.wav", b"1234")})
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/api/jobs", headers=self.headers,
                                    json={"source_text": "x" * 30001})
        self.assertEqual(response.status_code, 422)
        self.assertIn("source_text", response.json()["detail"])

    def test_invalid_upload_preserves_previous_voice(self):
        old = self.root / "voices" / f"u{self.uid}.wav"
        old.write_bytes(wav_bytes())
        response = self.client.post("/api/voice", headers=self.headers,
                                    files={"file": ("broken.wav", b"not audio")})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(old.read_bytes(), wav_bytes())

    def test_face_upload_rejects_images_before_task_creation(self):
        response = self.client.post("/api/face", headers=self.headers,
                                    files={"file": ("face.png", b"not an image")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("MP4 或 MOV", response.json()["detail"])

    def test_reference_text_survives_reload(self):
        response = self.client.post("/api/voice", headers=self.headers,
                                    files={"file": ("sample.wav", wav_bytes())},
                                    data={"ref_text": "this is the original reference"})
        self.assertEqual(response.status_code, 200)
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertEqual(me.get("voice_ref_text"), "this is the original reference")

    def test_login_cookie_can_authenticate_and_logout_clears_it(self):
        login = self.client.post("/api/login", json={"username": "flow-user", "password": "test-password"})
        self.assertEqual(login.status_code, 200)
        self.assertIn("session_token=", login.headers.get("set-cookie", ""))
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_reference_can_be_saved_after_upload_and_stale_sample_is_rejected(self):
        uploaded = self.client.post("/api/voice", headers=self.headers,
                                   files={"file": ("sample.wav", wav_bytes())}).json()
        response = self.client.post("/api/voice/reference", headers=self.headers,
            json={"ref_text": "entered after uploading", "voice_revision": uploaded.get("voice_revision", "")})
        self.assertEqual(response.status_code, 200)
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertEqual(me["voice_ref_text"], "entered after uploading")
        stale = self.client.post("/api/voice/reference", headers=self.headers,
            json={"ref_text": "wrong recording text", "voice_revision": "old-sample"})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get("/api/me", headers=self.headers).json()["voice_ref_text"], "entered after uploading")

    def test_latest_task_can_be_restored_after_reload(self):
        other = db.create_user("other-user", "test-password")
        db.create_job("own-task", self.uid, {})
        db.create_job("other-task", other, {})
        response = self.client.get("/api/jobs/latest", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "own-task")

    def test_charged_job_is_atomic_and_idempotent(self):
        params = {"source_text": "source", "_cost": 10}
        first = db.create_charged_job("charged-task", self.uid, params, 10, "request-1")
        self.assertEqual(first, ("charged-task", True, 90))
        second = db.create_charged_job("different-id", self.uid, params, 10, "request-1")
        self.assertEqual(second, ("charged-task", False, 90))
        self.assertEqual(db.get_credits(self.uid), 90)
        self.assertEqual(db.get_job("charged-task")["status"], "queued")

    def test_idempotency_key_cannot_change_inputs(self):
        params = {"source_text": "source", "_cost": 10}
        db.create_charged_job("fingerprint-task", self.uid, params, 10, "request-3", "hash-a")
        with self.assertRaises(db.IdempotencyConflict):
            db.create_charged_job("different-task", self.uid, params, 10, "request-3", "hash-b")
        self.assertEqual(db.get_credits(self.uid), 90)

    def test_charged_job_rejects_without_mutating_credits_or_rows(self):
        with self.assertRaises(db.InsufficientCredits):
            db.create_charged_job("too-expensive", self.uid, {"_cost": 101}, 101, "request-2")
        self.assertEqual(db.get_credits(self.uid), 100)
        self.assertIsNone(db.get_job("too-expensive"))

    def test_credit_ledger_tracks_charge_refund_and_admin_adjustment(self):
        db.add_credits(self.uid, 5)
        db.create_charged_job("ledger-task", self.uid, {"source_text": "source"}, 10, "ledger-request")
        self.assertTrue(db.refund_job_once("ledger-task", self.uid, 10))
        rows = db.list_credit_ledger(self.uid)
        kinds = [row["kind"] for row in rows]
        self.assertIn("register_bonus", kinds)
        self.assertIn("admin_adjustment", kinds)
        self.assertIn("job_charge", kinds)
        self.assertIn("job_refund", kinds)
        charge = next(row for row in rows if row["kind"] == "job_charge")
        refund = next(row for row in rows if row["kind"] == "job_refund")
        self.assertEqual((charge["delta"], charge["job_id"], charge["request_id"]), (-10, "ledger-task", "ledger-request"))
        self.assertEqual((refund["delta"], refund["job_id"]), (10, "ledger-task"))

    def test_admin_can_read_credit_ledger_without_secret_fields(self):
        admin_id = db.create_user("ledger-admin", "admin-password", is_admin=1)
        admin_token = db.verify_login("ledger-admin", "admin-password")
        db.add_credits(self.uid, 3)
        response = self.client.get("/api/admin/credits/ledger", headers={"Authorization": "Bearer " + admin_token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(row["user_id"] == self.uid for row in response.json()))
        self.assertFalse(any("api_key" in row for row in response.json()))

    def test_admin_config_does_not_echo_or_clear_saved_keys(self):
        admin_id = db.create_user("admin-user", "admin-password", is_admin=1)
        admin_token = db.verify_login("admin-user", "admin-password")
        self.cfg["llm"]["api_key"] = "secret-llm"
        self.cfg["digital_human"]["api_key"] = "secret-dh"
        self.cfg["voice_clone"]["dashscope_api_key"] = "secret-vc"
        view = app._admin_config_view(self.cfg)
        self.assertEqual(view["llm"]["api_key"], "")
        self.assertEqual(view["digital_human"]["api_key"], "")
        merged = app._merge_admin_config({**view, "llm": {**view["llm"], "model": "new-model"}})
        self.assertEqual(merged["llm"]["api_key"], "secret-llm")
        self.assertEqual(merged["digital_human"]["api_key"], "secret-dh")
        self.assertEqual(merged["voice_clone"]["dashscope_api_key"], "secret-vc")

    def test_task_restore_includes_user_inputs_but_not_internal_paths(self):
        params = {"link": "https://v.douyin.com/example/", "industry": "shop",
                  "extra": "keep this", "use_clone": True, "_cost": 10,
                  "_face_path": "/private/face.mov", "_voice_path": "/private/voice.wav"}
        db.create_job("restore-inputs", self.uid, params)
        restored = self.client.get("/api/jobs/latest", headers=self.headers).json()
        self.assertEqual(restored["params"], {k: v for k, v in params.items() if not k.startswith("_")})

    def test_history_keeps_completed_and_failed_tasks_scoped_to_account(self):
        other = db.create_user("other-history-user", "test-password")
        db.create_job("old-complete", self.uid, {"industry": "previous"})
        db.update_job("old-complete", status="done", result={"video_url": "/outputs/old/video.mp4"})
        db.create_job("new-failed", self.uid, {"industry": "latest"})
        db.update_job("new-failed", status="failed")
        db.create_job("not-owned", other, {})
        response = self.client.get("/api/jobs", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([j["id"] for j in response.json()], ["new-failed", "old-complete"])
        self.assertNotIn("result", response.json()[0])
        restored = self.client.get("/api/jobs/old-complete", headers=self.headers).json()
        self.assertEqual(restored["result"]["video_url"], "/api/media/job/old-complete/video")
        self.assertEqual(self.client.get("/api/jobs/not-owned", headers=self.headers).status_code, 404)

    def test_media_routes_require_auth_and_scope_files_to_account(self):
        other = db.create_user("media-other-user", "test-password")
        other_token = db.verify_login("media-other-user", "test-password")
        (self.root / "faces" / f"u{self.uid}.mp4").write_bytes(b"face")
        (self.root / "voices" / f"u{self.uid}.wav").write_bytes(wav_bytes())
        db.create_job("media-task", self.uid, {})
        final = self.root / "outputs" / "media-task" / "final"
        final.mkdir(parents=True)
        (final / "video.mp4").write_bytes(b"video")

        self.assertEqual(self.client.get("/api/media/face", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/media/voice", headers=self.headers).status_code, 200)
        video = self.client.get("/api/media/job/media-task/video", headers=self.headers)
        self.assertEqual(video.status_code, 200)
        ranged = self.client.get("/api/media/job/media-task/video", headers={**self.headers, "Range": "bytes=0-1"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"vi")
        self.assertEqual(self.client.get("/api/media/face").status_code, 401)
        other_headers = {"Authorization": "Bearer " + other_token}
        self.assertEqual(self.client.get("/api/media/face", headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get("/api/media/job/media-task/video", headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get("/outputs/media-task/final/video.mp4", headers=self.headers).status_code, 404)

    def test_history_exposes_quality_review_without_exposing_result_body(self):
        db.create_job("quality-task", self.uid, {"industry": "shop"})
        db.update_job("quality-task", status="done",
                      result={"quality_review": "pending", "media_checks_passed": True,
                              "video_url": "/outputs/quality-task/final/video.mp4"})
        history = self.client.get("/api/jobs", headers=self.headers)
        self.assertEqual(history.status_code, 200)
        item = next(row for row in history.json() if row["id"] == "quality-task")
        self.assertEqual(item["quality_review"], "pending")
        self.assertTrue(item["media_checks_passed"])
        self.assertNotIn("video_url", item)

    def test_missing_face_stops_before_paid_rewrite(self):
        db.create_job("missing-face", self.uid, {"link": "https://example.invalid", "_cost": 0})
        with patch.object(pipeline.extract, "extract_from_link") as extract, \
             patch.object(pipeline.rewrite, "rewrite_copy") as rewrite:
            with contextlib.redirect_stderr(io.StringIO()):
                pipeline.run_pipeline("missing-face")
        extract.assert_not_called()
        rewrite.assert_not_called()
        self.assertEqual(db.get_job("missing-face")["status"], "failed")

    def test_unavailable_clone_never_becomes_preset_audio(self):
        (self.root / "faces" / f"u{self.uid}.jpg").write_bytes(b"fixture")
        (self.root / "voices" / f"u{self.uid}.wav").write_bytes(wav_bytes())
        db.create_job("clone-job", self.uid, {"source_text": "source", "use_clone": True, "_cost": 0})
        with patch.object(pipeline.voiceclone, "available", return_value=False), \
             patch.object(pipeline.extract, "extract_from_link",
                          return_value={"transcript": "source", "title": "test"}), \
             patch.object(pipeline.rewrite, "rewrite_copy", return_value="new script"), \
             patch.object(pipeline.tts, "generate_audio") as preset, \
             patch.object(pipeline.compose, "compose_placeholder") as placeholder:
            with contextlib.redirect_stderr(io.StringIO()):
                pipeline.run_pipeline("clone-job")
        preset.assert_not_called()
        placeholder.assert_not_called()
        self.assertEqual(db.get_job("clone-job")["status"], "failed")
        self.assertIn("克隆", db.get_job("clone-job")["message"])

    def test_resume_without_checkpoint_never_restarts_paid_stages(self):
        db.create_job("lost-checkpoint", self.uid, {"source_text": "source", "_cost": 0})
        db.update_job("lost-checkpoint", status="waiting")
        with patch.object(pipeline, "check_inputs", return_value=("face.mov", None)), \
             patch.object(pipeline.rewrite, "rewrite_copy", side_effect=RuntimeError("must not run")) as rewrite:
            with contextlib.redirect_stderr(io.StringIO()):
                pipeline.run_pipeline("lost-checkpoint")
        rewrite.assert_not_called()
        self.assertEqual(db.get_job("lost-checkpoint")["status"], "waiting")

    def test_link_failure_stops_paid_stages_and_refunds_only_once(self):
        db.add_credits(self.uid, -10)
        db.create_job("blocked-link", self.uid, {"link": "https://v.douyin.com/example/", "_cost": 10})
        with patch.object(pipeline, "check_inputs", return_value=("face.mov", None)), \
             patch.object(pipeline.extract, "extract_from_link", side_effect=RuntimeError("作品详情 HTTP 403")), \
             patch.object(pipeline.rewrite, "rewrite_copy") as rewrite, \
             patch.object(pipeline.voiceclone, "clone") as clone, \
             patch.object(pipeline.lipsync, "lipsync") as lipsync, \
             contextlib.redirect_stderr(io.StringIO()):
            pipeline.run_pipeline("blocked-link")
            pipeline.run_pipeline("blocked-link")
        rewrite.assert_not_called()
        clone.assert_not_called()
        lipsync.assert_not_called()
        self.assertEqual(db.get_job("blocked-link")["status"], "failed")
        self.assertEqual(db.get_credits(self.uid), 100)

    def test_resumed_failure_refunds_once_and_marks_the_job(self):
        db.add_credits(self.uid, -10)
        db.create_job("resume-failed", self.uid, {"source_text": "source", "use_clone": True, "_cost": 10})
        audio = self.root / "outputs" / "resume-failed" / "voice.wav"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(wav_bytes())
        db.update_job("resume-failed", status="waiting", result={
            "transcript": "source", "rewritten": "script", "audio_url": "/saved.wav",
        })
        with patch.object(pipeline, "check_inputs", return_value=(str(audio), str(audio))), \
             patch.object(pipeline.lipsync, "lipsync", side_effect=RuntimeError("provider failed")), \
             contextlib.redirect_stderr(io.StringIO()):
            pipeline.run_pipeline("resume-failed")
            pipeline.run_pipeline("resume-failed")
        job = db.get_job("resume-failed")
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["result"]["credits_refunded"])
        self.assertEqual(db.get_credits(self.uid), 100)


if __name__ == "__main__":
    unittest.main()
