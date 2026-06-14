"""6 步全自动流水线编排（后台线程跑），实时更新任务进度供前端轮询。"""
import traceback
from pathlib import Path

import db
from config import (OUTPUTS_DIR, find_user_face, find_user_voice, load_config)
from services import (compose, digital_human, extract, lipsync, rewrite, tts,
                      voiceclone)

# 6 步对应进度（与前端步骤条一致）
STEPS = ["文案生成", "改写文案", "音频生成", "选择数字人形象", "成片/智能包装", "最终成片"]


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
    if not job:
        return
    p = job["params"]
    out_root = OUTPUTS_DIR / job_id
    try:
        # ① 文案生成（扒链接）
        _set(job_id, 1, 5, "正在自动化提取爆款文案…")
        link = extract.clean_link(p.get("link", ""))
        ex = extract.extract_from_link(link, job_id)
        _set(job_id, 1, 25, "已提取原视频口播文案",
             transcript=ex["transcript"], source_title=ex["title"], cover=ex.get("cover"))

        # ② 改写文案
        _set(job_id, 2, 30, "AI 正在改写成你行业的文案…")
        new_copy = rewrite.rewrite_copy(
            ex["transcript"], p.get("industry", ""), p.get("extra", ""), cfg)
        _set(job_id, 2, 50, "文案改写完成", rewritten=new_copy)

        # ③ 音频生成：用户上传了声音样本 + 克隆引擎就绪 -> 用克隆声音；否则 edge-tts
        voice_sample = find_user_voice(job["user_id"])
        want_clone = bool(p.get("use_clone")) and voice_sample and voiceclone.available()
        if want_clone:
            _set(job_id, 3, 55, "正在用你的声音克隆配音…")
            ref_txt_path = Path(voice_sample).with_suffix(".txt")
            ref_text = ref_txt_path.read_text(encoding="utf-8").strip() \
                if ref_txt_path.exists() else ""
            out_wav = out_root / "voice.wav"
            voiceclone.clone(new_copy, voice_sample, ref_text, str(out_wav))
            audio = {"path": str(out_wav), "url": f"/outputs/{job_id}/voice.wav",
                     "duration": tts.audio_duration(out_wav)}
        else:
            _set(job_id, 3, 55, "正在生成配音…")
            audio = tts.generate_audio(
                new_copy, p.get("voice"), float(p.get("speed", 1.0) or 1.0),
                job_id, out_root)
        _set(job_id, 3, 70, "配音生成完成", audio_url=audio["url"], duration=audio["duration"])

        # ④ 选择数字人形象
        avatar_id = p.get("avatar") or "a1"
        theme = next((t for t in compose.AVATAR_THEMES if t["id"] == avatar_id),
                     compose.AVATAR_THEMES[0])
        _set(job_id, 4, 78, f"已选择数字人形象：{theme['name']}", avatar=avatar_id)

        # ⑤⑥ 成片：优先级 云端数字人API > 本地Wav2Lip对口型 > 占位卡
        final_dir = out_root / "final"
        dh = None
        # 1) 配了云端数字人服务就用云端
        if cfg.get("digital_human", {}).get("provider", "none") != "none":
            _set(job_id, 5, 82, "正在合成数字人视频（云端）…")
            dh = digital_human.generate(
                audio["path"], new_copy, avatar_id, cfg, job_id, final_dir)
        # 2) 否则：用户上传了口播底版 + 本地引擎就绪 -> 本地免费对口型
        if dh is None:
            face = find_user_face(job["user_id"])
            if face and lipsync.available():
                _set(job_id, 5, 82, "正在对口型（本地 Wav2Lip · 免费）…")
                lipsync.lipsync(face, audio["path"],
                                str(final_dir / "video.mp4"))
                dh = {"video_url": f"/outputs/{job_id}/final/video.mp4",
                      "placeholder": False}
        # 3) 兜底：占位成片（头像卡+配音）
        if dh is None:
            _set(job_id, 5, 82, "生成占位成片（未上传底版/未配数字人）…")
            dh = compose.compose_placeholder(
                new_copy, audio["path"], theme,
                cfg["brand"]["name"], job_id, final_dir, audio["duration"])
        _set(job_id, 6, 100, "成片完成 🎉", **dh)

        db.update_job(job_id, status="done", message="成片完成 🎉")
    except Exception as e:
        traceback.print_exc()
        # 失败退还点数
        cost = int(p.get("_cost", 0) or 0)
        if cost:
            db.add_credits(job["user_id"], cost)
        db.update_job(job_id, status="failed",
                      message=f"失败：{e}（点数已退还）")
