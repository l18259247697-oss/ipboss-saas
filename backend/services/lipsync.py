"""本地·免费对口型引擎（Wav2Lip + MPS 苹果GPU加速）。
   输入：一张正脸照片或一段人脸视频 + 配音 -> 输出：嘴型对上配音的 mp4。
   零 API、零授权费。生产环境可整体迁到 GPU 服务器加速。"""
import os
import subprocess
from pathlib import Path

from config import load_config
from services import media
from services.health import check_server

ROOT = Path(__file__).resolve().parent.parent.parent
W2L_DIR = ROOT / "engines" / "Wav2Lip"
CKPT = W2L_DIR / "checkpoints" / "wav2lip_gan.pth"


def _lscfg() -> dict:
    return load_config().get("lipsync", {}) or {}


def available() -> bool:
    c = _lscfg()
    if c.get("provider") == "dashscope":
        return bool(c.get("api_key"))
    if (c.get("provider") or "").lower() == "server":
        return bool(c.get("base_url"))
    return CKPT.exists() and (W2L_DIR / "inference.py").exists()


def check_ready():
    c = _lscfg()
    if not available():
        raise RuntimeError("口型服务未配置，请填写可用的 API Key")
    if c.get("provider") == "server":
        check_server(c.get("base_url"), "口型服务")


# ---------- 自建 GPU 服务器 MuseTalk HTTP API ----------
def _lipsync_server(face_path, audio_path, out_path, c) -> str:
    import httpx
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base = c["base_url"].rstrip("/")
    with httpx.Client(timeout=1800) as cli:
        with open(face_path, "rb") as ff, open(audio_path, "rb") as af:
            r = cli.post(
                f"{base}/lipsync",
                files={
                    "face": (Path(face_path).name, ff, "application/octet-stream"),
                    "audio": (Path(audio_path).name, af, "application/octet-stream"),
                },
            )
        if r.status_code != 200:
            raise RuntimeError(f"GPU 服务器对口型失败 [{r.status_code}]：{r.text[:500]}")
        out_path.write_bytes(r.content)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("GPU 服务器对口型返回空视频")
    duration = media.probe(audio_path, "audio")["duration"]
    raw = out_path.with_name(out_path.stem + ".vendor.mp4")
    out_path.replace(raw)
    media.deliver_video_for_browser(raw, out_path, duration)
    try:
        media.check_video(out_path, duration)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    return str(out_path)


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
MAX_FACE_SECONDS = 8       # 视频底版最长取前 8 秒（无独显机器演示用，防超时）
FACE_MAX_WIDTH = 480       # 演示用：压到 480 宽，逐帧人脸检测才跑得快
FACE_FPS = 12              # 降到 12 帧：人脸检测帧数与生成帧数都减半 → 关键提速


def _to_wav16k(audio_path: Path, out_wav: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(out_wav)],
        capture_output=True, timeout=120,
    )


def _prep_face(face_path: str, work_dir: Path) -> str:
    """图片直接用；视频则截短+压小，避免长视频逐帧检测把机器跑爆。"""
    p = Path(face_path)
    if p.suffix.lower() not in VIDEO_EXTS:
        return face_path
    out = work_dir / "face_trim.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-t", str(MAX_FACE_SECONDS), "-i", str(p),
         "-vf", f"scale='min({FACE_MAX_WIDTH},iw)':-2", "-an",
         "-r", str(FACE_FPS), str(out)],
        capture_output=True, timeout=180,
    )
    return str(out) if out.exists() else face_path


def lipsync(face_path: str, audio_path: str, out_path: str, pads=(0, 10, 0, 0)) -> str:
    """对口型。face 可为图片或视频；audio 任意格式（内部转 16k wav）。"""
    c = _lscfg()
    if c.get("provider") == "dashscope":
        from services.segmented_lipsync import generate
        return generate(face_path, audio_path, out_path, c)
    if (c.get("provider") or "").lower() == "server":
        return _lipsync_server(face_path, audio_path, out_path, c)
    if not available():
        raise RuntimeError("Wav2Lip 引擎未就绪（缺权重）")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_out = out_path.with_name(out_path.stem + ".raw.mp4")
    wav = out_path.parent / "ls_input.wav"
    _to_wav16k(Path(audio_path), wav)
    face_use = _prep_face(face_path, out_path.parent)  # 视频底版先截短压小

    env = dict(os.environ)
    env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"  # 个别算子 MPS 不支持时回落 CPU
    cmd = [
        "python3", "inference.py",
        "--checkpoint_path", str(CKPT),
        "--face", str(face_use),
        "--audio", str(wav),
        "--outfile", str(raw_out),
        "--pads", *map(str, pads),
        "--nosmooth",
    ]
    # 视频底版逐帧很重：内部降采样加速（仅对视频，照片不受影响）
    if Path(face_use).suffix.lower() in VIDEO_EXTS:
        cmd += ["--resize_factor", "2"]
    # 超时放宽到 15 分钟（演示用视频帧多，给足余量）
    proc = subprocess.run(cmd, cwd=str(W2L_DIR), env=env,
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not raw_out.exists():
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise RuntimeError(f"对口型失败：{tail}")
    duration = media.probe(audio_path, "audio")["duration"]
    media.deliver_video_for_browser(raw_out, out_path, duration)
    try:
        media.check_video(out_path, duration)
    except Exception:
        out_path.unlink(missing_ok=True)
        raise
    return str(out_path)
