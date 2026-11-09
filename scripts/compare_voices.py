"""Explicit, resumable model audition. Does not change the app configuration."""
import argparse
import fcntl
import hashlib
import html
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from config import load_config
from services import media

BASE = "https://dashscope.aliyuncs.com/api/v1"
BASELINE_MODEL = "qwen-audio-3.0-tts-plus"
FLASH31_MODEL = "qwen-audio-3.1-tts-flash"
ORIGINAL_MODEL = "siliconflow-cosyvoice2"
SF_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
SF_BASE = "https://api.siliconflow.cn/v1"
MODELS = {BASELINE_MODEL: ("Qwen-Audio 3.0 TTS Plus", 1.4),
          ORIGINAL_MODEL: ("原方案 · 硅基流动 CosyVoice 2", None),
          FLASH31_MODEL: ("Qwen-Audio 3.1 TTS Flash", None),
          "cosyvoice-v3.5-plus": ("CosyVoice 3.5 Plus", 1.5)}
OUT = ROOT / "outputs/e2e-20260928/voice-comparison-20260928"
STATE = ROOT / "data/e2e-20260928/voice-comparison-20260928"
REFERENCE = ROOT / "data/voices/u2.m4a"
SF_REFERENCE = ROOT / "outputs/e2e-20260928/92e3faf0048c/inputs/reference.wav"
SF_REFERENCE_TEXT = ROOT / "data/e2e-20260928/voices/u1.txt"
TEXT = (
    "大家好，我是手作汉堡黑哥，在同安开了一家汉堡店。今天想跟大家聊聊，做生意到底什么最重要？"
    "在我看来，不是把话说得多漂亮，而是让顾客每一次来，都觉得这顿饭吃得踏实。"
    "比如，同样的价格，就应该给同样的分量。不能看人家饭量小，就自作主张少放一点。"
    "如果顾客主动说，今天不太饿，帮我少做一点，那当然可以。"
    "你看，这些事情听起来很小，但顾客心里都有数。把每一个细节认真做好，比喊一百句口号都有用。"
    "你们觉得呢？欢迎来聊聊，你吃汉堡最在意的是什么？"
)
INSTRUCTION = "像店主面对面聊天，自然亲切，语速适中，句间短暂停顿，不要播音腔。"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cost_plan(model, text):
    if not text or len(text) > 300:
        raise RuntimeError("Audition is restricted to one non-empty script of at most 300 characters")
    if model == ORIGINAL_MODEL:
        byte_count = len(text.encode("utf-8"))
        return {"billing_unit": "utf8_bytes", "input_utf8_bytes": byte_count,
                "cny_per_thousand_bytes": 0.05, "planned_cny": round(byte_count * 0.05 / 1000, 6),
                "provider_enforced_cap": False, "max_new_synthesis_requests": 1}
    if model == FLASH31_MODEL:
        # Token allowances are planning assumptions, not a provider-enforced cap.
        return {"billing_unit": "tokens", "input_cny_per_million": 1.5,
                "output_cny_per_million": 12, "assumed_input_tokens": 2000,
                "assumed_output_tokens": 30000, "planned_cny": 0.363,
                "provider_enforced_cap": False, "max_new_synthesis_requests": 1}
    chars = sum(2 if "\u4e00" <= c <= "\u9fff" else 1 for c in text)
    price = MODELS[model][1]
    return {"billing_unit": "characters", "estimated_billing_characters": chars,
            "planned_cny": round(price * chars / 10000, 6),
            "provider_enforced_cap": False, "max_new_synthesis_requests": 1}


def usage_cost(model, usage):
    if model == ORIGINAL_MODEL:
        # The binary speech response does not contain usage or an account bill.
        return None
    if model == FLASH31_MODEL:
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
        if not isinstance(inputs, int) or not isinstance(outputs, int) or min(inputs, outputs) < 0:
            return None
        return round((inputs * 1.5 + outputs * 12) / 1000000, 8)
    characters = usage.get("characters")
    if not isinstance(characters, int) or characters < 0:
        return None
    return round(characters * MODELS[model][1] / 10000, 6)


def checked_json(response):
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Vendor returned a non-JSON response") from None
    if response.is_error or data.get("code"):
        # Vendor request bodies may contain audio URLs; never print the full response.
        raise RuntimeError("Vendor rejected request: HTTP %s, code=%s, request_id=%s" %
                           (response.status_code, data.get("code"), data.get("request_id")))
    return data


finalize_wav = media.finalize_wav
verify_audio = media.verify_audio


def verify_existing(models=None):
    import whisper
    import torch
    import re
    from difflib import SequenceMatcher

    torch.set_num_threads(2)
    asr = whisper.load_model("small", device="cpu")
    normalize = lambda value: re.sub(r"[^\u4e00-\u9fffa-z0-9]", "", value.lower())
    expected = normalize(TEXT)
    for model in models or MODELS:
        path = STATE / (model + ".synthesis.json")
        state = read(path)
        output = OUT / (model + ".wav")
        if state.get("status") != "complete" or not output.exists():
            continue
        if hashlib.sha256(output.read_bytes()).hexdigest() != state.get("sha256"):
            raise RuntimeError("Output changed since generation; inspect before verification")
        original = STATE / (model + ".vendor.wav")
        if not original.exists():
            original = path.with_suffix(".vendor.wav")
        changed = finalize_wav(output, original)
        verify_audio(output)
        state.update(header_finalized=changed or state.get("header_finalized", False), full_decode="passed",
                     sha256=hashlib.sha256(output.read_bytes()).hexdigest())
        if original.exists():
            state["vendor_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
        save(path, state)
        audit_path = STATE / (model + ".asr.json")
        audit = read(audit_path)
        if audit.get("audio_sha256") != state["sha256"]:
            result = asr.transcribe(str(output), language="zh", task="transcribe", fp16=False,
                                    condition_on_previous_text=False, temperature=0)
            transcript = result["text"]
            audit = {"audio_sha256": state["sha256"], "model": "whisper-small", "text": transcript,
                     "segments": result["segments"], "normalized_match_ratio": SequenceMatcher(None, expected, normalize(transcript)).ratio(),
                     "note": "ASR similarity is not a speaker-similarity or naturalness score."}
            save(audit_path, audit)
        print(json.dumps({"model": model, "full_decode": "passed", "text": audit["text"],
                          "normalized_match_ratio": audit["normalized_match_ratio"]}, ensure_ascii=False), flush=True)
    render()


def upload(client, audio, headers):
    policy = checked_json(client.get(BASE + "/uploads", headers=headers,
                                    params={"action": "getPolicy", "model": "voice-enrollment"}))["data"]
    host = urlsplit(policy["upload_host"])
    if host.scheme != "https" or not (host.hostname or "").endswith(".aliyuncs.com"):
        raise RuntimeError("Unexpected upload destination")
    key = policy["upload_dir"].rstrip("/") + "/" + uuid.uuid4().hex + ".wav"
    form = {"OSSAccessKeyId": policy["oss_access_key_id"], "policy": policy["policy"],
            "Signature": policy["signature"], "key": key,
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"], "success_action_status": "200"}
    with audio.open("rb") as source:
        result = client.post(policy["upload_host"], data=form,
                             files={"file": (audio.name, source, "audio/wav")})
    if result.is_error:
        raise RuntimeError("Audio upload failed: HTTP %s" % result.status_code)
    return "oss://" + key


def enroll(client, headers, model, audio, path):
    fingerprint = hashlib.sha256(audio.read_bytes() + model.encode() + headers["Authorization"].encode()).hexdigest()
    state = read(path)
    if state and state.get("fingerprint") != fingerprint:
        raise RuntimeError("Reference, account or model changed; use a separate comparison")
    prefix = "ab" + fingerprint[:8]
    if state.get("status") == "submitting":
        result = checked_json(client.post(BASE + "/services/audio/tts/customization", headers=headers,
            json={"model": "voice-enrollment", "input": {"action": "list_voice", "prefix": prefix,
                                                         "page_index": 0, "page_size": 100}}))
        voices = result.get("output", {}).get("voice_list", [])
        matches = [v for v in voices if v.get("voice_id", "").startswith(model + "-" + prefix + "-")]
        if len(matches) != 1:
            raise RuntimeError("Enrollment outcome unknown; no automatic resubmission")
        state.update(voice_id=matches[0]["voice_id"], status="created")
        save(path, state)
    if state.get("status") == "rejected":
        raise RuntimeError("Enrollment was rejected; inspect saved evidence before a new attempt")
    if not state.get("voice_id"):
        url = upload(client, audio, headers)
        state = {"fingerprint": fingerprint, "model": model, "prefix": prefix, "status": "submitting"}
        save(path, state)
        response = client.post(BASE + "/services/audio/tts/customization",
            headers={**headers, "X-DashScope-OssResourceResolve": "enable"},
            json={"model": "voice-enrollment", "input": {"action": "create_voice", "target_model": model,
                  "prefix": prefix, "url": url, "language_hints": ["zh"], "enable_preprocess": False}})
        if 400 <= response.status_code < 500:
            state.update(status="rejected", http_status=response.status_code)
            try:
                detail = response.json()
                state.update(code=detail.get("code"), message=detail.get("message"), request_id=detail.get("request_id"))
            except ValueError:
                pass
            save(path, state)
        result = checked_json(response)
        voice = result.get("output", {}).get("voice_id")
        if not voice:
            raise RuntimeError("Enrollment outcome unknown: no voice ID")
        state.update(voice_id=voice, status="created", request_id=result.get("request_id"))
        save(path, state)
    for _ in range(30):
        result = checked_json(client.post(BASE + "/services/audio/tts/customization", headers=headers,
            json={"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": state["voice_id"]}}))
        output = result.get("output", {})
        if output.get("target_model") != model:
            raise RuntimeError("Enrolled voice does not match requested model")
        if output.get("status") == "OK":
            state["status"] = "ready"
            save(path, state)
            return state["voice_id"]
        if output.get("status") != "DEPLOYING":
            raise RuntimeError("Voice is not usable: " + str(output.get("status")))
        time.sleep(2)
    raise RuntimeError("Voice still deploying; resume this same comparison later")


def synthesize(client, headers, model, voice, text, output, path):
    payload = {"model": model, "input": {"text": text, "voice": voice, "format": "wav",
               "sample_rate": 24000, "rate": 1.0, "instruction": INSTRUCTION, "seed": 928}}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + headers["Authorization"].encode()).hexdigest()
    state = read(path)
    if state and state.get("fingerprint") != fingerprint:
        raise RuntimeError("Synthesis inputs changed; use a separate comparison")
    if state.get("status") == "complete" and output.exists():
        if hashlib.sha256(output.read_bytes()).hexdigest() != state.get("sha256"):
            raise RuntimeError("Output changed since verification")
        return state
    if state.get("status") in {"submitting", "rejected"}:
        raise RuntimeError("Synthesis outcome unresolved or rejected; no automatic paid retry")
    if not state.get("response"):
        plan = cost_plan(model, text)
        if plan["planned_cny"] > 0.5:
            raise RuntimeError("Planned call cost exceeds the audition allowance")
        state = {"fingerprint": fingerprint, "model": model, "status": "submitting", "started_at": time.time(),
                 "cost_plan": plan}
        save(path, state)
        response = client.post(BASE + "/services/audio/tts/SpeechSynthesizer", headers=headers, json=payload)
        if 400 <= response.status_code < 500:
            state.update(status="rejected", http_status=response.status_code)
            try:
                detail = response.json()
                state.update(code=detail.get("code"), message=detail.get("message"), request_id=detail.get("request_id"))
            except ValueError:
                pass
            save(path, state)
        result = checked_json(response)
        state.update(response=result, status="generated", generation_seconds=round(time.time() - state["started_at"], 2))
        save(path, state)
    result = state["response"]
    audio = result.get("output", {}).get("audio", {})
    url = audio.get("url", "")
    parts = urlsplit(url)
    if result.get("output", {}).get("finish_reason") != "stop" or parts.scheme not in {"http", "https"}:
        raise RuntimeError("Synthesis did not return a complete audio result")
    if not (parts.hostname or "").endswith(".aliyuncs.com"):
        raise RuntimeError("Unexpected generated-audio host")
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".download.wav")
    with client.stream("GET", url) as response:
        if response.is_error:
            raise RuntimeError("Download failed; reuse saved result instead of regenerating")
        with pending.open("wb") as target:
            for chunk in response.iter_bytes():
                target.write(chunk)
    original = path.with_suffix(".vendor.wav")
    repaired = finalize_wav(pending, original)
    verify_audio(pending)
    info = media.probe(pending, "audio")
    if info["duration"] < 20:
        raise RuntimeError("Long-script output is unexpectedly short; not publishing")
    pending.replace(output)
    state.update(status="complete", duration=info["duration"], sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                 header_finalized=repaired, full_decode="passed",
                 usage=result.get("usage", {}), request_id=result.get("request_id"),
                 list_price_cny_from_usage=usage_cost(model, result.get("usage", {})))
    if original.exists():
        state["vendor_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    save(path, state)
    return state


def original_voice(config, reference, ref_text, comparison_reference):
    if config.get("provider") != "siliconflow" or config.get("model") != SF_MODEL or not config.get("api_key"):
        raise RuntimeError("Original provider configuration changed; not substituting another model")
    if not ref_text.strip() or reference.read_bytes() != comparison_reference.read_bytes():
        raise RuntimeError("Original reference does not match the comparison")
    fingerprint = hashlib.sha256(reference.read_bytes() + SF_MODEL.encode() +
                                 config["api_key"].encode() + ref_text.strip().encode()).hexdigest()
    cache = read(reference.with_suffix(".voice.json"))
    if cache.get("fingerprint") != fingerprint or not cache.get("uri", "").startswith("speech:"):
        raise RuntimeError("Original voice cache does not match; no automatic enrollment")
    return cache["uri"]


def synthesize_original(client, headers, voice, text, output, path):
    # Match the production adapter, including its lack of a style instruction.
    payload = {"model": SF_MODEL, "voice": voice, "input": text, "response_format": "wav"}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + headers["Authorization"].encode()).hexdigest()
    state = read(path)
    if state and state.get("fingerprint") != fingerprint:
        raise RuntimeError("Synthesis inputs changed; use a separate comparison")
    if state.get("status") == "complete" and output.exists():
        if hashlib.sha256(output.read_bytes()).hexdigest() != state.get("sha256"):
            raise RuntimeError("Output changed since verification")
        return state
    if state.get("status") in {"submitting", "rejected"}:
        raise RuntimeError("Synthesis outcome unresolved or rejected; no automatic paid retry")
    original = path.with_suffix(".vendor.wav")
    if not state:
        if original.exists() or output.exists():
            raise RuntimeError("Untracked audio exists; inspect before submitting")
        plan = cost_plan(ORIGINAL_MODEL, text)
        if plan["planned_cny"] > 0.5:
            raise RuntimeError("Planned call cost exceeds the audition allowance")
        state = {"fingerprint": fingerprint, "model": ORIGINAL_MODEL, "vendor_model": SF_MODEL,
                 "status": "submitting", "started_at": time.time(), "cost_plan": plan,
                 "client_trace_id": uuid.uuid4().hex, "instruction": None, "reference_reused": True}
        save(path, state)
        response = client.post(SF_BASE + "/audio/speech", json=payload,
                               headers={**headers, "X-Trace-Id": state["client_trace_id"]})
        if response.is_error:
            state.update(status="rejected" if response.status_code < 500 else "submitting",
                         http_status=response.status_code)
            save(path, state)
            raise RuntimeError("Original provider returned HTTP %s; no automatic paid retry" % response.status_code)
        pending = original.with_suffix(".pending.wav")
        pending.write_bytes(response.content)
        pending.replace(original)
        state.update(status="generated", vendor_sha256=hashlib.sha256(response.content).hexdigest(),
                     request_id=response.headers.get("x-siliconcloud-trace-id"),
                     generation_seconds=round(time.time() - state["started_at"], 2))
        save(path, state)
    if not original.exists() or hashlib.sha256(original.read_bytes()).hexdigest() != state.get("vendor_sha256"):
        raise RuntimeError("Saved vendor audio missing or changed; no automatic paid retry")
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".download.wav")
    pending.write_bytes(original.read_bytes())
    repaired = finalize_wav(pending, original)
    verify_audio(pending)
    info = media.probe(pending, "audio")
    if info["duration"] < 20:
        raise RuntimeError("Long-script output is unexpectedly short; not publishing")
    pending.replace(output)
    state.update(status="complete", duration=info["duration"], sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                 header_finalized=repaired, full_decode="passed", usage={},
                 list_price_cny_from_input=state["cost_plan"]["planned_cny"])
    save(path, state)
    return state


def render():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    public = []
    for model, (name, price) in MODELS.items():
        state = read(STATE / (model + ".synthesis.json"))
        review = read(STATE / (model + ".review.json"))
        review_note = review.get("note", "") if review.get("audio_sha256") == state.get("sha256") else ""
        ready = state.get("status") == "complete" and (OUT / (model + ".wav")).exists()
        public.append({"model": model, "status": state.get("status", "not_run"),
                       "duration": state.get("duration"), "usage": state.get("usage", {}),
                       "sha256": state.get("sha256"), "generation_seconds": state.get("generation_seconds"),
                       "preferred_by_user": model == BASELINE_MODEL,
                       "review_note": review_note,
                       "instruction": None if model == ORIGINAL_MODEL else INSTRUCTION,
                       "list_price_cny_from_input": state.get("list_price_cny_from_input"),
                       "list_price_cny_from_usage": usage_cost(model, state.get("usage", {}))})
        status = {"submitting": "生成结果待核对", "generated": "音频校验中", "rejected": "服务拒绝，未重试"}.get(state.get("status"), "尚未生成")
        player = ('<audio controls preload="metadata" src="%s.wav"></audio><a download href="%s.wav">下载音频</a>' % (model, model)) if ready else '<p class="status">%s</p>' % status
        duration = ("%.1f 秒" % state["duration"]) if ready else "待验证"
        if model == BASELINE_MODEL:
            duration += " · 当前优选"
        cost = state.get("list_price_cny_from_input") if model == ORIGINAL_MODEL else usage_cost(model, state.get("usage", {}))
        price_note = '<p class="status">本稿合成标价估算：%.5f 元，非实扣账单</p>' % cost if ready and cost is not None else ""
        quality_note = '<p class="review">%s</p>' % html.escape(review_note) if ready and review_note else ""
        rows.append('<section><h2>%s <small>%s</small></h2>%s%s%s</section>' % (name, duration, player, price_note, quality_note))
    save(OUT / "results.json", {"text": TEXT, "instruction": INSTRUCTION, "models": public})
    content = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>声音试听对照</title><style>
*{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:#17202c;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}
main{max-width:860px;margin:auto;padding:28px 22px}header{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap;border-bottom:2px solid #157d78;padding-bottom:16px}
h1{font-size:26px;margin:0}h2{font-size:18px;margin:0 0 12px}small{font-size:14px;font-weight:400;color:#67717b;margin-left:10px}
section{padding:22px 0;border-bottom:1px solid #d9dee4}audio{display:block;width:100%%;height:54px;margin:12px 0}a{color:#126a76;text-underline-offset:4px}p{margin:0}.script{white-space:pre-wrap;line-height:1.95}.status{color:#67717b}.review{color:#9b3434;margin-top:8px}
@media(max-width:480px){main{padding:20px 16px}h1{font-size:23px}h2 small{display:block;margin:4px 0 0}}
</style><main><header><h1>声音试听对照</h1><a href="/">返回工作台</a></header>
<section><h2>本人原录音 <small>9.1 秒</small></h2><audio controls preload="metadata" src="reference.wav"></audio></section>
%s<section><h2>对照条件</h2><p>同一段本人录音、同一篇稿。原方案沿用已有音色及原参数，未加语气指令；其余版本使用自然聊天语气指令。本页仅比较声音，不含改口型和视频费用。</p></section>
<section><h2>试听稿 <small>AI 合成音频，待本人试听确认</small></h2><p class="script">%s</p></section></main>
<script>document.querySelectorAll('audio').forEach(a=>a.addEventListener('play',()=>document.querySelectorAll('audio').forEach(b=>{if(a!==b)b.pause()})))</script></html>""" % ("".join(rows), html.escape(TEXT))
    (OUT / "index.html").write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=list(MODELS))
    parser.add_argument("--execute", action="store_true", help="Authorize one model call using the already approved test budget")
    parser.add_argument("--verify", action="store_true", help="Local full decode and Whisper check, no API calls")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    with (STATE / "comparison.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (OUT / "reference.wav").exists():
            media.normalize_audio(REFERENCE, OUT / "reference.wav")
        render()
        if args.verify:
            if args.execute:
                parser.error("--verify cannot be combined with --execute")
            verify_existing([args.model] if args.model else None)
            return
        plans = {model: cost_plan(model, TEXT) for model in ([args.model] if args.model else MODELS)}
        if any(plan["planned_cny"] > 0.5 for plan in plans.values()):
            raise RuntimeError("Planned call cost exceeds the audition allowance")
        print(json.dumps({"script_characters": len(TEXT), "cost_plans": plans,
                          "execute": args.execute}, ensure_ascii=False), flush=True)
        if not args.execute:
            return
        if not args.model:
            parser.error("--execute requires --model; one representative call at a time")
        config = load_config()
        original = args.model == ORIGINAL_MODEL
        key = config.get("voice_clone" if original else "lipsync", {}).get("api_key")
        if not key:
            raise RuntimeError("Missing existing provider key")
        headers = {"Authorization": "Bearer " + key}
        try:
            with httpx.Client(timeout=300 if original else 180) as client:
                if original:
                    voice = original_voice(config["voice_clone"], SF_REFERENCE,
                                           SF_REFERENCE_TEXT.read_text(encoding="utf-8"), OUT / "reference.wav")
                else:
                    voice = enroll(client, headers, args.model, OUT / "reference.wav", STATE / (args.model + ".voice.json"))
                print(json.dumps({"model": args.model, "voice_status": "ready"}), flush=True)
                if original:
                    state = synthesize_original(client, headers, voice, TEXT, OUT / (args.model + ".wav"),
                                                STATE / (args.model + ".synthesis.json"))
                else:
                    state = synthesize(client, headers, args.model, voice, TEXT, OUT / (args.model + ".wav"),
                                       STATE / (args.model + ".synthesis.json"))
                print(json.dumps({k: state.get(k) for k in ("model", "status", "duration", "usage", "generation_seconds")}), flush=True)
        finally:
            render()


if __name__ == "__main__":
    main()
