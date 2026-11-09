"""Offline vendor protocol and no-resubmission tests, not visual quality tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import videoretalk as vt


class VideoRetalkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.face = self.root / "face.mp4"
        self.audio = self.root / "voice.wav"
        self.output = self.root / "final/video.mp4"
        self.face.write_bytes(b"fixture")
        self.audio.write_bytes(b"fixture")
        self.requests = []
        self.fail_submit = False
        self.fail_query = False
        self.extension = True
        self.reject_submit = False
        self.patches = [patch.object(vt, "validate_face", return_value={"duration": 12}),
                        patch.object(vt.media, "probe", return_value={"duration": 12}),
                        patch.object(vt.media, "deliver_video_for_browser", side_effect=self.fake_delivery),
                        patch.object(vt.media, "check_video")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        client = httpx.Client(transport=httpx.MockTransport(self.respond))
        client_patch = patch.object(vt.httpx, "Client", return_value=client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(client.close)
        self.client_patch = client_patch

    def fake_delivery(self, source, destination, expected_duration):
        Path(destination).write_bytes(Path(source).read_bytes())
        return str(destination)

    def respond(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/uploads"):
            return httpx.Response(200, json={"data": {
                "upload_host": "https://fixture.oss-cn-beijing.aliyuncs.com", "upload_dir": "temp",
                "oss_access_key_id": "temporary", "policy": "fixture", "signature": "fixture",
                "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true"}})
        if request.url.host.startswith("fixture."):
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200)
        if request.url.path.endswith("/video-synthesis"):
            body = json.loads(request.content)
            self.assertEqual(body["parameters"]["video_extension"], self.extension)
            self.assertTrue(body["input"]["video_url"].startswith("oss://"))
            self.assertEqual(request.headers["X-DashScope-OssResourceResolve"], "enable")
            if self.fail_submit:
                raise httpx.ReadTimeout("unknown outcome", request=request)
            if self.reject_submit:
                return httpx.Response(400)
            return httpx.Response(200, json={"output": {"task_id": "task-1"}})
        if request.url.path.endswith("/tasks/task-1"):
            if self.fail_query:
                return httpx.Response(503)
            return httpx.Response(200, json={"output": {"task_status": "SUCCEEDED",
                                "video_url": "https://result.example.invalid/video.mp4"},
                                "usage": {"video_duration": 12}})
        if request.url.host == "result.example.invalid":
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, content=b"generated fixture")
        raise AssertionError("Unexpected request")

    def run_generation(self):
        return vt.generate(self.face, self.audio, self.output, {"api_key": "offline-test-only"},
                           video_extension=self.extension)

    def submissions(self):
        return [r for r in self.requests if r.url.path.endswith("/video-synthesis")]

    def reopen_client(self):
        # The adapter owns/closed its first HTTP client; provide a fresh transport for resume.
        self.client_patch.stop()
        client = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.client_patch = patch.object(vt.httpx, "Client", return_value=client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(client.close)

    def test_upload_submit_poll_download_contract(self):
        self.assertEqual(self.run_generation(), str(self.output))
        self.assertEqual(self.output.read_bytes(), b"generated fixture")
        self.assertEqual(len(self.submissions()), 1)

    def test_query_failure_resumes_original_task_without_new_submission(self):
        self.fail_query = True
        with self.assertRaises(vt.PendingTask):
            self.run_generation()
        self.fail_query = False
        self.reopen_client()
        self.run_generation()
        self.assertEqual(len(self.submissions()), 1)

    def test_unknown_submission_is_not_repeated(self):
        self.fail_submit = True
        with self.assertRaises(vt.PendingTask):
            self.run_generation()
        self.reopen_client()
        with self.assertRaises(vt.PendingTask):
            self.run_generation()
        self.assertEqual(len(self.submissions()), 1)

    def test_invalid_media_is_not_published_as_final(self):
        vt.media.check_video.side_effect = RuntimeError("invalid media")
        with self.assertRaises(RuntimeError):
            self.run_generation()
        self.assertFalse(self.output.exists())

    def test_missing_key_sends_nothing(self):
        with self.assertRaises(RuntimeError):
            vt.generate(self.face, self.audio, self.output, {})
        self.assertEqual(self.requests, [])

    def test_prepared_face_disables_extension_and_resumes_same_task(self):
        self.extension = False
        self.fail_query = True
        with self.assertRaises(vt.PendingTask):
            self.run_generation()
        self.fail_query = False
        self.reopen_client()
        self.run_generation()
        self.assertEqual(len(self.submissions()), 1)

    def test_changed_extension_or_media_blocks_even_existing_task_lookup(self):
        self.fail_query = True
        with self.assertRaises(vt.PendingTask):
            self.run_generation()
        count = len(self.requests)
        self.reopen_client()
        self.extension = False
        with self.assertRaisesRegex(vt.PendingTask, "改变"):
            self.run_generation()
        self.extension = True
        self.face.write_bytes(b"different")
        with self.assertRaisesRegex(vt.PendingTask, "改变"):
            self.run_generation()
        self.assertEqual(len(self.requests), count)

    def test_known_rejection_is_not_automatically_resubmitted(self):
        self.reject_submit = True
        with self.assertRaises(RuntimeError):
            self.run_generation()
        self.reopen_client()
        with self.assertRaisesRegex(RuntimeError, "未自动重复提交"):
            self.run_generation()
        self.assertEqual(len(self.submissions()), 1)

    def test_video_without_extension_must_cover_the_audio(self):
        self.extension = False
        vt.validate_face.return_value = {"duration": 11.9}
        with self.assertRaisesRegex(RuntimeError, "短于配音"):
            self.run_generation()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
