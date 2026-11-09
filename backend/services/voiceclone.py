"""声音克隆引擎（可插拔）。
  dashscope   : 用户试听选定的千问 3.0 Plus，百炼北京 API
  siliconflow : 原硅基流动 CosyVoice2 声音复刻 API，保留可选
  local       : 服务器自建 CosyVoice2 脚本（部署到GPU后用）
都没配 -> available()=False，克隆任务报错，不自动回退普通音色。

config.json 的 voice_clone 段示例：
  {"provider":"siliconflow","api_key":"sk-...","model":"FunAudioLLM/CosyVoice2-0.5B"}
"""
import base64
import hashlib
import json
import tempfile
import uuid
from pathlib import Path

import httpx

from config import load_config
from services import media, dashscope_voice
from services.health import check_server

SF_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"


def _cfg():
    cfg = load_config()
    voice = dict(cfg.get("voice_clone", {}) or {})
    if voice.get("provider") == "dashscope":
        lip = cfg.get("lipsync", {})
        voice["api_key"] = voice.get("dashscope_api_key") or (lip.get("api_key") if lip.get("provider") == "dashscope" else "")
    return voice


def available() -> bool:
    c = _cfg()
    p = (c.get("provider") or "none").lower()
    if p == "siliconflow":
        return bool(c.get("api_key"))
    if p == "dashscope":
        return bool(c.get("api_key")) and c.get("model") == dashscope_voice.MODEL
    if p == "server":
        return bool(c.get("base_url"))
    if p == "local":
        return bool(c.get("python") and c.get("script")
                    and Path(c["python"]).exists() and Path(c["script"]).exists())
    return False


def check_ready():
    c = _cfg()
    if not available():
        raise RuntimeError("声音克隆未配置，不会改用普通音色")
    if c.get("provider") == "server":
        check_server(c.get("base_url"), "声音克隆服务")


def clone(text: str, ref_audio: str, ref_text: str, out_path: str) -> str:
    c = _cfg()
    p = (c.get("provider") or "none").lower()
    if p == "siliconflow":
        result = _clone_siliconflow(text, ref_audio, ref_text, out_path, c)
    elif p == "dashscope":
        result = dashscope_voice.generate(text, ref_audio, out_path, c)
    elif p in {"server", "local"}:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=Path(out_path).parent) as tmp:
            normalized = Path(tmp) / "reference.wav"
            media.normalize_audio(ref_audio, normalized)
            method = _clone_server if p == "server" else _clone_local
            result = method(text, str(normalized), ref_text, out_path, c)
    else:
        raise RuntimeError("声音克隆引擎未配置")
    media.probe(result, "audio")
    return result


# ---------- 自建 GPU 服务器 CosyVoice3 HTTP API ----------
def _clone_server(text, ref_audio, ref_text, out_path, c) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    base = c["base_url"].rstrip("/")
    with httpx.Client(timeout=900) as cli:
        with open(ref_audio, "rb") as af:
            r = cli.post(
                f"{base}/voiceclone",
                data={"text": text, "ref_text": ref_text or ""},
                files={"ref_audio": (Path(ref_audio).name, af, "application/octet-stream")},
            )
        if r.status_code != 200:
            raise RuntimeError(f"GPU 服务器克隆失败 [{r.status_code}]：{r.text[:500]}")
        Path(out_path).write_bytes(r.content)
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        raise RuntimeError("GPU 服务器克隆返回空音频")
    return out_path


# ---------- 硅基流动 CosyVoice2 ----------
def _sf_voice_uri(ref_audio: str, ref_text: str, c: dict) -> str:
    """Cache the voice URI with its audio, model, account and reference fingerprint."""
    if not ref_text.strip():
        raise RuntimeError("声音克隆需要填写「这段录音里你说的那句话」（参考文本）")
    model = c.get("model", DEFAULT_MODEL)
    fingerprint = hashlib.sha256(
        Path(ref_audio).read_bytes() + model.encode() + c["api_key"].encode() + ref_text.strip().encode()
    ).hexdigest()
    sidecar = Path(ref_audio).with_suffix(".voice.json")
    if sidecar.exists():
        try:
            cache = json.loads(sidecar.read_text(encoding="utf-8"))
            if cache.get("fingerprint") == fingerprint and cache.get("uri"):
                return cache["uri"]
        except (ValueError, OSError):
            pass
    with tempfile.TemporaryDirectory() as tmp:
        normalized = Path(tmp) / "reference.wav"
        info = media.normalize_audio(ref_audio, normalized)
        if not 2 < info["duration"] < 30:
            raise RuntimeError("克隆参考音频须大于 2 秒且小于 30 秒")
        b64 = "data:audio/wav;base64," + base64.b64encode(normalized.read_bytes()).decode()
    payload = {
        "model": model,
        "customName": "voice_" + uuid.uuid4().hex[:12],
        "text": ref_text.strip(),
        "audio": b64,
    }
    with httpx.Client(timeout=120) as cli:
        r = cli.post(f"{SF_BASE}/uploads/audio/voice",
                     headers={"Authorization": f"Bearer {c['api_key']}"}, json=payload)
        r.raise_for_status()
        uri = r.json().get("uri")
    if not uri:
        raise RuntimeError("硅基流动未返回音色 uri")
    sidecar.write_text(json.dumps({"fingerprint": fingerprint, "uri": uri}), encoding="utf-8")
    return uri


def _clone_siliconflow(text, ref_audio, ref_text, out_path, c) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    uri = _sf_voice_uri(ref_audio, ref_text, c)
    model = c.get("model", DEFAULT_MODEL)
    fmt = "wav" if str(out_path).endswith(".wav") else "mp3"
    with httpx.Client(timeout=300) as cli:
        r = cli.post(f"{SF_BASE}/audio/speech",
                     headers={"Authorization": f"Bearer {c['api_key']}"},
                     json={"model": model, "voice": uri, "input": text,
                           "response_format": fmt})
        r.raise_for_status()
        Path(out_path).write_bytes(r.content)
    if not Path(out_path).exists() or Path(out_path).stat().st_size == 0:
        raise RuntimeError("硅基流动合成返回空音频")
    return out_path


# ---------- 本地自建 CosyVoice（部署到 GPU 服务器时用）----------
def _clone_local(text, ref_audio, ref_text, out_path, c) -> str:
    import subprocess
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [c["python"], c["script"], "--ref", ref_audio, "--ref-text", ref_text or "",
           "--text", text, "--out", out_path, "--model", c.get("model", "")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"本地声音克隆失败：{(proc.stderr or proc.stdout)[-500:]}")
    return out_path
