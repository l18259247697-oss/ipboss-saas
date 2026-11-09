"""Run one real over-120-second VideoRetalk segmentation test.

The approved existing 134.32-second voice is reused to avoid a second ASR/TTS
charge. This calls the production segmented_lipsync path, permits only the two
expected missing segment submissions, and stops on budget or unknown state.
"""
import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_DATA_DIR", str(ROOT / "data/e2e-20260928"))
os.environ.setdefault("APP_OUTPUTS_DIR", str(ROOT / "outputs/e2e-20260928"))
os.environ.setdefault("APP_CONFIG_FILE", str(ROOT / "data/config.json"))
import sys
sys.path.insert(0, str(ROOT / "backend"))

from config import load_config
from services import media, segmented_lipsync


PRICE = 0.08
PRIOR_UNUSED = 2.0512
NEW_LIMIT = 10.0
HARD_LIMIT = PRIOR_UNUSED + NEW_LIMIT


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def save(path, payload):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "outputs/e2e-20260928/long-segment-e2e-20261017/final/video.mp4")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output = output.resolve()
    folder = output.parent
    ledger_path = folder.parent / "budget.json"
    face = ROOT / "data/e2e-20260928/faces/u3.mov"
    audio = ROOT / "outputs/e2e-20260928/f79f1d7b06f9/voice.wav"
    cfg = load_config()["lipsync"]
    if cfg.get("provider") != "dashscope" or not cfg.get("api_key"):
        raise RuntimeError("未配置北京 VideoRetalk，未发起请求")
    if not face.exists() or not audio.exists():
        raise RuntimeError("长片验收输入缺失，未发起请求")
    if folder.exists() and any(folder.glob("part-*/videoretalk-task.json")):
        raise RuntimeError("已有长片供应商任务记录，未知状态下禁止自动重试")
    folder.mkdir(parents=True, exist_ok=True)
    ledger = {
        "date": "2026-10-17", "scope": "real over-120-second segmented VideoRetalk E2E",
        "model": "videoretalk", "face_sha256": digest(face), "audio_sha256": digest(audio),
        "audio_duration": media.probe(audio, "audio")["duration"],
        "prior_unused_cny": PRIOR_UNUSED, "new_authorized_cny": NEW_LIMIT,
        "hard_run_limit_cny": HARD_LIMIT, "planned_cny": 0.0, "actual_cny": 0.0,
        "status": "preparing", "new_paid_submissions": 0,
        "no_new_asr_or_tts": True,
    }
    save(ledger_path, ledger)
    original = segmented_lipsync.videoretalk.generate

    def guarded_generate(part_face, part_audio, part_output, provider_cfg, **kwargs):
        duration = media.probe(part_audio, "audio")["duration"]
        estimate = duration * PRICE
        if ledger["planned_cny"] + estimate > HARD_LIMIT + 1e-6:
            ledger["status"] = "budget_stopped"
            save(ledger_path, ledger)
            raise RuntimeError(f"本次分段预计超过授权额度，未提交: {estimate:.4f} 元")
        ledger["planned_cny"] += estimate
        ledger["new_paid_submissions"] += 1
        ledger["status"] = "provider_running"
        save(ledger_path, ledger)
        try:
            result = original(part_face, part_audio, part_output, provider_cfg, **kwargs)
        except Exception as exc:
            state_file = Path(part_output).parent / "videoretalk-task.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if state.get("status") == "submitting" and not state.get("task_id"):
                    ledger["status"] = "unknown_submission"
                    save(ledger_path, ledger)
            raise exc
        state_file = Path(part_output).parent / "videoretalk-task.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        usage = state.get("usage", {})
        actual = float(usage.get("video_duration", duration)) * PRICE
        ledger["actual_cny"] += actual
        ledger["status"] = "provider_part_succeeded"
        save(ledger_path, ledger)
        return result

    segmented_lipsync.videoretalk.generate = guarded_generate
    try:
        result = segmented_lipsync.generate(face, audio, output, cfg)
        state = json.loads((folder / "segments.json").read_text(encoding="utf-8"))
        ledger["status"] = "complete"
        ledger["result_sha256"] = digest(result)
        ledger["parts"] = [{"name": p["name"], "duration": p["duration"],
                             "task_id": json.loads((folder / p["name"] / "videoretalk-task.json").read_text()) .get("task_id")}
                            for p in state["parts"]]
        save(ledger_path, ledger)
        print(json.dumps({"status": "complete", "output": str(result),
                          "planned_cny": round(ledger["planned_cny"], 6),
                          "actual_cny": round(ledger["actual_cny"], 6),
                          "parts": len(state["parts"])}, ensure_ascii=False))
    except Exception:
        if ledger["status"] not in {"unknown_submission", "budget_stopped"}:
            ledger["status"] = "failed_before_or_after_known_submission"
            save(ledger_path, ledger)
        raise


if __name__ == "__main__":
    main()
