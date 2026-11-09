"""Build a local-only review page for the existing 2026-10-14 E2E sample.

No provider imports, network access, generation requests, or database writes.
Motion matching excludes the face and is not a lip-sync quality measurement.
"""
import hashlib
import html
import json
import sqlite3
import subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
JOB = "989f97d01076"
OUT = ROOT / "outputs/e2e-20260928"
MEDIA = OUT / JOB
REVIEW = OUT / "quality-review-20261014"
VIDEO_SHA = "125964e580fdabfeb7531008a0ae91da39ceb04ae82b9244450e229425d2f46c"
VOICE_SHA = "3178e7ebc102252fe8c4152b499c76163071691b96c31aa889148b55f4361bea"


def digest(path):
    with path.open("rb") as source:
        value = hashlib.sha256()
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def body_frames(path):
    data = subprocess.check_output([
        "ffmpeg", "-v", "error", "-threads", "2", "-i", str(path), "-an",
        "-vf", "fps=10,crop=iw*0.8:ih*0.52:iw*0.1:ih*0.44,scale=32:48,format=gray",
        "-filter_threads", "1", "-threads", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ])
    return np.frombuffer(data, np.uint8).reshape(-1, 1536).astype(np.int16)


def main():
    video, voice = MEDIA / "final/video.mp4", MEDIA / "voice.wav"
    if digest(video) != VIDEO_SHA or digest(voice) != VOICE_SHA:
        raise RuntimeError("Sample identity changed; do not attach old review findings")
    with sqlite3.connect(f"file:{ROOT / 'data/e2e-20260928/app.db'}?mode=ro", uri=True) as db:
        status, payload = db.execute("SELECT status,result FROM jobs WHERE id=?", (JOB,)).fetchone()
    result = json.loads(payload)
    if status != "done" or result.get("quality_review") != "pending":
        raise RuntimeError("Sample review state changed; refresh the review conclusions first")
    source, output = body_frames(MEDIA / "inputs/face.mov"), body_frames(video)
    scores = np.stack([np.abs(output - frame).mean(axis=1) for frame in source], axis=1)
    matches = scores.argmin(axis=1)
    near_start = matches <= 1
    returns = [i for i in range(1, len(matches)) if near_start[i] and not near_start[i - 1]]
    # Collapse ambiguous neighboring low-motion matches, not the original video.
    cycles = []
    for frame in returns:
        if not cycles or frame - cycles[-1] > 50:
            cycles.append(frame)
    report = {
        "job_id": JOB, "video_sha256": VIDEO_SHA, "voice_sha256": VOICE_SHA,
        "status": status, "quality_review": result["quality_review"],
        "method": "10 fps lower-body grayscale nearest-source-frame comparison",
        "not_a_lipsync_or_voice_similarity_score": True,
        "new_paid_requests": 0, "database_writes": 0,
        "source_frames_sampled": len(source), "output_frames_sampled": len(output),
        "median_matching_pixel_error": float(np.median(scores.min(axis=1))),
        "returns_to_source_start_seconds": [round(i / 10, 1) for i in cycles],
        "median_cycle_seconds": float(np.median(np.diff(cycles)) / 10),
        "backwards_source_jumps_over_3_seconds": [i / 10 for i in range(1, len(matches))
                                                  if int(matches[i]) - int(matches[i - 1]) < -30],
        "matched_source_seconds": [round(int(i) / 10, 1) for i in matches],
        "limits": ["Body repetition only; no phoneme alignment or likeness judgment",
                   "10 fps sampling cannot exclude short glitches", "Human effect acceptance still pending",
                   "Over-120-second fresh segmented E2E not run"],
    }
    REVIEW.mkdir(parents=True, exist_ok=True)
    (REVIEW / "motion-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    subprocess.run([
        "ffmpeg", "-v", "error", "-threads", "2", "-ss", "2", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=540:-2", "-filter_threads", "1", "-threads", "1",
        "-map_metadata", "-1", "-y", str(REVIEW / "poster.jpg"),
    ], check=True)
    page = TEMPLATE.replace("__JOB__", JOB).replace("__SCRIPT__", html.escape(result["rewritten"]))
    (REVIEW / "index.html").write_text(page)
    print(json.dumps({k: v for k, v in report.items() if k != "matched_source_seconds"}, ensure_ascii=False))


TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>完整成片效果复核</title>
<style>
*{box-sizing:border-box;letter-spacing:0}body{margin:0;background:#f6f7f8;color:#202429;font:15px/1.6 system-ui,sans-serif}
main{max-width:1120px;margin:auto;padding:24px}header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;border-bottom:1px solid #ced4da;margin-bottom:20px}
h1{font-size:24px;line-height:1.3;margin:0 0 16px}h2{font-size:17px;margin:0 0 10px}p{margin:8px 0}a{color:#126347}
.layout{display:grid;grid-template-columns:minmax(240px,380px) minmax(0,1fr);gap:28px;align-items:start}
video{display:block;width:100%;aspect-ratio:9/16;object-fit:contain;background:#151515}audio{width:100%;max-width:100%}
select{display:block;width:100%;max-width:100%;font:inherit;margin:10px 0;padding:8px;border:1px solid #89939b;border-radius:4px;background:white}
section{padding:0 0 18px;margin-bottom:18px;border-bottom:1px solid #dce0e3}.status{color:#9b351f;font-weight:600}.muted{color:#5c666f;font-size:13px}
ul{padding-left:20px;margin:8px 0}li{margin:8px 0}.script{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;font-weight:600}
@media(max-width:640px){main{padding:16px}.layout{grid-template-columns:minmax(0,1fr)}header{align-items:flex-start}h1{font-size:21px}video{max-height:70vh}}
</style></head><body><main>
<header><h1>完整成片效果复核</h1><a href="/">工作台</a></header>
<div class="layout"><div>
<video id="reviewVideo" controls playsinline preload="metadata" poster="poster.jpg" src="/outputs/__JOB__/final/video.mp4"></video>
<label for="chapter">检查片段</label>
<select id="chapter">
<option value="0,111.733304">完整成片 · 00:00–01:51.73</option>
<option value="0,5">开头口型 · 00:00–00:05</option>
<option value="6.5,9">动作转向 · 00:06.5–00:09</option>
<option value="13.5,17">动作循环 · 00:13.5–00:17</option>
<option value="54,58">读音复核：克隆 · 00:54–00:58</option>
<option value="81.5,89">读音复核：成片 · 01:21.5–01:29</option>
<option value="105,111.733304">结尾完整性 · 01:45–01:51.73</option>
</select>
<div id="playError" role="status"></div>
<a href="/outputs/__JOB__/final/video.mp4" download>下载原始完整成片</a>
<p class="muted">任务 __JOB__ · 原速 111.73 秒 · 1080 × 1920</p>
</div><div>
<section><h2>验收状态</h2><p class="status">链路已完成，效果待本人确认</p>
<p>当前使用千问 3.0 TTS Plus 配音和 VideoRetalk 口型。此页复用已有成片，没有重新生成或新增 API 请求。</p></section>
<section><h2>当前发现</h2><ul>
<li>底版约 7.45 秒，画面按正放、倒放延长，动作约每 15 秒重复一轮。连续衔接不等于动作自然。</li>
<li>全片音轨与本次配音对应；本地转写未发现整段漏读，但“成片 / 影片”存在识别差异，待试听确认。</li>
<li>“不是那种一听就很假的机器音”是稿件的主观描述，不是已经验证的效果保证。</li>
<li>这条成片不足两分钟，超过两分钟的全新分段 E2E 仍未验收。</li>
</ul></section>
<section><h2>本人录音</h2><audio controls preload="metadata" src="/outputs/__JOB__/inputs/reference.wav"></audio>
<p class="muted">大家好我是手作汉堡黑哥，我现在在同安开了一家手作汉堡店，天天生意火爆</p>
<h2>本次完整配音</h2><audio controls preload="metadata" src="/outputs/__JOB__/voice.wav"></audio></section>
<section><h2>仍待确认</h2><p>声音像不像本人；说话与嘴型是否同步；牙齿和嘴边是否自然；动作循环能否接受。自动检查不替代这些判断。</p>
<a href="motion-evidence.json">本地动作比对证据</a></section>
<details><summary>本次完整口播稿</summary><p class="script">__SCRIPT__</p></details>
</div></div></main>
<script>
const video=document.getElementById('reviewVideo'),chapter=document.getElementById('chapter'),error=document.getElementById('playError');
let stopAt=null,pendingStart=null;
const seek=()=>{if(pendingStart!==null&&video.readyState>0){video.currentTime=pendingStart;pendingStart=null;}};
chapter.addEventListener('change',()=>{
  const [start,end]=chapter.value.split(',').map(Number);video.pause();error.textContent='';
  stopAt=start===0&&end>110?null:end;pendingStart=start;seek();
});
video.addEventListener('loadedmetadata',seek);
video.addEventListener('error',()=>{error.textContent='视频加载失败，请刷新重试。';});
video.addEventListener('timeupdate',()=>{if(stopAt!==null&&video.currentTime>=stopAt){video.pause();stopAt=null;}});
for(const current of document.querySelectorAll('audio,video'))current.addEventListener('play',()=>{
  for(const other of document.querySelectorAll('audio,video'))if(other!==current)other.pause();
});
</script></body></html>"""


if __name__ == "__main__":
    main()
