"""声音克隆引擎（可插拔）。
推荐引擎：CosyVoice2（阿里开源，中文零样本克隆最佳，跑在 GPU 服务器上）。
本机没装引擎时 available() 返回 False，配音自动回退到免费 edge-tts。

部署到 GPU 服务器时：
  1) git clone CosyVoice + 下载 CosyVoice2-0.5B 权重
  2) 建独立 venv 装依赖
  3) 在 data/config.json 的 voice_clone 段填 enabled/python/script/model
之后用户上传一段自己声音(10-30秒)，本模块即用其声线合成任意文案。"""
import json
import subprocess
from pathlib import Path

from config import load_config


def _cfg():
    return load_config().get("voice_clone", {}) or {}


def available() -> bool:
    c = _cfg()
    if not c.get("enabled"):
        return False
    py = c.get("python")
    script = c.get("script")
    return bool(py and script and Path(py).exists() and Path(script).exists())


def clone(text: str, ref_audio: str, ref_text: str, out_wav: str) -> str:
    """用 ref_audio 的声线把 text 合成到 out_wav。需引擎就绪，否则抛异常。
    约定：调用一个独立脚本 infer_cosyvoice.py，参数 --ref --ref-text --text --out。"""
    c = _cfg()
    if not available():
        raise RuntimeError("声音克隆引擎未就绪")
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        c["python"], c["script"],
        "--ref", ref_audio,
        "--ref-text", ref_text or "",
        "--text", text,
        "--out", out_wav,
        "--model", c.get("model", ""),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0 or not Path(out_wav).exists():
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"声音克隆失败：{tail}")
    return out_wav
