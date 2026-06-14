"""步骤①文案生成：扒抖音爆款链接 -> 提取口播文案。
复用「抖音爆款拆解」skill 的备料脚本（自带 f2 下载 + 本机 Whisper 转写）。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from config import DOUYIN_PREP, OUTPUTS_DIR


def _find_manifest(outdir: Path):
    for m in outdir.rglob("manifest.json"):
        return m
    return None


def extract_from_link(link: str, job_id: str, hook_seconds: int | None = None) -> dict:
    """返回 {transcript, title, raw_copy, duration, cover}。失败抛异常。"""
    if not DOUYIN_PREP.exists():
        raise RuntimeError(
            f"未找到备料脚本：{DOUYIN_PREP}。请确认已安装『抖音爆款拆解』skill。"
        )
    outdir = OUTPUTS_DIR / job_id / "prep"
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(DOUYIN_PREP), link, "--outdir", str(outdir)]
    if hook_seconds:
        cmd += ["--hook-seconds", str(hook_seconds)]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"扒链接失败：{tail}")

    manifest_path = _find_manifest(outdir)
    if not manifest_path:
        raise RuntimeError("扒链接完成但未找到 manifest.json")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    transcript = (data.get("口播文案") or "").strip()
    if not transcript:
        # 兜底读 transcript.txt
        t = manifest_path.parent / "transcript.txt"
        if t.exists():
            transcript = t.read_text(encoding="utf-8").strip()
    if not transcript:
        raise RuntimeError("未能提取到口播文案（视频可能无人声）")

    cover_rel = None
    cover_path = manifest_path.parent / (data.get("封面") or "cover.jpg")
    if cover_path.exists():
        cover_rel = f"/outputs/{job_id}/prep/{cover_path.relative_to(outdir)}"

    return {
        "transcript": transcript,
        "title": (data.get("文案") or {}).get("标题", ""),
        "raw_copy": (data.get("文案") or {}).get("原始文案", ""),
        "duration": data.get("时长秒"),
        "cover": cover_rel,
    }


def clean_link(text: str) -> str:
    """从抖音分享口令里抠出链接。"""
    m = re.search(r"https?://[^\s,，]+", text or "")
    return m.group(0) if m else (text or "").strip()
