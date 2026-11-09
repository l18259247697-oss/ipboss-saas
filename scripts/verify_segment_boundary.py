"""Isolated 16-second seam test and 7-second context follow-up, NOT full E2E.

Default: prepare locally and stop before any API call. --submit: reuse the approved
voice/face, sequentially call VideoRetalk within the existing cumulative budget.
Never edit the old failed-quality job or synthesize another voice.
"""
import argparse
import json
import os
import sys
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_DATA_DIR", str(ROOT / "data/e2e-20260928"))
os.environ.setdefault("APP_OUTPUTS_DIR", str(ROOT / "outputs/e2e-20260928"))
os.environ.setdefault("APP_CONFIG_FILE", str(ROOT / "data/config.json"))
sys.path.insert(0, str(ROOT / "backend"))
from config import load_config
from services import media, segmented_lipsync as segmented
from services.videoretalk import PendingTask, _write_state

KNOWN_TASKS = {
    "7bbf7835-b71f-40d9-9060-7d44a99b1e49", "62d58311-a801-42eb-bfbf-9ad60fc3174a",
    "8dfa1b43-2539-455e-a992-ec1cf561137a", "a1d384e5-d0c9-4fba-ad29-3f9189db21c6",
    "ec47cec1-4daa-4f71-8e13-57c54321d1cb", "bd068f52-5c39-4841-994e-decaed897767",
}


class PreparedLocally(Exception):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--context-check", action="store_true", help="7-second overlap regression after the 16-second test")
    args = parser.parse_args()
    seconds = 7 if args.context_check else 16
    folder = ROOT / "outputs/e2e-20260928" / ("seam-context-20261014" if args.context_check else "seam-20261014")
    expected_tasks = set(KNOWN_TASKS)
    if args.context_check:
        expected_tasks.update({"5d2857ea-a67f-453f-a9f5-32da1a97fdc7", "3480717c-b2a2-4cb6-a238-1571bc972ce5"})
    folder.mkdir(parents=True, exist_ok=True)
    face = ROOT / "data/e2e-20260928/faces/u3.mov"
    original = ROOT / "outputs/e2e-20260928/f79f1d7b06f9/voice.wav"
    audio = folder / "voice.wav"
    with wave.open(str(original), "rb") as source:
        params = source.getparams()
        pcm = source.readframes(params.framerate * seconds)
    if audio.exists():
        with wave.open(str(audio), "rb") as existing:
            if existing.getparams()[:3] != params[:3] or existing.readframes(existing.getnframes()) != pcm:
                raise RuntimeError("Boundary-test input changed; not overwriting")
    else:
        with wave.open(str(audio), "wb") as dest:
            dest.setparams(params)
            dest.writeframes(pcm)
    prior_usage = 0
    seen = set()
    for path in (ROOT / "outputs").rglob("videoretalk-task.json"):
        if folder in path.parents:
            continue
        state = json.loads(path.read_text())
        if state.get("task_id") not in expected_tasks or state.get("status") != "SUCCEEDED":
            raise RuntimeError("Unreconciled prior API task; do not spend")
        seen.add(state["task_id"])
        prior_usage += state["usage"]["video_duration"] * 0.08
    if seen != expected_tasks:
        raise RuntimeError("Prior budget evidence missing")
    cfg = load_config()["lipsync"]
    if cfg.get("provider") != "dashscope" or not cfg.get("api_key"):
        raise RuntimeError("Expected configured Beijing VideoRetalk account")
    duration = media.probe(audio, "audio")["duration"]
    if duration != seconds:
        raise RuntimeError("Unexpected test duration")
    ledger = {"date": "2026-10-14", "scope": "isolated segment-boundary test, not full E2E",
              "cumulative_limit_cny": 20, "prior_reserved_cny": 19.30 if args.context_check else 18,
              "prior_known_lipsync_list_price_cny": round(prior_usage, 6),
              "new_max_list_price_cny": 0.64 if args.context_check else 1.30,
              "planned_total_reserved_cny": 19.94 if args.context_check else 19.30,
              "actual_account_bill_verified": False, "model": "videoretalk",
              "face_sha256": segmented._hash(face), "audio_sha256": segmented._hash(audio),
              "source_voice_sha256": segmented._hash(original), "voice_seconds": duration,
              "no_new_tts_or_asr": True, "paid_submission_enabled": args.submit}
    _write_state(folder / "budget.json", ledger)
    output = folder / "final/video.mp4"
    # Exercise exactly the production segmentation path with a shorter test-only limit.
    with patch.object(segmented, "SINGLE_LIMIT", 5 if args.context_check else 10):
        if args.submit:
            segmented.generate(face, audio, output, cfg)
        else:
            with patch.object(segmented.videoretalk, "generate", side_effect=PreparedLocally):
                try:
                    segmented.generate(face, audio, output, cfg)
                except PreparedLocally:
                    pass
    state = json.loads((output.parent / "segments.json").read_text())
    if state["status"] == "complete":
        info = media.check_video(output, duration)
        with wave.open(str(audio), "rb") as source:
            original_pcm = source.readframes(source.getnframes())
        split_pcm = bytearray()
        tasks = []
        for part in state["parts"]:
            with wave.open(str(output.parent / part["name"] / "audio.wav"), "rb") as source:
                split_pcm.extend(source.readframes(source.getnframes()))
            vendor = json.loads((output.parent / part["name"] / "videoretalk-task.json").read_text())
            tasks.append({k: vendor[k] for k in ("task_id", "status", "usage")})
        if original_pcm != split_pcm:
            raise RuntimeError("Core audio samples changed")
        _write_state(folder / "result-evidence.json", {
            "scope": ledger["scope"], "plan_version": state["plan_version"],
            "video_sha256": segmented._hash(output), "video_duration": info["duration"],
            "audio_duration": media.probe(output, "audio")["duration"],
            "width": info["stream"]["width"], "height": info["stream"]["height"],
            "decode_passed": True, "core_pcm_equal": True, "core_pcm_bytes": len(original_pcm),
            "tasks": tasks, "list_price_estimate_cny": round(sum(t["usage"]["video_duration"] for t in tasks) * 0.08, 6),
            "quality_review": "pending", "full_e2e": "not_run"})
    print(json.dumps({"status": state["status"], "scope": ledger["scope"],
                      "parts": [{k: p[k] for k in ("name", "duration", "start_frame", "end_frame")}
                                for p in state["parts"]],
                      "budget_reserved_cny": ledger["planned_total_reserved_cny"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
