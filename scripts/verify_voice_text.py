"""Local-only completeness check. This is not a voice-similarity quality score."""
import argparse
import difflib
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

os.environ.update(OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--outputs-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{12}", args.job_id):
        raise ValueError("Expected a product job ID")
    checkpoint = Path.home() / ".cache/whisper/small.pt"
    if not checkpoint.is_file():
        raise RuntimeError("Local Whisper checkpoint missing; no download or cloud fallback")
    with sqlite3.connect(f"file:{args.data_dir / 'app.db'}?mode=ro", uri=True) as db:
        row = db.execute("SELECT result FROM jobs WHERE id=?", (args.job_id,)).fetchone()
    script = json.loads(row[0])["rewritten"]
    audio = args.outputs_dir / args.job_id / "voice.wav"
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = args.report_dir / "voice-whisper.json"
    identity = {"audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(script.encode()).hexdigest()}
    if report.exists():
        result = json.loads(report.read_text())
        if result["identity"] != identity:
            raise RuntimeError("Existing local verification belongs to different input")
        recognized = result["recognized"]
    else:
        import torch
        import whisper
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        model = whisper.load_model(str(checkpoint), device="cpu")
        recognized = model.transcribe(str(audio), language="zh", fp16=False, temperature=0,
                                     condition_on_previous_text=False, verbose=False,
                                     initial_prompt="以下是简体中文的普通话口播。")
        report.write_text(json.dumps({"identity": identity, "recognized": recognized},
                                     ensure_ascii=False, indent=2))
    normalize = lambda value: "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", value)).lower()
    original, actual = normalize(script), normalize(recognized["text"])
    matcher = difflib.SequenceMatcher(None, original, actual, autojunk=False)
    changes = [{"type": tag, "script": original[a:b], "asr": actual[c:d],
                "context": original[max(0, a - 12):b + 12]}
               for tag, a, b, c, d in matcher.get_opcodes() if tag != "equal"]
    summary = {"job_id": args.job_id, "method": "Whisper small, local CPU 2 threads, no cloud",
               "script_characters": len(script), "normalized_characters": len(original),
               "similarity": matcher.ratio(), "not_a_voice_similarity_score": True,
               "differences": changes, "identity": identity}
    (args.report_dir / "voice-text-comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
