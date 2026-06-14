"""步骤③音频生成：edge-tts 免费配音（无需 key）。"""
import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts


def _rate_str(speed: float) -> str:
    # speed 1.0 -> +0%, 1.5 -> +50%, 0.8 -> -20%
    pct = int(round((speed - 1.0) * 100))
    return f"{pct:+d}%"


async def _synth(text, voice, rate, out_path):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(out_path)


def audio_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return 0.0


def generate_audio(text: str, voice: str, speed: float, job_id: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "voice.mp3"
    rate = _rate_str(speed or 1.0)
    asyncio.run(_synth(text, voice or "zh-CN-XiaoxiaoNeural", rate, str(out_path)))
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("配音生成失败（edge-tts 无输出）")
    return {
        "path": str(out_path),
        "url": f"/outputs/{job_id}/voice.mp3",
        "duration": audio_duration(out_path),
    }
