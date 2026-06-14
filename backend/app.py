"""一键追爆 SaaS —— FastAPI 后端入口。"""
import os
import threading
import uuid

from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     UploadFile)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import pipeline
from config import (FACES_DIR, FRONTEND_DIR, OUTPUTS_DIR, VOICES, VOICES_DIR,
                    find_user_face, find_user_voice, load_config, save_config)
from services import compose, lipsync, voiceclone

app = FastAPI(title="一键追爆 SaaS")


# ---------- 启动初始化 ----------
@app.on_event("startup")
def _startup():
    db.init_db()
    cfg = load_config()
    # 默认管理员
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    if not db.user_by_name(admin_user):
        db.create_user(admin_user, os.environ.get("ADMIN_PASSWORD", "admin888"),
                       credits=999999, is_admin=1)
    # 生成数字人形象占位图
    av_dir = OUTPUTS_DIR / "avatars"
    av_dir.mkdir(parents=True, exist_ok=True)
    for t in compose.AVATAR_THEMES:
        f = av_dir / f"{t['id']}.png"
        if not f.exists():
            compose.render_avatar(t, f)


# ---------- 鉴权 ----------
def auth(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    u = db.user_by_token(token)
    if not u:
        raise HTTPException(401, "未登录或登录已过期")
    return u


def admin_only(u=Depends(auth)):
    if not u["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return u


# ---------- 数据模型 ----------
class AuthIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    link: str
    industry: str = ""
    extra: str = ""
    voice: str = "zh-CN-XiaoxiaoNeural"
    speed: float = 1.0
    avatar: str = "a1"
    mode: str = "auto"
    use_clone: bool = False


# ---------- 账号 ----------
@app.post("/api/register")
def register(body: AuthIn):
    if db.user_by_name(body.username):
        raise HTTPException(400, "用户名已存在")
    if len(body.username) < 3 or len(body.password) < 6:
        raise HTTPException(400, "用户名≥3位，密码≥6位")
    cfg = load_config()
    uid = db.create_user(body.username, body.password, credits=cfg["register_bonus"])
    token = db.verify_login(body.username, body.password)
    return {"token": token, "username": body.username, "credits": db.get_credits(uid)}


@app.post("/api/login")
def login(body: AuthIn):
    token = db.verify_login(body.username, body.password)
    if not token:
        raise HTTPException(401, "用户名或密码错误")
    u = db.user_by_name(body.username)
    return {"token": token, "username": u["username"],
            "credits": u["credits"], "is_admin": bool(u["is_admin"])}


@app.get("/api/me")
def me(u=Depends(auth)):
    face = find_user_face(u["id"])
    voice = find_user_voice(u["id"])
    return {"username": u["username"], "credits": db.get_credits(u["id"]),
            "is_admin": bool(u["is_admin"]),
            "has_face": bool(face),
            "face_url": f"/data/faces/{Path(face).name}" if face else None,
            "has_voice": bool(voice),
            "voice_url": f"/data/voices/{Path(voice).name}" if voice else None}


@app.post("/api/voice")
async def upload_voice(file: UploadFile = File(...), ref_text: str = Form(""),
                       u=Depends(auth)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".aac", ".mp4", ".mov"}:
        raise HTTPException(400, "请上传音频(mp3/wav/m4a)或带人声的视频")
    for old in VOICES_DIR.glob(f"u{u['id']}.*"):
        old.unlink()
    dest = VOICES_DIR / f"u{u['id']}{ext}"
    dest.write_bytes(await file.read())
    if ref_text.strip():
        (VOICES_DIR / f"u{u['id']}.txt").write_text(ref_text.strip(), encoding="utf-8")
    return {"ok": True, "voice_url": f"/data/voices/{dest.name}"}


@app.post("/api/face")
async def upload_face(file: UploadFile = File(...), u=Depends(auth)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".mp4", ".mov"}:
        raise HTTPException(400, "只支持图片(jpg/png)或视频(mp4/mov)")
    for old in FACES_DIR.glob(f"u{u['id']}.*"):
        old.unlink()
    dest = FACES_DIR / f"u{u['id']}{ext}"
    data = await file.read()
    dest.write_bytes(data)
    return {"ok": True, "face_url": f"/data/faces/{dest.name}",
            "is_video": ext in {".mp4", ".mov"}}


# ---------- 公共配置 ----------
@app.get("/api/public")
def public():
    cfg = load_config()
    return {
        "brand": cfg["brand"],
        "voices": VOICES,
        "avatars": [{"id": t["id"], "name": t["name"],
                     "img": f"/outputs/avatars/{t['id']}.png"}
                    for t in compose.AVATAR_THEMES],
        "credits_per_video": cfg["credits_per_video"],
        "llm_ready": bool(cfg["llm"]["api_key"] and cfg["llm"]["base_url"]),
        "dh_ready": cfg["digital_human"]["provider"] != "none",
        "lipsync_ready": lipsync.available(),
        "voiceclone_ready": voiceclone.available(),
    }


# ---------- 任务 ----------
@app.post("/api/jobs")
def create_job(body: JobIn, u=Depends(auth)):
    cfg = load_config()
    cost = cfg["credits_per_video"]
    if db.get_credits(u["id"]) < cost:
        raise HTTPException(402, f"点数不足（需 {cost} 点），请充值")
    db.add_credits(u["id"], -cost)  # 先扣，失败再退
    job_id = uuid.uuid4().hex[:12]
    params = body.dict()
    params["_cost"] = cost
    db.create_job(job_id, u["id"], params)
    threading.Thread(target=pipeline.run_pipeline, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, "credits": db.get_credits(u["id"])}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, u=Depends(auth)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != u["id"]:
        raise HTTPException(404, "任务不存在")
    return {
        "id": job["id"], "status": job["status"], "step": job["step"],
        "progress": job["progress"], "message": job["message"],
        "result": job["result"], "steps": pipeline.STEPS,
    }


# ---------- 后台（管理员）----------
@app.get("/api/admin/config")
def admin_get_config(u=Depends(admin_only)):
    return load_config()


@app.post("/api/admin/config")
def admin_set_config(cfg: dict, u=Depends(admin_only)):
    return save_config(cfg)


@app.get("/api/admin/users")
def admin_users(u=Depends(admin_only)):
    return db.list_users()


class CreditIn(BaseModel):
    user_id: int
    delta: int


@app.post("/api/admin/credits")
def admin_credits(body: CreditIn, u=Depends(admin_only)):
    db.add_credits(body.user_id, body.delta)
    return {"ok": True}


# ---------- 静态文件 ----------
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")
app.mount("/data/faces", StaticFiles(directory=str(FACES_DIR)), name="faces")
app.mount("/data/voices", StaticFiles(directory=str(VOICES_DIR)), name="voices")


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(str(FRONTEND_DIR / "admin.html"))


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
