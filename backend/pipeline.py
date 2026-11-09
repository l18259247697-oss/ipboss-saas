"""6 步全自动流水线编排（后台线程跑），实时更新任务进度供前端轮询。"""
import traceback
from pathlib import Path

import db
from config import (OUTPUTS_DIR, find_user_face, find_user_voice, load_config)
from services import (compose, digital_human, extract, lipsync, rewrite, tts,
                      voiceclone, media)
from services.videoretalk import PendingTask, validate_face

# 6 步对应进度（与前端步骤条一致）
STEPS = ["提取文案", "改写文案", "生成配音", "检查底版", "换口型", "校验成片"]


def check_inputs(params, user_id):
    if params.get("mode", "auto") != "auto":
        raise RuntimeError("当前仅支持全自动生成")
    if not (params.get("link") or params.get("source_text") or "").strip():
        raise RuntimeError("请填写视频链接或原始文案")
    face = params.get("_face_path") or find_user_face(user_id)
    if not face or not Path(face).is_file():
        raise RuntimeError("请先上传你的人脸视频底版，不会用占位片替代")
    voice_sample = params.get("_voice_path") or find_user_voice(user_id)
    if params.get("use_clone"):
        if not voice_sample or not Path(voice_sample).is_file():
            raise RuntimeError("请先上传你的声音样本")
        if not voiceclone.available():
            raise RuntimeError("声音克隆服务未配置，不会改用普通音色")
        ref_file = Path(voice_sample).with_suffix(".txt")
        reference = (params.get("voice_ref_text") or "").strip() or (
            ref_file.read_text(encoding="utf-8").strip() if ref_file.exists() else "")
        if not reference:
            raise RuntimeError("请填写声音样本对应的原文")
    if not lipsync.available():
        raise RuntimeError("口型服务未配置，不会生成占位视频")
    if load_config().get("lipsync", {}).get("provider") == "dashscope":
        validate_face(face)
    return face, voice_sample


def _set(job_id, step, progress, message, **result_patch):
    fields = {"step": step, "progress": progress, "message": message, "status": "running"}
    if result_patch:
        job = db.get_job(job_id)
        res = job["result"]
        res.update(result_patch)
        fields["result"] = res
    db.update_job(job_id, **fields)


def run_pipeline(job_id: str):
    cfg = load_config()
    job = db.get_job(job_id)
    if not job or job["status"] in {"done", "failed"}:
        return
    if not db.claim_job(job_id):
        return
    p = job["params"]
    out_root = OUTPUTS_DIR / job_id
    try:
        face, voice_sample = check_inputs(p, job["user_id"])
        out_root.mkdir(parents=True, exist_ok=True)
        previous = job["result"] if job["status"] == "waiting" else {}
        if job["status"] == "waiting" and not all(previous.get(k) for k in ("transcript", "rewritten")):
            raise PendingTask("任务缺少可恢复的文案记录，已停止自动重做")
        # ① 文案生成（扒链接）
        _set(job_id, 1, 5, "正在自动化提取爆款文案…")
        link = extract.clean_link(p.get("link", ""))
        if previous:
            ex = {"transcript": previous["transcript"], "title": previous.get("source_title", "")}
        elif (p.get("source_text") or "").strip():
            ex = {"transcript": p["source_text"].strip(), "title": "输入的原始文案"}
        else:
            ex = extract.extract_from_link(link, job_id)
        _set(job_id, 1, 25, "已提取原视频口播文案",
             transcript=ex["transcript"], source_title=ex["title"], cover=ex.get("cover"))
        if ex.get("transcript_model"):
            _set(job_id, 1, 25, "已提取原视频口播文案",
                 transcript_model=ex["transcript_model"], transcript_request_id=ex["transcript_request_id"])

        # ② 改写文案
        _set(job_id, 2, 30, "AI 正在改写成你行业的文案…")
        new_copy = previous.get("rewritten") or rewrite.rewrite_copy(
            ex["transcript"], p.get("industry", ""), p.get("extra", ""), cfg)
        _set(job_id, 2, 50, "文案改写完成", rewritten=new_copy)

        # ③ Respect the selected voice mode; resuming must reuse the saved audio.
        want_clone = bool(p.get("use_clone"))
        if previous.get("audio_url"):
            audio_file = out_root / ("voice.wav" if want_clone else "voice.mp3")
            if not audio_file.is_file():
                raise PendingTask("原配音文件缺失，未自动重新生成")
            audio = {"path": str(audio_file), "url": previous["audio_url"]}
        elif want_clone:
            if previous and cfg.get("voice_clone", {}).get("provider") != "dashscope":
                raise PendingTask("原声音服务没有可核对的生成记录，已停止自动重做")
            _set(job_id, 3, 55, "正在用你的声音克隆配音…")
            ref_text = (p.get("voice_ref_text") or "").strip()
            if not ref_text:
                ref_txt_path = Path(voice_sample).with_suffix(".txt")
                ref_text = ref_txt_path.read_text(encoding="utf-8").strip() \
                    if ref_txt_path.exists() else ""
            out_wav = out_root / "voice.wav"
            voiceclone.clone(new_copy, voice_sample, ref_text, str(out_wav))
            audio = {"path": str(out_wav), "url": f"/outputs/{job_id}/voice.wav",
                     "duration": tts.audio_duration(out_wav)}
        else:
            if previous:
                raise PendingTask("原普通配音缺少完成记录，已停止自动重做")
            _set(job_id, 3, 55, "正在生成配音…")
            audio = tts.generate_audio(
                new_copy, p.get("voice"), float(p.get("speed", 1.0) or 1.0),
                job_id, out_root)
        audio["duration"] = media.probe(audio["path"], "audio")["duration"]
        _set(job_id, 3, 70, "配音生成完成", audio_url=audio["url"], duration=audio["duration"],
             voice_mode="clone" if want_clone else "preset",
             voice_model=previous.get("voice_model") or (cfg.get("voice_clone", {}).get("model") if want_clone else p.get("voice")))

        _set(job_id, 4, 78, "底版已确认，使用本次提交的素材")
        final_dir = out_root / "final"
        _set(job_id, 5, 82, "正在使用你的视频和配音生成口型…")
        output = final_dir / "video.mp4"
        lipsync.lipsync(face, audio["path"], str(output))
        _set(job_id, 6, 96, "正在检查成片画面、音轨和时长…")
        checked = media.check_video(output, audio["duration"])
        _set(job_id, 6, 100, "成片完成", video_url=f"/outputs/{job_id}/final/video.mp4",
             placeholder=False, video_duration=checked["duration"],
             media_checks_passed=True, quality_review="pending")

        db.update_job(job_id, status="done", message="成片完成 🎉")
    except PendingTask as exc:
        db.update_job(job_id, status="waiting", message=str(exc))
    except Exception as e:
        traceback.print_exc()
        # 失败退还点数
        cost = int(p.get("_cost", 0) or 0)
        if cost:
            db.refund_job_once(job_id, job["user_id"], cost)
        db.update_job(job_id, status="failed",
                      message=f"失败：{e}（点数已退还）")
