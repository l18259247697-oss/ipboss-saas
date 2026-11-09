"""Selected Qwen voice adapter. Unknown paid submissions are never repeated."""
import fcntl
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from config import DATA_DIR
from services import media
from services.videoretalk import BASE, PendingTask, _upload, _write_state

MODEL = "qwen-audio-3.0-tts-plus"
INSTRUCTION = "像店主面对面聊天，自然亲切，语速适中，句间短暂停顿，不要播音腔。"
STATE_DIR = DATA_DIR / "voice-clone"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _json(response):
    if response.is_error:
        raise RuntimeError("千问声音接口返回 HTTP %s，请核对权限与余额" % response.status_code)
    data = response.json()
    if data.get("code"):
        raise RuntimeError("千问声音接口未成功，错误码：" + str(data["code"]))
    return data


def _enroll(client, headers, reference, cache):
    fingerprint = hashlib.sha256(reference.read_bytes() + MODEL.encode() + headers["Authorization"].encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (fingerprint + ".json")
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read(path)
        prefix = "vc" + fingerprint[:8]
        endpoint = BASE + "/services/audio/tts/customization"
        if state.get("status") == "submitting":
            result = _json(client.post(endpoint, headers=headers, json={"model": "voice-enrollment",
                           "input": {"action": "list_voice", "prefix": prefix, "page_index": 0, "page_size": 100}}))
            voices = result.get("output", {}).get("voice_list", [])
            matches = [v for v in voices if v.get("voice_id", "").startswith(MODEL + "-" + prefix + "-")]
            if len(matches) != 1:
                raise PendingTask("音色创建结果待核对，未重新创建；请稍后继续查询")
            state.update(status="created", voice_id=matches[0]["voice_id"])
            _write_state(path, state)
        if state.get("status") == "rejected":
            raise RuntimeError("该音色创建已被拒绝，未自动重复提交")
        if not state.get("voice_id"):
            url = _upload(client, reference, headers, model="voice-enrollment")
            state = {"fingerprint": fingerprint, "model": MODEL, "status": "submitting", "prefix": prefix}
            _write_state(path, state)
            response = client.post(endpoint, headers={**headers, "X-DashScope-OssResourceResolve": "enable"},
                json={"model": "voice-enrollment", "input": {"action": "create_voice", "target_model": MODEL,
                      "prefix": prefix, "url": url, "language_hints": ["zh"], "enable_preprocess": False}})
            if 400 <= response.status_code < 500:
                state.update(status="rejected", http_status=response.status_code)
                _write_state(path, state)
            if response.status_code >= 500:
                raise PendingTask("音色创建结果未知，未自动重试；请继续查询原音色")
            result = _json(response)
            voice = result.get("output", {}).get("voice_id")
            if not voice:
                raise PendingTask("音色创建未返回编号，未自动重新创建")
            state.update(status="created", voice_id=voice)
            _write_state(path, state)
        result = _json(client.post(endpoint, headers=headers, json={"model": "voice-enrollment",
                       "input": {"action": "query_voice", "voice_id": state["voice_id"]}}))
        detail = result.get("output", {})
        if detail.get("target_model") != MODEL:
            raise RuntimeError("音色与千问 3.0 Plus 模型不匹配，已停止合成")
        if detail.get("status") == "DEPLOYING":
            raise PendingTask("本人音色正在准备，稍后继续查询即可")
        if detail.get("status") != "OK":
            raise RuntimeError("千问音色尚不可用：" + str(detail.get("status")))
        state["status"] = "ready"
        _write_state(path, state)
        return state["voice_id"]


def _synthesize(client, headers, voice, text, output, path, fingerprint):
    state = _read(path)
    if state and state.get("fingerprint") != fingerprint:
        raise PendingTask("配音输入、账号或模型已改变，不能自动复用或重做原任务")
    if state.get("status") == "complete":
        if not output.exists() or hashlib.sha256(output.read_bytes()).hexdigest() != state.get("sha256"):
            raise PendingTask("已生成的配音文件丢失或改变，未重新付费生成")
        return str(output)
    if state.get("status") == "submitting":
        raise PendingTask("配音提交结果未知，需核对供应商记录，未自动重复扣费生成")
    if state.get("status") == "rejected":
        raise RuntimeError("配音请求已被拒绝，未自动重试")
    if not state:
        state = {"status": "submitting", "fingerprint": fingerprint, "model": MODEL,
                 "started_at": time.time()}
        _write_state(path, state)
        response = client.post(BASE + "/services/audio/tts/SpeechSynthesizer", headers=headers,
            json={"model": MODEL, "input": {"text": text, "voice": voice, "format": "wav", "sample_rate": 24000,
                  "rate": 1.0, "instruction": INSTRUCTION, "seed": 928}})
        if 400 <= response.status_code < 500:
            state.update(status="rejected", http_status=response.status_code)
            _write_state(path, state)
        if response.status_code >= 500:
            raise PendingTask("配音提交结果未知，未自动重新生成")
        result = _json(response)
        state.update(status="generated", response=result, generation_seconds=round(time.time() - state["started_at"], 2))
        _write_state(path, state)
    result = state["response"]
    url = result.get("output", {}).get("audio", {}).get("url", "")
    parts = urlsplit(url)
    if result.get("output", {}).get("finish_reason") != "stop" or parts.scheme not in {"http", "https"} or not (parts.hostname or "").endswith(".aliyuncs.com"):
        raise PendingTask("配音返回结果不完整，已保留供应商记录，未重新生成")
    # DashScope can return an HTTP OSS URL; retrieve that same object over TLS.
    url = parts._replace(scheme="https").geturl()
    original = path.with_suffix(".vendor.wav")
    if original.exists():
        if hashlib.sha256(original.read_bytes()).hexdigest() != state.get("vendor_sha256"):
            raise PendingTask("供应商配音原文件已改变，需核对后继续")
    else:
        pending = path.with_suffix(".download.wav")
        with client.stream("GET", url) as response:
            if response.is_error:
                raise PendingTask("配音已生成但下载暂时失败，继续时将下载同一结果")
            with pending.open("wb") as dest:
                for chunk in response.iter_bytes():
                    dest.write(chunk)
        pending.replace(original)
        state["vendor_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        _write_state(path, state)
    pending = output.with_suffix(".pending.wav")
    pending.write_bytes(original.read_bytes())
    repaired = media.finalize_wav(pending, original)
    media.verify_audio(pending)
    info = media.probe(pending, "audio")
    if info["duration"] <= 2:
        raise RuntimeError("配音须大于 2 秒，未提交口型任务")
    pending.replace(output)
    state.update(status="complete", duration=info["duration"], header_finalized=repaired, full_decode="passed",
                 sha256=hashlib.sha256(output.read_bytes()).hexdigest(), usage=result.get("usage", {}),
                 request_id=result.get("request_id"))
    _write_state(path, state)
    return str(output)


def generate(text, reference, output, cfg):
    if cfg.get("model") != MODEL or not cfg.get("api_key"):
        raise RuntimeError("请配置千问 3.0 Plus 和百炼北京地域 Key")
    if not text.strip() or len(text) > 2000:
        raise RuntimeError("配音稿须为 1–2000 字符")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    job_key = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
    private_dir = STATE_DIR / "jobs" / job_key
    private_dir.mkdir(parents=True, exist_ok=True)
    with (private_dir / "job.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        normalized = private_dir / "reference.wav"
        media.normalize_audio(reference, normalized)
        if not 2 < media.probe(normalized, "audio")["duration"] < 30:
            raise RuntimeError("克隆录音须为 2–30 秒")
        headers = {"Authorization": "Bearer " + cfg["api_key"]}
        fingerprint = hashlib.sha256(normalized.read_bytes() + MODEL.encode() + text.encode() +
                                     INSTRUCTION.encode() + headers["Authorization"].encode()).hexdigest()
        path = private_dir / "synthesis.json"
        state = _read(path)
        if state and state.get("fingerprint") != fingerprint:
            raise PendingTask("配音输入或账号已改变，不能自动重做原任务")
        try:
            with httpx.Client(timeout=180) as client:
                voice = ""
                if not state:
                    voice = _enroll(client, headers, normalized, STATE_DIR / "voices")
                return _synthesize(client, headers, voice, text, output, path, fingerprint)
        except (httpx.RequestError, ValueError) as exc:
            raise PendingTask("千问请求或下载中断，已保留进度；继续时不会重做已提交的配音") from exc
