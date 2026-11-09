"""Split only the vendor requests, never the spoken script or final audio."""
import fcntl
import hashlib
import json
import math
import subprocess
import wave
from array import array
from fractions import Fraction
from pathlib import Path

from services import face_timeline, media, videoretalk
from services.videoretalk import PendingTask, _write_state

SINGLE_LIMIT = 120
HIGH_FPS_LIMIT = 59.5
PLAN_VERSION = 3


def _hash(path):
    with Path(path).open("rb") as f:
        digest = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def _segment_limit(face_info):
    stream = face_info.get("stream", {})
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not rate or "/" not in str(rate):
        return SINGLE_LIMIT
    try:
        numerator, denominator = (int(value) for value in str(rate).split("/", 1))
    except ValueError:
        return SINGLE_LIMIT
    fps = numerator / denominator if denominator else 0
    # VideoRetalk rejects non-extension requests over 60s when fps is high;
    # leave room for the 0.25s context on each side of a core segment.
    return min(SINGLE_LIMIT, HIGH_FPS_LIMIT) if fps > 40 else SINGLE_LIMIT


def _partition(audio, folder, segment_limit=SINGLE_LIMIT):
    normalized = folder / "full-audio.wav"
    media.normalize_audio(audio, normalized)
    with wave.open(str(normalized), "rb") as source:
        rate, total = source.getframerate(), source.getnframes()
        pcm = source.readframes(total)
    count = math.ceil(total / rate / (segment_limit - min(10, segment_limit / 5)))
    cuts = [0]
    for i in range(1, count):
        center = round(total * i / count)
        # Prefer a quiet 100 ms window near the balanced boundary, without gaps.
        radius, window = round(rate * 1.5), round(rate * 0.1)
        low = max(center - radius, cuts[-1] + round(rate * 2.05))
        high = min(center + radius, total - round(rate * 2.05 * (count - i)),
                   cuts[-1] + round(rate * (segment_limit - 0.5)))
        candidates = range(low, high + 1, max(1, window // 2))
        def energy(position):
            samples = array("h", pcm[(position - window // 2) * 2:(position + window // 2) * 2])
            return (sum(x * x for x in samples) / max(len(samples), 1), abs(position - center))
        cuts.append(min(candidates, key=energy))
    cuts.append(total)
    parts = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:]), 1):
        part = folder / f"part-{index:03d}"
        part.mkdir(exist_ok=True)
        path = part / "audio.wav"
        with wave.open(str(path), "wb") as dest:
            dest.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            dest.writeframes(pcm[start * 2:end * 2])
        duration = (end - start) / rate
        if not 2 < duration < segment_limit:
            raise RuntimeError("音频分段长度校验失败，未提交口型服务")
        parts.append({"name": part.name, "start_sample": start, "end_sample": end,
                      "sample_rate": rate, "duration": duration, "audio_sha256": _hash(path)})
    return parts


def _request_audio(folder, part, timeline):
    fps = Fraction(timeline["fps"])
    with wave.open(str(folder / "full-audio.wav"), "rb") as source:
        rate, total = source.getframerate(), source.getnframes()
        context = round(fps / 4)
        start_frame = max(0, round(Fraction(part["start_sample"], rate) * fps) - context)
        start = round(start_frame / fps * rate)
        end = min(total, part["end_sample"] + round(rate / 4))
        source.setpos(start)
        pcm = source.readframes(end - start)
        params = source.getparams()
    path = folder / part["name"] / "request-audio.wav"
    with wave.open(str(path), "wb") as dest:
        dest.setparams(params)
        dest.writeframes(pcm)
    return {"request_start_sample": start, "request_end_sample": end,
            "request_duration": (end - start) / rate, "request_audio_sha256": _hash(path)}


def generate(face, audio, output, cfg):
    duration = media.probe(audio, "audio")["duration"]
    if not cfg.get("api_key"):
        raise RuntimeError("未配置口型服务 API Key")
    face_info = videoretalk.validate_face(face)
    segment_limit = _segment_limit(face_info)
    if duration < segment_limit:
        return videoretalk.generate(face, audio, output, cfg)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256((_hash(face) + _hash(audio) + videoretalk.MODEL + cfg["api_key"]).encode()).hexdigest()
    manifest = output.parent / "segments.json"
    with (output.parent / "segments.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(manifest.read_text()) if manifest.exists() else {}
        if state and state.get("fingerprint") != fingerprint:
            raise PendingTask("长视频输入或账号已改变，未复用旧分段或重复生成")
        if state.get("status") == "complete":
            if not output.exists() or _hash(output) != state.get("sha256"):
                raise PendingTask("已完成长视频丢失或改变，未自动重做")
            return str(output)
        if state and state.get("plan_version") != PLAN_VERSION:
            raise PendingTask("旧分段计划存在画面接缝风险，已保留原任务；未切换输入或重复计费")
        if not state:
            if any(output.parent.glob("part-*/videoretalk-task.json")):
                raise PendingTask("已有口型任务但分段计划丢失，未覆盖原素材或重新提交")
            state = {"fingerprint": fingerprint, "duration": duration, "status": "processing",
                     "plan_version": PLAN_VERSION,
                     "segment_limit": segment_limit,
                     "parts": _partition(audio, output.parent, segment_limit)}
            state["full_audio_sha256"] = _hash(output.parent / "full-audio.wav")
            _write_state(manifest, state)
        full_audio = output.parent / "full-audio.wav"
        if not full_audio.exists() or _hash(full_audio) != state.get("full_audio_sha256"):
            raise PendingTask("完整配音检查点丢失或改变，未准备新上下文或重复提交")
        cycle = output.parent / "face-timeline/cycle.mp4"
        if not state.get("timeline"):
            if any(output.parent.glob("part-*/videoretalk-task.json")):
                raise PendingTask("已有口型任务但缺少人物时间线记录，未重新准备或提交")
            state["timeline"] = face_timeline.prepare_cycle(face, cycle.parent)
            state["timeline"]["sha256"] = _hash(cycle)
            _write_state(manifest, state)
        if not cycle.exists() or _hash(cycle) != state["timeline"]["sha256"]:
            raise PendingTask("人物时间线丢失或改变，未重复提交")
        # Finish and persist every input before any external paid action.
        for index, part in enumerate(state["parts"]):
            prepared = output.parent / part["name"] / "face.mp4"
            if not part.get("face_sha256"):
                if (prepared.parent / "videoretalk-task.json").exists():
                    raise PendingTask("已有口型任务但缺少对应人物分段，未重新提交")
                part.update(_request_audio(output.parent, part, state["timeline"]))
                part.update(face_timeline.prepare_part(cycle, state["timeline"], part, prepared,
                                                      last=index == len(state["parts"]) - 1))
                part["face_sha256"] = _hash(prepared)
                _write_state(manifest, state)
            if not prepared.exists() or _hash(prepared) != part["face_sha256"]:
                raise PendingTask("人物分段丢失或改变，未重复提交")
            chunk_audio = prepared.parent / "audio.wav"
            if not chunk_audio.exists() or _hash(chunk_audio) != part["audio_sha256"]:
                raise PendingTask("分段音频丢失或改变，未自动重新提交")
            request_audio = prepared.parent / "request-audio.wav"
            if not request_audio.exists() or _hash(request_audio) != part["request_audio_sha256"]:
                raise PendingTask("口型上下文音频丢失或改变，未重复提交")
        for part in state["parts"]:
            folder = output.parent / part["name"]
            chunk_audio, video = folder / "audio.wav", folder / "video.mp4"
            if not chunk_audio.exists() or _hash(chunk_audio) != part["audio_sha256"]:
                raise PendingTask("分段音频丢失或改变，未自动重新提交")
            if part.get("video_sha256"):
                if not video.exists() or _hash(video) != part["video_sha256"]:
                    raise PendingTask("已完成的分段成片丢失或改变，未自动重新生成")
            else:
                videoretalk.generate(folder / "face.mp4", folder / "request-audio.wav", video, cfg,
                                     video_extension=False)
                media.check_video(video, part["request_duration"])
                part["video_sha256"] = _hash(video)
                _write_state(manifest, state)
            aligned = folder / "timeline.mp4"
            if part.get("aligned_sha256"):
                if not aligned.exists() or _hash(aligned) != part["aligned_sha256"]:
                    raise PendingTask("已对齐的分段丢失或改变，未自动重做")
            else:
                try:
                    part["rounding_frames"] = face_timeline.align_result(video, aligned, state["timeline"], part)
                except RuntimeError as exc:
                    raise PendingTask(str(exc)) from exc
                part["aligned_sha256"] = _hash(aligned)
                _write_state(manifest, state)
        listing = output.parent / "concat.txt"
        listing.write_text("".join(f"file '{part['name']}/timeline.mp4'\n" for part in state["parts"]))
        pending = output.with_name("joined.pending.mp4")
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
            "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-threads", "1", "-movflags", "+faststart", str(pending),
        ], capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise PendingTask("分段已生成但本地拼接失败，可复用原分段继续")
        try:
            media.deliver_video_for_browser(pending, output, duration)
        except RuntimeError as exc:
            raise PendingTask(str(exc)) from exc
        try:
            media.check_video(output, duration)
        except RuntimeError as exc:
            output.unlink(missing_ok=True)
            raise PendingTask(str(exc)) from exc
        state.update(status="complete", sha256=_hash(output))
        _write_state(manifest, state)
        return str(output)
