"""One frame-indexed ping-pong timeline shared by all paid lip-sync segments."""
import math
import subprocess
from fractions import Fraction
from pathlib import Path

from services import media

REVERSE_BLOCK_FRAMES = 24
MAX_VENDOR_FRAME_DELTA = 4
ENCODING = ["-an", "-sn", "-dn", "-map_metadata", "-1", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-bf", "0",
            "-threads", "2", "-movflags", "+faststart"]


def _render(arguments, target):
    target = Path(target)
    pending = target.with_suffix(".pending.mp4")
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2",
                             "-filter_threads", "1", *arguments, *ENCODING, str(pending)],
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError("人物连续画面准备失败，未提交新的口型任务")
    pending.replace(target)


def frame_count(path):
    stream = media.probe(path, "video")["stream"]
    count = int(stream.get("nb_frames") or 0)
    if count <= 0:
        raise RuntimeError("无法核对人物视频帧数，未提交口型任务")
    return count


def prepare_cycle(face, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stream = media.probe(face, "video")["stream"]
    fps = Fraction(stream.get("avg_frame_rate") or stream["r_frame_rate"])
    if not 15 <= fps <= 60:
        raise RuntimeError("人物视频帧率不支持连续分段")
    forward = folder / "forward.mp4"
    # FFmpeg applies the original display rotation before encoding, then strips tags.
    _render(["-i", str(face), "-map", "0:v:0", "-vf", f"fps={fps},setpts=PTS-STARTPTS",
             "-video_track_timescale", str(fps.numerator)], forward)
    frames = frame_count(forward)
    if frames < 3:
        raise RuntimeError("人物底版帧数不足")
    clips = [forward]
    end = frames - 1
    while end > 1:
        start = max(1, end - REVERSE_BLOCK_FRAMES)
        reverse = folder / f"reverse-{start:06d}.mp4"
        # Seek half a frame early; buffer at most one small block, not the whole video.
        _render(["-ss", f"{float((start - Fraction(1, 2)) / fps):.9f}", "-i", str(forward),
                 "-map", "0:v:0", "-vf", f"trim=end_frame={end-start},reverse,setpts=PTS-STARTPTS",
                 "-frames:v", str(end - start), "-video_track_timescale", str(fps.numerator)], reverse)
        if frame_count(reverse) != end - start:
            raise RuntimeError("人物倒放帧数校验失败")
        clips.append(reverse)
        end = start
    listing = folder / "cycle.txt"
    listing.write_text("".join(f"file '{clip.name}'\n" for clip in clips))
    cycle = folder / "cycle.mp4"
    pending = folder / "cycle.pending.mp4"
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat",
                             "-safe", "1", "-i", str(listing), "-map", "0:v:0", "-c", "copy",
                             "-map_metadata", "-1", "-video_track_timescale", str(fps.numerator),
                             "-movflags", "+faststart", str(pending)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode or frame_count(pending) != frames * 2 - 2:
        raise RuntimeError("人物循环时间线校验失败")
    pending.replace(cycle)
    normalized = media.probe(cycle, "video")["stream"]
    return {"fps": str(fps), "source_frames": frames, "cycle_frames": frames * 2 - 2,
            "width": normalized["width"], "height": normalized["height"]}


def prepare_part(cycle, timeline, part, destination, *, last=False):
    fps = Fraction(timeline["fps"])
    start = round(Fraction(part["start_sample"], part["sample_rate"]) * fps)
    end_time = Fraction(part["end_sample"], part["sample_rate"]) * fps
    end = math.ceil(end_time) if last else round(end_time)
    request_start = round(Fraction(part.get("request_start_sample", part["start_sample"]),
                                    part["sample_rate"]) * fps)
    request_end = part.get("request_end_sample", part["end_sample"])
    # Guard frames ensure video_extension=False never truncates the input speech.
    input_frames = math.ceil(Fraction(request_end - part.get("request_start_sample", part["start_sample"]),
                                      part["sample_rate"]) * fps) + 2
    phase = request_start % timeline["cycle_frames"]
    _render(["-stream_loop", "-1", "-i", str(cycle), "-map", "0:v:0",
             "-vf", f"trim=start_frame={phase}:end_frame={phase+input_frames},setpts=PTS-STARTPTS",
             "-frames:v", str(input_frames), "-video_track_timescale", str(fps.numerator)], destination)
    if frame_count(destination) != input_frames:
        raise RuntimeError("人物分段时间线不完整，未提交口型任务")
    return {"start_frame": start, "end_frame": end, "input_frames": input_frames,
            "trim_start_frame": start - request_start}


def align_result(source, destination, timeline, part):
    """Absorb a few high-fps vendor rounding frames; never stretch video/audio."""
    fps = Fraction(timeline["fps"])
    stream = media.probe(source, "video")["stream"]
    if any(stream[k] != timeline[k] for k in ("width", "height")):
        raise RuntimeError("口型返回画面尺寸改变，已保留原结果，未自动重做")
    if Fraction(stream["avg_frame_rate"]) != fps:
        raise RuntimeError("口型返回帧率改变，已保留原结果，未自动重做")
    expected = part["end_frame"] - part["start_frame"]
    actual = frame_count(source)
    request_expected = part.get("input_frames", expected + 2) - 2
    trim_start = part.get("trim_start_frame", 0)
    delta = max(0, trim_start + expected - actual)
    if abs(request_expected - actual) >= MAX_VENDOR_FRAME_DELTA or delta >= MAX_VENDOR_FRAME_DELTA:
        raise RuntimeError(f"口型返回帧数偏差达到{MAX_VENDOR_FRAME_DELTA}帧，已保留原结果，未自动重做")
    _render(["-i", str(source), "-map", "0:v:0", "-vf",
             f"tpad=stop_mode=clone:stop={MAX_VENDOR_FRAME_DELTA},trim=start_frame={trim_start}:end_frame={trim_start+expected},setpts=PTS-STARTPTS",
             "-frames:v", str(expected), "-video_track_timescale", str(fps.numerator)], destination)
    if frame_count(destination) != expected:
        raise RuntimeError("口型分段对齐校验失败")
    return delta
