"""步骤①文案生成：扒抖音爆款链接 -> 提取口播文案。
复用「抖音爆款拆解」skill 的备料脚本（自带 f2 下载 + 本机 Whisper 转写）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from config import DOUYIN_PREP, OUTPUTS_DIR, ROOT_DIR, load_config
from services import media, transcribe


def _find_manifest(outdir: Path):
    for m in outdir.rglob("manifest.json"):
        return m
    return None


def _failure_message(output: str) -> str:
    # Subprocess logs can contain signed URLs; never return raw logs to a user.
    blocked = re.search(r"\b403\s+Forbidden\b|Status Code:\s*403\b|HTTP\s*403\b", output, re.I)
    if blocked:
        stage = "作品详情接口" if re.search(r"aweme/detail|fetch_post_detail|fetch_one_video", output) else "链接提取接口"
        return (f"抖音{stage}拒绝访问（HTTP 403），当前提取方式未通过；"
                "不代表作品已删除。本次未进入付费生成，可先使用原文输入")
    if "ModuleNotFoundError" in output or "找不到 f2" in output:
        return "链接提取依赖未就绪，请检查下载器和 Python 环境；本次未进入付费生成"
    if re.search(r"\b429\s+Too Many Requests\b|Status Code:\s*429\b", output, re.I):
        return "链接提取受到限流，请稍后再试；本次未进入付费生成"
    return "链接提取未成功，尚未取得原视频文案；本次未进入付费生成，可先使用原文输入"


def extract_from_link(link: str, job_id: str, hook_seconds: int | None = None) -> dict:
    """返回 {transcript, title, raw_copy, duration, cover}。失败抛异常。"""
    if not DOUYIN_PREP.exists():
        raise RuntimeError(
            f"未找到备料脚本：{DOUYIN_PREP}。请确认已安装『抖音爆款拆解』skill。"
        )
    outdir = OUTPUTS_DIR / job_id / "prep"
    outdir.mkdir(parents=True, exist_ok=True)
    asr = load_config().get("asr", {})
    cloud = asr.get("provider", "local") == "dashscope"
    if asr.get("provider", "local") not in {"local", "dashscope"}:
        raise RuntimeError("未知转写服务，未自动切换")
    if cloud and hook_seconds:
        raise RuntimeError("云端转写当前仅支持完整原片，未截取口播")
    if cloud and not asr.get("api_key"):
        raise RuntimeError("云端转写未配置百炼北京 API Key")

    cmd = [sys.executable, str(DOUYIN_PREP), link, "--outdir", str(outdir)]
    if cloud:
        cmd += ["--no-transcribe", "--frames", "4"]
    if hook_seconds:
        cmd += ["--hook-seconds", str(hook_seconds)]

    env = dict(os.environ)
    env.setdefault("OMP_NUM_THREADS", "2")
    env.setdefault("MKL_NUM_THREADS", "2")
    local_python = ROOT_DIR / ".venv" / "bin" / "python"
    if local_python.exists():
        env.setdefault("DOUYIN_VENV_PY", str(local_python))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("链接提取超时，尚未取得原视频文案；本次未进入付费生成") from exc
    if proc.returncode != 0:
        raise RuntimeError(_failure_message((proc.stderr or "") + "\n" + (proc.stdout or "")))

    manifest_path = _find_manifest(outdir)
    if not manifest_path:
        raise RuntimeError("扒链接完成但未找到 manifest.json")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = {}
    if cloud:
        video = Path(data.get("视频文件") or "").resolve()
        if not video.is_file() or outdir.resolve() not in video.parents:
            raise RuntimeError("未找到本任务下载的原视频，不能用标题代替转写")
        audio = outdir / "source-audio.wav"
        media.normalize_audio(video, audio)
        result = transcribe.transcribe_audio(audio, outdir / "source-asr.json", asr)
        transcript = result["transcript"]
        evidence = {"transcript_model": result["model"], "transcript_request_id": result["request_id"]}
    else:
        transcript = (data.get("口播文案") or "").strip()
    if not cloud and not transcript:
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
        **evidence,
        "transcript": transcript,
        "title": (data.get("文案") or {}).get("标题", ""),
        "raw_copy": (data.get("文案") or {}).get("原始文案", ""),
        "duration": data.get("时长秒"),
        "cover": cover_rel,
    }


def clean_link(text: str) -> str:
    """从抖音分享口令里抠出链接。"""
    m = re.search(r"https?://[^\s,，；;）)】]+", text or "")
    return m.group(0).rstrip("。.!！?？") if m else (text or "").strip()
