"""Audio-only Beijing ASR with a durable, non-repeating paid attempt."""
import base64
import fcntl
import hashlib
import json
import re
from pathlib import Path

import httpx

from services import media
from services.videoretalk import PendingTask, _write_state

MODEL = "qwen3-asr-flash-2026-02-10"
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
NATIVE_MODEL = "qwen-audio-3.0-asr-flash"
NATIVE_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"


def transcribe_audio(audio_path, state_path, cfg):
    key = cfg.get("api_key")
    if not key:
        raise RuntimeError("云端转写未配置百炼北京 API Key，未改用其他服务")
    model = cfg.get("model") or NATIVE_MODEL
    if model not in {MODEL, NATIVE_MODEL}:
        raise RuntimeError("转写模型未通过当前接口验证")
    native = model == NATIVE_MODEL
    audio_path, state_path = Path(audio_path), Path(state_path)
    duration = media.probe(audio_path, "audio")["duration"]
    if not 0 < duration <= 300:
        raise RuntimeError("当前云端转写支持不超过 5 分钟的音频")
    if ((audio_path.stat().st_size + 2) // 3) * 4 + 64 > 10_000_000:
        raise RuntimeError("音频 Base64 超过 10 MB，未发送转写请求")
    raw = audio_path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    fingerprint = hashlib.sha256((source_hash + model + key).encode()).hexdigest()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and state.get("fingerprint") != fingerprint:
            raise RuntimeError("转写记录与本次音频、模型或账号不一致，已停止复用")
        if state.get("status") == "done":
            return state
        if state.get("status") == "rejected":
            raise RuntimeError("转写服务已拒绝本次任务，未重复提交")
        if state.get("status") == "invalid":
            raise RuntimeError("未识别到完整口播文案，未继续生成配音")
        if state and state.get("status") != "not_sent":
            raise PendingTask("转写请求结果未知，已停止自动重试，请核对供应商记录")
        state = {"status": "submitting", "model": model, "fingerprint": fingerprint,
                 "audio_sha256": source_hash, "duration": duration}
        _write_state(state_path, state)
        messages = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
            "data": "data:audio/wav;base64," + base64.b64encode(raw).decode("ascii"),
        }}]}]
        payload = ({"model": model, "input": {"messages": messages},
                    "parameters": {"format": "wav", "language_hints": ["zh"]}} if native else
                   {"model": model, "messages": messages, "stream": False,
                    "asr_options": {"language": "zh", "enable_itn": False}})
        try:
            with httpx.Client(timeout=90) as client:
                response = client.post(NATIVE_ENDPOINT if native else ENDPOINT,
                                       headers={"Authorization": "Bearer " + key}, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            state.update(status="not_sent")
            _write_state(state_path, state)
            raise RuntimeError("转写服务未连接，HTTP 请求尚未发送") from exc
        except httpx.RequestError as exc:
            raise PendingTask("转写连接中断，结果未知，未自动重复提交") from exc
        if response.status_code >= 500:
            raise PendingTask("转写服务结果未知，未自动重复提交")
        if response.is_error:
            state.update(status="rejected", http_status=response.status_code)
            # Keep diagnostic IDs only; vendor error messages may echo request data.
            try:
                error = response.json()
                code = error.get("code") or (error.get("error") or {}).get("code")
                request_id = error.get("request_id") or error.get("id")
                for name, value in (("error_code", code), ("request_id", request_id)):
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                        state[name] = value
            except (ValueError, AttributeError, TypeError):
                pass
            _write_state(state_path, state)
            raise RuntimeError(f"转写服务拒绝任务（HTTP {response.status_code}）")
        try:
            result = response.json()
            if native:
                text = (result["output"]["text"] or "").strip()
                complete = result["output"].get("sentence", {}).get("sentence_end") is True
            else:
                choice = result["choices"][0]
                text = (choice["message"]["content"] or "").strip()
                complete = choice.get("finish_reason") == "stop"
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise PendingTask("转写返回格式异常，未自动重复提交") from exc
        state.update(request_id=result.get("request_id") if native else result.get("id"),
                     usage=result.get("usage", {}), response=result)
        if not text or not complete:
            state.update(status="invalid")
            _write_state(state_path, state)
            raise RuntimeError("未识别到完整口播文案，未继续生成配音")
        state.update(status="done", transcript=text)
        _write_state(state_path, state)
        return state
