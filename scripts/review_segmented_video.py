"""Build a local review page for a completed over-120-second segmented result.

This is read-only: it verifies the saved segment/vendor evidence and media, then
builds a browser page for human review. It never submits, polls, or edits jobs.
"""
import argparse
import hashlib
import html
import json
import os
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def media_probe(path, kind):
    result = subprocess.run([
        "ffprobe", "-v", "error", "-of", "json", "-show_streams", "-show_format",
        str(path),
    ], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    streams = [s for s in payload["streams"] if s.get("codec_type") == kind]
    if not streams:
        raise RuntimeError(f"缺少 {kind} 轨道: {path}")
    stream = streams[0]
    return {"duration": float(stream.get("duration") or payload["format"].get("duration") or 0),
            "width": stream.get("width"), "height": stream.get("height"),
            "codec": stream.get("codec_name")}


def frame_count(path):
    with wave.open(str(path), "rb") as source:
        return source.getnframes()


def render_images(video, review, duration):
    review.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-threads", "2", "-ss", "2", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=540:-2", "-map_metadata", "-1", "-y",
        str(review / "poster.jpg"),
    ], check=True)
    interval = max(duration / 8, 1)
    subprocess.run([
        "ffmpeg", "-v", "error", "-threads", "2", "-i", str(video), "-vf",
        f"fps=1/{interval},scale=180:-2,tile=4x2", "-frames:v", "1",
        "-map_metadata", "-1", "-y", str(review / "contact-sheet.jpg"),
    ], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, help="包含 final/video.mp4 和 segments.json 的分段结果目录")
    parser.add_argument("--review-dir", type=Path)
    args = parser.parse_args()

    result_root = (ROOT / args.root).resolve() if not args.root.is_absolute() else args.root.resolve()
    manifest_path = result_root / "segments.json"
    video = result_root / "video.mp4"
    full_audio = result_root / "full-audio.wav"
    if not manifest_path.exists() or not video.exists() or not full_audio.exists():
        raise RuntimeError("分段结果缺少 manifest、最终视频或完整音频")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise RuntimeError("分段任务尚未完成，不能生成通过式复核页")

    audio_info = media_probe(full_audio, "audio")
    video_info = media_probe(video, "video")
    if digest(video) != manifest.get("sha256"):
        raise RuntimeError("最终视频 SHA-256 与 manifest 不一致")
    if abs(video_info["duration"] - audio_info["duration"]) > 0.25:
        raise RuntimeError("最终音视频时长偏差超过 250ms")

    full_frames = frame_count(full_audio)
    core_frames = 0
    parts = []
    for item in manifest.get("parts", []):
        folder = result_root / item["name"]
        core_audio = folder / "audio.wav"
        timeline = folder / "timeline.mp4"
        task_path = folder / "videoretalk-task.json"
        if not all(path.exists() for path in (core_audio, timeline, task_path)):
            raise RuntimeError(f"分段 {item['name']} 缺少成片、音频或供应商记录")
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task.get("status") != "SUCCEEDED":
            raise RuntimeError(f"分段 {item['name']} 供应商状态不是 SUCCEEDED")
        if digest(core_audio) != item.get("audio_sha256"):
            raise RuntimeError(f"分段 {item['name']} 音频哈希不一致")
        if digest(timeline) != item.get("aligned_sha256"):
            raise RuntimeError(f"分段 {item['name']} 对齐视频哈希不一致")
        count = frame_count(core_audio)
        core_frames += count
        parts.append({
            "name": item["name"], "start_sample": item["start_sample"],
            "end_sample": item["end_sample"], "duration": item["duration"],
            "request_duration": item.get("request_duration"),
            "video_duration": media_probe(timeline, "video")["duration"],
            "task_id": task.get("task_id"), "usage": task.get("usage", {}),
        })
    if core_frames != full_frames:
        raise RuntimeError(f"核心音频样本不守恒: {core_frames} != {full_frames}")

    app_output_root = Path(os.environ.get("APP_OUTPUTS_DIR", ROOT / "outputs/e2e-20260928")).resolve()
    try:
        output_rel = result_root.relative_to(app_output_root)
    except ValueError:
        output_rel = result_root.relative_to(ROOT / "outputs")
    media_url = "/outputs/" + str(output_rel).replace("\\", "/")
    review = args.review_dir.resolve() if args.review_dir else result_root / "quality-review"
    render_images(video, review, video_info["duration"])
    evidence = {
        "scope": "completed over-120-second segmented VideoRetalk result",
        "technical_acceptance": "passed",
        "effect_review": "pending_human_review",
        "new_paid_requests": 0,
        "video_sha256": digest(video),
        "video_duration": video_info["duration"],
        "audio_duration": audio_info["duration"],
        "video_stream": video_info,
        "core_audio_frames": core_frames,
        "full_audio_frames": full_frames,
        "audio_frame_conservation": core_frames == full_frames,
        "parts": parts,
        "limits": [
            "技术检查不判断声音是否像本人",
            "技术检查不判断每个音素的口型自然度",
            "短底版的动作重复仍需人工观看确认",
        ],
    }
    budget_path = result_root.parent / "budget.json"
    if budget_path.exists():
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
        evidence["budget"] = {
            "new_authorized_cny": budget.get("new_authorized_cny"),
            "hard_run_limit_cny": budget.get("hard_run_limit_cny"),
            "actual_cny": budget.get("actual_cny"),
            "new_paid_submissions": budget.get("new_paid_submissions"),
            "status": budget.get("status"),
        }
    (review / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    chapters = "".join(
        f'<option value="{p["start_sample"] / audio_info.get("sample_rate", 24000):.3f},{p["end_sample"] / audio_info.get("sample_rate", 24000):.3f}">{html.escape(p["name"])} · {p["duration"]:.2f} 秒</option>'
        for p in parts
    )
    page = TEMPLATE.replace("__VIDEO__", media_url + "/video.mp4")
    page = page.replace("__AUDIO__", media_url + "/full-audio.wav")
    page = page.replace("__POSTER__", "poster.jpg").replace("__CONTACT__", "contact-sheet.jpg")
    page = page.replace("__DURATION__", f"{video_info['duration']:.3f}")
    page = page.replace("__CHAPTERS__", chapters)
    (review / "index.html").write_text(page, encoding="utf-8")
    print(json.dumps({"review": str(review), "technical_acceptance": "passed",
                      "effect_review": "pending_human_review", "parts": len(parts),
                      "video_duration": video_info["duration"]}, ensure_ascii=False))


TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>长片分段效果复核</title>
<style>*{box-sizing:border-box;letter-spacing:0}body{margin:0;background:#f6f7f8;color:#202429;font:15px/1.6 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #ced4da;margin-bottom:20px}h1{font-size:24px;line-height:1.3;margin:0 0 16px}h2{font-size:17px;margin:0 0 10px}p{margin:8px 0}a{color:#126347}.layout{display:grid;grid-template-columns:minmax(240px,380px) minmax(0,1fr);gap:28px;align-items:start}video{display:block;width:100%;aspect-ratio:9/16;object-fit:contain;background:#151515}audio{width:100%;max-width:100%}select{display:block;width:100%;max-width:100%;font:inherit;margin:10px 0;padding:8px;border:1px solid #89939b;border-radius:4px;background:white}section{padding:0 0 18px;margin-bottom:18px;border-bottom:1px solid #dce0e3}.pass{color:#147a54;font-weight:600}.pending{color:#9b351f;font-weight:600}.muted{color:#5c666f;font-size:13px}img{max-width:100%;height:auto;border:1px solid #dce0e3}@media(max-width:640px){main{padding:16px}.layout{grid-template-columns:minmax(0,1fr)}h1{font-size:21px}}</style></head>
<body><main><header><h1>长片分段效果复核</h1><a href="/">返回工作台</a></header><div class="layout"><div><video id="video" controls playsinline preload="metadata" poster="__POSTER__" src="__VIDEO__"></video><label for="chapter">检查片段</label><select id="chapter"><option value="0,__DURATION__">完整成片 · 00:00–全片</option>__CHAPTERS__</select><p><a href="__VIDEO__" download>下载完整成片</a></p><audio controls preload="metadata" src="__AUDIO__"></audio></div><div><section><h2>验收状态</h2><p class="pass">技术完整性：通过</p><p class="pending">人工效果：待确认</p><p>技术检查已确认分段供应商结果、最终音视频、核心音频样本守恒和浏览器播放条件；这不等于声音相似度或口型自然度已通过。</p></section><section><h2>全片画面抽查</h2><img src="__CONTACT__" alt="全片抽帧联系表"><p class="muted">请重点观察分段边界、嘴型、牙齿/嘴边、头部和手部动作是否自然。</p></section><section><h2>人工确认项</h2><ul><li>声音是否像本人，是否有明显机器感或错误读音。</li><li>开口、闭口、唇齿音是否与配音同步。</li><li>分段边界是否出现姿态、构图或动作跳变。</li><li>短底版循环动作是否可以接受。</li></ul><a href="evidence.json">查看技术证据 JSON</a></section></div></div></main><script>const v=document.getElementById('video'),s=document.getElementById('chapter');s.addEventListener('change',()=>{const [a,b]=s.value.split(',').map(Number);v.pause();v.currentTime=a;v.ontimeupdate=()=>{if(b&&v.currentTime>=b){v.pause();v.ontimeupdate=null}}});for(const x of document.querySelectorAll('audio,video'))x.addEventListener('play',()=>document.querySelectorAll('audio,video').forEach(y=>{if(y!==x)y.pause()}));</script></body></html>"""


if __name__ == "__main__":
    main()
