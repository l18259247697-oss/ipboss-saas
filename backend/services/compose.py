"""步骤④⑤⑥占位成片：Pillow 渲染数字人形象卡 + ffmpeg 合成「头像图 + 配音」真 mp4。
   说明：本机 ffmpeg 未编 libass，字幕以 .srt 旁挂输出；接入云端数字人后会被真对口型视频替换。"""
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import CN_FONT

W, H = 1080, 1920  # 竖屏 9:16
AVATAR_THEMES = [
    {"id": "a1", "name": "商务男·西装", "bg": (28, 33, 48), "accent": (255, 138, 0)},
    {"id": "a2", "name": "知性女·职场", "bg": (40, 30, 46), "accent": (236, 100, 140)},
    {"id": "a3", "name": "活力青年·街头", "bg": (24, 42, 40), "accent": (0, 200, 160)},
    {"id": "a4", "name": "亲和大姐·门店", "bg": (46, 36, 26), "accent": (255, 184, 0)},
    {"id": "a5", "name": "专家顾问·书房", "bg": (26, 34, 44), "accent": (90, 160, 255)},
    {"id": "a6", "name": "国风主播·茶室", "bg": (44, 28, 28), "accent": (220, 90, 70)},
]


def _font(size):
    return ImageFont.truetype(CN_FONT, size)


def render_avatar(theme: dict, out_path: Path):
    """生成一张数字人形象占位图（供步骤④形象库展示用）。"""
    img = Image.new("RGB", (400, 520), theme["bg"])
    d = ImageDraw.Draw(img)
    # 头像圆
    cx, cy, r = 200, 200, 110
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=theme["accent"])
    d.ellipse([cx - 45, cy - 50, cx + 45, cy + 20], fill=(255, 255, 255))  # 脸
    d.rounded_rectangle([cx - 70, cy + 30, cx + 70, cy + 130], 40, fill=(255, 255, 255))  # 身
    # 名字
    f = _font(34)
    name = theme["name"]
    tw = d.textlength(name, font=f)
    d.text(((400 - tw) / 2, 410), name, font=f, fill=(255, 255, 255))
    img.save(out_path)


def _render_card(text: str, theme: dict, brand: str, out_path: Path):
    """成片背景卡：品牌 + 数字人形象 + 当前文案首段。"""
    img = Image.new("RGB", (W, H), theme["bg"])
    d = ImageDraw.Draw(img)
    # 顶部品牌条
    d.rectangle([0, 0, W, 150], fill=theme["accent"])
    bf = _font(64)
    d.text((60, 40), brand, font=bf, fill=(255, 255, 255))
    # 中部数字人形象（占位）
    cx, cy, r = W // 2, 620, 260
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=theme["accent"])
    d.ellipse([cx - 110, cy - 120, cx + 110, cy + 40], fill=(255, 255, 255))
    d.rounded_rectangle([cx - 170, cy + 70, cx + 170, cy + 320], 90, fill=(255, 255, 255))
    # 文案预览（底部大字幕区）
    f = _font(58)
    preview = text.strip().replace("\n", " ")
    lines = textwrap.wrap(preview, width=16)[:5]
    y = 1180
    for ln in lines:
        tw = d.textlength(ln, font=f)
        d.text(((W - tw) / 2, y), ln, font=f, fill=(255, 255, 255))
        y += 86
    # 角标
    sf = _font(36)
    tag = "数字人口播 · 演示成片"
    d.text((60, H - 90), tag, font=sf, fill=(200, 200, 200))
    img.save(out_path)


def _write_srt(text: str, duration: float, out_path: Path):
    import re as _re
    sents = [s for s in _re.split(r"[。！？!?\n]", text) if s.strip()]
    if not sents:
        sents = [text]
    total_chars = sum(len(s) for s in sents) or 1
    t = 0.0

    def ts(sec):
        h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = []
    for i, s in enumerate(sents, 1):
        dur = duration * (len(s) / total_chars)
        lines.append(f"{i}\n{ts(t)} --> {ts(t + dur)}\n{s.strip()}\n")
        t += dur
    out_path.write_text("\n".join(lines), encoding="utf-8")


def compose_placeholder(text, audio_path, theme, brand, job_id, out_dir: Path, duration: float):
    """合成占位成片 mp4（头像卡 + 配音）。返回 {video url, srt url}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    card = out_dir / "card.png"
    _render_card(text, theme, brand, card)
    srt = out_dir / "subtitle.srt"
    _write_srt(text, duration or 1.0, srt)

    video = out_dir / "video.mp4"
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(card), "-i", str(audio_path),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-vf", "scale=1080:1920",
        str(video),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not video.exists():
        raise RuntimeError(f"成片合成失败：{(proc.stderr or '')[-400:]}")
    return {
        "video_url": f"/outputs/{job_id}/final/video.mp4",
        "srt_url": f"/outputs/{job_id}/final/subtitle.srt",
        "placeholder": True,
    }
