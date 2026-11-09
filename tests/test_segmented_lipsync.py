"""Real WAV splitting/MP4 joining with a fake, serial vendor generator."""
import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import media, segmented_lipsync as segmented
from services.videoretalk import PendingTask


class SegmentedTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.audio = self.root / "voice.wav"
        with wave.open(str(self.audio), "wb") as w:
            w.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            w.writeframes(b"\x01\x00" * 153600)
        self.face = self.root / "face.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "nullsrc=s=320x240:r=25,geq=lum='16+2*N':cb=128:cr=128",
                        "-frames:v", "70", "-c:v", "libx264", "-threads", "1", str(self.face)],
                       check=True, capture_output=True, timeout=30)
        self.output = self.root / "final/video.mp4"
        self.stack.enter_context(patch.object(segmented, "SINGLE_LIMIT", 5))
        self.stack.enter_context(patch.object(segmented.videoretalk, "validate_face"))
        self.calls = []
        self.face_inputs = []
        self.fail_second = False
        self.stack.enter_context(patch.object(segmented.videoretalk, "generate", side_effect=self.fake_vendor))

    def fake_vendor(self, face, audio, out, cfg, *, video_extension=True):
        self.calls.append(Path(out).parent.name)
        self.face_inputs.append((Path(face), video_extension))
        if self.fail_second and self.calls[-1] == "part-002":
            raise PendingTask("existing vendor task waiting")
        duration = media.probe(audio, "audio")["duration"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x240:r=25",
                        "-i", str(audio), "-t", str(duration), "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(out)],
                       check=True, capture_output=True, timeout=30)
        return str(out)

    def generate(self):
        return segmented.generate(self.face, self.audio, self.output, {"api_key": "offline-only"})

    def test_high_fps_limit_leaves_context_headroom(self):
        with patch.object(segmented, "SINGLE_LIMIT", 120):
            self.assertEqual(segmented._segment_limit({"stream": {"avg_frame_rate": "50/1"}}), 59.5)
        self.assertEqual(segmented._segment_limit({"stream": {"avg_frame_rate": "25/1"}}), 5)

    def test_complete_audio_is_partitioned_once_and_final_media_decodes(self):
        self.generate()
        self.assertEqual(self.calls, ["part-001", "part-002"])
        chunks = sorted(self.output.parent.glob("part-*/audio.wav"))
        frames = b""
        for p in chunks:
            with wave.open(str(p), "rb") as w:
                frames += w.readframes(w.getnframes())
        with wave.open(str(self.audio), "rb") as w:
            self.assertEqual(frames, w.readframes(w.getnframes()))
        media.check_video(self.output, 6.4)
        self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_second_part_continues_global_face_motion_instead_of_restarting(self):
        self.generate()
        self.assertEqual([extension for _, extension in self.face_inputs], [False, False])
        pixels = []
        for face, _ in self.face_inputs:
            result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(face),
                                     "-vf", "scale=1:1", "-pix_fmt", "gray", "-threads", "1",
                                     "-f", "rawvideo", "-"], check=True, capture_output=True)
            pixels.append(result.stdout)
        # Frame 80 is on the reverse half of a 138-frame cycle, not frame zero.
        self.assertGreater(pixels[1][0], 100)
        state = json.loads((self.output.parent / "segments.json").read_text())
        offset = state["parts"][1]["trim_start_frame"]
        self.assertLess(abs(pixels[0][79] - pixels[1][offset]), 5)
        self.assertGreater(offset, 0)

    def test_context_overlaps_only_vendor_requests_not_final_audio(self):
        self.generate()
        state = json.loads((self.output.parent / "segments.json").read_text())
        left, right = state["parts"]
        self.assertEqual(left["end_sample"], right["start_sample"])
        self.assertGreater(left["request_end_sample"], right["request_start_sample"])
        self.assertEqual(left["end_frame"], right["start_frame"])
        with wave.open(str(self.output.parent / "full-audio.wav"), "rb") as source:
            pcm = source.readframes(source.getnframes())
        for part in state["parts"]:
            with wave.open(str(self.output.parent / part["name"] / "request-audio.wav"), "rb") as source:
                self.assertEqual(source.readframes(source.getnframes()),
                                 pcm[part["request_start_sample"]*2:part["request_end_sample"]*2])

    def test_vendor_temporal_warmup_is_trimmed_inside_overlap(self):
        def delayed_vendor(face, audio, out, cfg, **kwargs):
            duration = media.probe(audio, "audio")["duration"]
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "1", "-i", str(face),
                            "-i", str(audio), "-vf", "tpad=start=4:start_mode=clone", "-t", str(duration),
                            "-c:v", "libx264", "-threads", "1", "-c:a", "aac", str(out)],
                           check=True, capture_output=True, timeout=30)
        with patch.object(segmented.videoretalk, "generate", side_effect=delayed_vendor):
            self.generate()
        pixels = []
        for video in sorted(self.output.parent.glob("part-*/timeline.mp4")):
            result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", "scale=1:1",
                                     "-pix_fmt", "gray", "-threads", "1", "-f", "rawvideo", "-"],
                                    capture_output=True, check=True)
            pixels.append(result.stdout)
        self.assertLess(abs(pixels[0][-1] - pixels[1][0]), 5)

    def test_resume_does_not_recreate_finished_segment_or_lose_full_audio(self):
        self.fail_second = True
        with self.assertRaises(PendingTask):
            self.generate()
        self.assertFalse(self.output.exists())
        self.fail_second = False
        self.generate()
        self.assertEqual(self.calls, ["part-001", "part-002", "part-002"])
        media.check_video(self.output, 6.4)

    def test_changed_input_stops_before_reusing_segment(self):
        self.generate()
        self.face.write_bytes(b"changed-face")
        with self.assertRaisesRegex(PendingTask, "改变"):
            self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_missing_or_changed_completed_chunk_is_not_regenerated(self):
        self.fail_second = True
        with self.assertRaises(PendingTask):
            self.generate()
        (self.output.parent / "part-001/video.mp4").write_bytes(b"changed")
        with self.assertRaisesRegex(PendingTask, "分段成片"):
            self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_changed_prepared_face_stops_resume_without_new_submission(self):
        self.fail_second = True
        with self.assertRaises(PendingTask):
            self.generate()
        (self.output.parent / "part-002/face.mp4").write_bytes(b"changed")
        with self.assertRaisesRegex(PendingTask, "人物分段"):
            self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_legacy_completed_video_is_preserved_but_partial_plan_is_not_migrated(self):
        self.generate()
        manifest = self.output.parent / "segments.json"
        state = json.loads(manifest.read_text())
        state.pop("plan_version")
        manifest.write_text(json.dumps(state))
        before = hashlib.sha256(self.output.read_bytes()).hexdigest()
        self.generate()
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), before)
        self.assertEqual(len(self.calls), 2)
        state["status"] = "processing"
        manifest.write_text(json.dumps(state))
        with self.assertRaisesRegex(PendingTask, "旧分段计划"):
            self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_lost_plan_cannot_reassign_an_existing_paid_request(self):
        self.fail_second = True
        with self.assertRaises(PendingTask):
            self.generate()
        manifest = self.output.parent / "segments.json"
        state = json.loads(manifest.read_text())
        state.pop("timeline")
        manifest.write_text(json.dumps(state))
        (self.output.parent / "part-002/videoretalk-task.json").write_text('{"task_id":"existing"}')
        with self.assertRaisesRegex(PendingTask, "已有口型任务"):
            self.generate()
        self.assertEqual(len(self.calls), 2)

    def test_lost_manifest_does_not_overwrite_audio_of_existing_task(self):
        folder = self.output.parent / "part-001"
        folder.mkdir(parents=True)
        (folder / "videoretalk-task.json").write_text('{"task_id":"existing"}')
        (folder / "audio.wav").write_bytes(b"original-paid-input")
        with self.assertRaisesRegex(PendingTask, "分段计划丢失"):
            self.generate()
        self.assertEqual((folder / "audio.wav").read_bytes(), b"original-paid-input")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
