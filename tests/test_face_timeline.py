"""Decode real moving frames, including reverse blocks, wrapping, and rotation."""
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from services import face_timeline as timeline, media


def ffmpeg(*args):
    return subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "1", *args],
                          capture_output=True, check=True, timeout=60)


def pixels(path):
    return ffmpeg("-i", str(path), "-vf", "scale=1:1", "-pix_fmt", "gray",
                  "-threads", "1", "-f", "rawvideo", "-").stdout


class FaceTimelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.face = self.root / "face.mp4"
        ffmpeg("-f", "lavfi", "-i", "nullsrc=s=320x240:r=30000/1001,geq=lum='16+2*N':cb=128:cr=128",
               "-frames:v", "70", "-c:v", "libx264", "-threads", "1", str(self.face))

    def test_reverse_blocks_and_multiple_loops_preserve_frame_order(self):
        info = timeline.prepare_cycle(self.face, self.root / "prepared")
        cycle = self.root / "prepared/cycle.mp4"
        source_pixels = pixels(self.root / "prepared/forward.mp4")
        expected = source_pixels + source_pixels[-2:0:-1]
        actual = pixels(cycle)
        self.assertEqual(len(actual), len(expected))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected)), 2)
        fps = Fraction(info["fps"])
        part = {"start_sample": round(120 / fps * 24000),
                "end_sample": round(280 / fps * 24000), "sample_rate": 24000}
        result = self.root / "part.mp4"
        plan = timeline.prepare_part(cycle, info, part, result)
        actual = pixels(result)
        expected = bytes(expected[(120 + n) % len(expected)] for n in range(plan["input_frames"]))
        self.assertEqual(len(actual), len(expected))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected)), 3)
        self.assertEqual((plan["start_frame"], plan["end_frame"]), (120, 280))
        self.assertGreaterEqual(media.probe(result, "video")["duration"],
                                (part["end_sample"] - part["start_sample"]) / 24000)

    def test_rotation_is_baked_into_pixels_without_copying_source_tags(self):
        rotated = self.root / "rotated.mov"
        ffmpeg("-display_rotation", "90", "-i", str(self.face), "-c", "copy",
               "-metadata", "location=+01.0000+002.0000/", str(rotated))
        original = media.probe(rotated, "video")["stream"]
        self.assertTrue(any(s.get("rotation") == 90 for s in original.get("side_data_list", [])))
        info = timeline.prepare_cycle(rotated, self.root / "prepared")
        self.assertEqual((info["width"], info["height"]), (240, 320))
        result = media.probe(self.root / "prepared/cycle.mp4", "video")["stream"]
        self.assertFalse(any(s.get("rotation") for s in result.get("side_data_list", [])))
        metadata = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json",
                                   str(self.root / "prepared/cycle.mp4")], capture_output=True, check=True).stdout
        self.assertNotIn(b"location", metadata)

    def test_large_vendor_frame_loss_is_rejected_instead_of_hidden(self):
        info = timeline.prepare_cycle(self.face, self.root / "prepared")
        part = {"start_frame": 0, "end_frame": 74}
        with self.assertRaisesRegex(RuntimeError, "达到4帧"):
            timeline.align_result(self.root / "prepared/forward.mp4", self.root / "aligned.mp4", info, part)
        self.assertFalse((self.root / "aligned.mp4").exists())

    def test_one_rounding_frame_does_not_shift_the_next_segment(self):
        info = timeline.prepare_cycle(self.face, self.root / "prepared")
        original = self.root / "prepared/forward.mp4"
        output = self.root / "aligned.mp4"
        delta = timeline.align_result(original, output, info, {"start_frame": 80, "end_frame": 151})
        self.assertEqual(delta, 1)
        actual, expected = pixels(output), pixels(original)
        self.assertEqual(len(actual), 71)
        self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected + expected[-1:])), 3)


if __name__ == "__main__":
    unittest.main()
