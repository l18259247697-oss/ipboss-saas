"""本地·免费对口型引擎（Wav2Lip + MPS 苹果GPU加速）。
   输入：一张正脸照片或一段人脸视频 + 配音 -> 输出：嘴型对上配音的 mp4。
   零 API、零授权费。生产环境可整体迁到 GPU 服务器加速。"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
W2L_DIR = ROOT / "engines" / "Wav2Lip"
CKPT = W2L_DIR / "checkpoints" / "wav2lip_gan.pth"


def available() -> bool:
    return CKPT.exists() and (W2L_DIR / "inference.py").exists()


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
MAX_FACE_SECONDS = 15      # 视频底版最长只取前 15 秒（防过热/卡死）
FACE_MAX_WIDTH = 1080      # 视频底版宽度上限。540→1080 后画面明显更清晰，
                           # MPS 上 8s 片仅多耗几秒；机器吃力可回调到 720。


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
         "-r", "25", str(out)],
        capture_output=True, timeout=180,
    )
    return str(out) if out.exists() else face_path


def lipsync(face_path: str, audio_path: str, out_path: str, pads=(0, 10, 0, 0)) -> str:
    """对口型。face 可为图片或视频；audio 任意格式（内部转 16k wav）。"""
    if not available():
        raise RuntimeError("Wav2Lip 引擎未就绪（缺权重）")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
        "--outfile", str(out_path),
        "--pads", *map(str, pads),
        "--nosmooth",
    ]
    # 超时收紧到 8 分钟：再卡也不会一直烤机器
    proc = subprocess.run(cmd, cwd=str(W2L_DIR), env=env,
                          capture_output=True, text=True, timeout=480)
    if proc.returncode != 0 or not out_path.exists():
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise RuntimeError(f"对口型失败：{tail}")
    return str(out_path)
