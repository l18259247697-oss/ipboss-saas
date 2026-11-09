"""Decode-based media checks shared by uploads and generated outputs."""
import json
import math
import struct
import subprocess
from pathlib import Path


def probe(path, kind):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("文件不是可读取的音视频，或文件已损坏")
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == kind), None)
    if not stream:
        raise RuntimeError("文件缺少" + ("音轨" if kind == "audio" else "视频画面"))
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("无法读取有效的媒体时长")
    return {"duration": duration, "stream": stream, "streams": data["streams"]}


def normalize_audio(source, destination):
    probe(source, "audio")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0", "-vn",
         "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-threads", "1", str(destination)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError("录音转换失败，原声音样本未替换")
    return probe(destination, "audio")


def normalize_video_for_browser(source, destination=None, expected_duration=None):
    """Encode a stable web-delivery MP4 from a valid generated video.

    Some vendor results are near-60fps H.264 files that decode with ffmpeg but
    render as a black surface in embedded Chromium. Keep the paid source
    untouched until this local, atomic delivery step succeeds.
    """
    source = Path(source)
    if expected_duration is None:
        expected_duration = probe(source, "audio")["duration"]
    video = probe(source, "video")["stream"]
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        numerator, denominator = (int(value) for value in str(rate).split("/", 1))
        source_fps = numerator / denominator if denominator else 0
    except (ValueError, ZeroDivisionError):
        source_fps = 0
    target_fps = 30 if source_fps > 30 else max(source_fps, 1)
    destination = source if destination is None else Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.stem + ".browser.pending.mp4")
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"fps={target_fps:g},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-profile:v", "main", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-b:a", "192k", "-movflags", "+faststart", str(pending),
    ], capture_output=True, text=True, timeout=600)
    if result.returncode:
        pending.unlink(missing_ok=True)
        raise RuntimeError("成片浏览器兼容化失败，原始成片已保留")
    try:
        delivered = probe(pending, "video")
        probe(pending, "audio")
        if abs(delivered["duration"] - expected_duration) > max(0.5, expected_duration * 0.05):
            raise RuntimeError("成片浏览器兼容化后时长不一致，原始成片已保留")
    except Exception:
        pending.unlink(missing_ok=True)
        raise
    pending.replace(destination)
    return str(destination)


def deliver_video_for_browser(source, destination, expected_duration):
    """Atomically publish a browser-compatible delivery after full validation."""
    destination = Path(destination)
    delivery = destination.with_name(destination.stem + ".browser.delivery.mp4")
    try:
        normalize_video_for_browser(source, delivery, expected_duration=expected_duration)
        check_video(delivery, expected_duration)
        delivery.replace(destination)
    except Exception:
        delivery.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return str(destination)


def finalize_wav(path, original):
    """Fix only observed vendor streaming lengths; preserve PCM and provenance."""
    raw = path.read_bytes()
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise RuntimeError("Expected a RIFF WAV file")
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    if riff_size == len(raw) - 8:
        return False
    position, block_align = 12, None
    while position + 8 <= len(raw):
        kind, size = struct.unpack_from("<4sI", raw, position)
        if kind == b"fmt " and size >= 16 and position + 8 + size <= len(raw):
            codec, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", raw, position + 8)
            if (codec, channels, bits) != (1, 1, 16) or rate not in {24000, 44100} or byte_rate != rate * 2:
                raise RuntimeError("Unexpected vendor WAV format; not modifying")
        if kind == b"data":
            actual = len(raw) - position - 8
            known_stream = ((riff_size, size) == (0x7fffffbf, 0x7fffff9b)
                            or (size == 0xffffff00 and riff_size == size + position))
            if not known_stream or block_align != 2 or actual % block_align:
                raise RuntimeError("Not the known streaming WAV header; not modifying")
            if original.exists() and original.read_bytes() != raw:
                raise RuntimeError("Original vendor audio would be overwritten")
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(raw)
            fixed = bytearray(raw)
            struct.pack_into("<I", fixed, 4, len(raw) - 8)
            struct.pack_into("<I", fixed, position + 4, actual)
            pending = path.with_suffix(".fixed.wav")
            pending.write_bytes(fixed)
            pending.replace(path)
            return True
        position += 8 + size + size % 2
    raise RuntimeError("Missing PCM data chunk")


def verify_audio(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
                             "-map", "0:a:0", "-threads", "1", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Generated audio failed full decode")


def check_video(path, expected_duration):
    info = probe(path, "video")
    audio = probe(path, "audio")
    tolerance = max(0.5, expected_duration * 0.05)
    if abs(info["duration"] - expected_duration) > tolerance or abs(audio["duration"] - expected_duration) > tolerance:
        raise RuntimeError("成片音视频时长与配音不一致，不能作为完整成片交付")
    result = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
                             "-map", "0:v:0", "-map", "0:a:0", "-threads", "1", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("成片解码检查失败")
    return info
