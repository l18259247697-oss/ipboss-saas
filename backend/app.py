"""一键追爆 SaaS —— FastAPI 后端入口。"""
import os
import copy
import hashlib
import shutil
import tempfile
import threading
import uuid
import json
from typing import Optional

from pathlib import Path

from fastapi import (Cookie, Depends, FastAPI, File, Form, Header, HTTPException,
                     Query, Response, UploadFile)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import pipeline
from config import (FACES_DIR, FRONTEND_DIR, OUTPUTS_DIR, VOICES, VOICES_DIR,
                    find_user_face, find_user_voice, load_config, save_config)
from services import compose, lipsync, media, voiceclone
from services.videoretalk import validate_face

app = FastAPI(title="一键追爆 SaaS")

MAX_VOICE_UPLOAD_BYTES = int(os.environ.get("MAX_VOICE_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_FACE_UPLOAD_BYTES = int(os.environ.get("MAX_FACE_UPLOAD_BYTES", str(300 * 1024 * 1024)))
JOB_MAX_CONCURRENCY = max(1, int(os.environ.get("JOB_MAX_CONCURRENCY", "1")))
_PIPELINE_SLOTS = threading.BoundedSemaphore(JOB_MAX_CONCURRENCY)


def _run_pipeline_with_slot(job_id):
    with _PIPELINE_SLOTS:
        pipeline.run_pipeline(job_id)


def _start_pipeline(job_id):
    threading.Thread(target=_run_pipeline_with_slot, args=(job_id,), daemon=True).start()


# ---------- 启动初始化 ----------
@app.on_event("startup")
def _startup():
    db.init_db()
    db.recover_interrupted_jobs()
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
def auth(authorization: str = Header(default=""), session_token: str = Cookie(default="")):
    token = authorization.replace("Bearer ", "").strip() or session_token.strip()
    u = db.user_by_token(token)
    if not u:
        raise HTTPException(401, "未登录或登录已过期")
    return u


def admin_only(u=Depends(auth)):
    if not u["is_admin"]:
        raise HTTPException(403, "需要管理员权限")
    return u


@app.get("/health")
def health():
    """Liveness probe: no paid provider calls and no credentials in response."""
    try:
        db.init_db()
        db.get_credits(-1)
    except Exception as exc:
        raise HTTPException(503, "数据库不可用") from exc
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Readiness snapshot for deployment probes and operators."""
    cfg = load_config()
    checks = {
        "database": True,
        "llm": bool(cfg["llm"].get("api_key") and cfg["llm"].get("base_url")),
        "lipsync": lipsync.available(),
    }
    payload = {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}
    if payload["status"] != "ready":
        return Response(content=json.dumps(payload, ensure_ascii=False), status_code=503,
                        media_type="application/json")
    return payload


# ---------- 数据模型 ----------
class AuthIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    link: str = ""
    source_text: str = ""
    industry: str = ""
    extra: str = ""
    voice: str = "zh-CN-XiaoxiaoNeural"
    speed: float = 1.0
    avatar: str = "a1"
    mode: str = "auto"
    use_clone: bool = False
    voice_ref_text: str = ""


# ---------- 账号 ----------
def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        "session_token", token, httponly=True, secure=os.environ.get("COOKIE_SECURE") == "1",
        samesite="lax", max_age=7 * 24 * 60 * 60,
    )


def _copy_upload_limited(upload: UploadFile, destination: Path, limit: int):
    total = 0
    with destination.open("wb") as out:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, f"上传文件不能超过 {limit // (1024 * 1024)} MB")
            out.write(chunk)
    return total


def _validate_job_lengths(body: JobIn):
    limits = (("link", body.link, 2048), ("source_text", body.source_text, 30000),
              ("industry", body.industry, 200), ("extra", body.extra, 4000),
              ("voice_ref_text", body.voice_ref_text, 1000))
    for name, value, limit in limits:
        if len(value or "") > limit:
            raise HTTPException(422, f"{name} 不能超过 {limit} 个字符")


@app.post("/api/register")
def register(body: AuthIn, response: Response):
    if db.user_by_name(body.username):
        raise HTTPException(400, "用户名已存在")
    if len(body.username) < 3 or len(body.password) < 6:
        raise HTTPException(400, "用户名≥3位，密码≥6位")
    cfg = load_config()
    uid = db.create_user(body.username, body.password, credits=cfg["register_bonus"])
    token = db.verify_login(body.username, body.password)
    _set_session_cookie(response, token)
    return {"token": token, "username": body.username, "credits": db.get_credits(uid)}


@app.post("/api/login")
def login(body: AuthIn, response: Response):
    token = db.verify_login(body.username, body.password)
    if not token:
        raise HTTPException(401, "用户名或密码错误")
    u = db.user_by_name(body.username)
    _set_session_cookie(response, token)
    return {"token": token, "username": u["username"],
            "credits": u["credits"], "is_admin": bool(u["is_admin"])}


@app.get("/api/me")
def me(response: Response, authorization: str = Header(default=""), u=Depends(auth)):
    header_token = authorization.replace("Bearer ", "").strip()
    if header_token:
        _set_session_cookie(response, header_token)
    face = find_user_face(u["id"])
    voice = find_user_voice(u["id"])
    ref_file = Path(voice).with_suffix(".txt") if voice else None
    return {"username": u["username"], "credits": db.get_credits(u["id"]),
            "is_admin": bool(u["is_admin"]),
            "has_face": bool(face),
            "face_url": "/api/media/face" if face else None,
            "has_voice": bool(voice),
            "voice_url": "/api/media/voice" if voice else None,
            "voice_revision": hashlib.sha256(Path(voice).read_bytes()).hexdigest() if voice else "",
            "voice_ref_text": ref_file.read_text(encoding="utf-8") if ref_file and ref_file.exists() else ""}


@app.post("/api/logout")
def logout(response: Response, u=Depends(auth)):
    response.delete_cookie("session_token", path="/")
    return {"ok": True}


@app.post("/api/voice")
async def upload_voice(file: UploadFile = File(...), ref_text: str = Form(""),
                       u=Depends(auth)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a", ".aac", ".mp4", ".mov"}:
        raise HTTPException(400, "请上传音频(mp3/wav/m4a)或带人声的视频")
    dest = VOICES_DIR / f"u{u['id']}.wav"
    with tempfile.TemporaryDirectory(dir=VOICES_DIR) as tmp:
        source = Path(tmp) / ("source" + ext)
        _copy_upload_limited(file, source, MAX_VOICE_UPLOAD_BYTES)
        normalized = Path(tmp) / "voice.wav"
        try:
            info = media.normalize_audio(source, normalized)
            if not 2 < info["duration"] < 30:
                raise RuntimeError("声音样本须大于 2 秒且小于 30 秒，请上传完整的短录音")
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        normalized.replace(dest)
    for old in VOICES_DIR.glob(f"u{u['id']}.*"):
        if old != dest:
            old.unlink()
    dest.with_suffix(".txt").write_text(ref_text.strip(), encoding="utf-8")
    return {"ok": True, "voice_url": "/api/media/voice",
            "voice_revision": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "voice_ref_text": ref_text.strip(), "duration": info["duration"]}


class VoiceReferenceIn(BaseModel):
    ref_text: str
    voice_revision: str


@app.post("/api/voice/reference")
def save_voice_reference(body: VoiceReferenceIn, u=Depends(auth)):
    voice = find_user_voice(u["id"])
    if not voice:
        raise HTTPException(422, "请先上传声音样本")
    revision = hashlib.sha256(Path(voice).read_bytes()).hexdigest()
    if body.voice_revision != revision:
        raise HTTPException(409, "声音样本已改变，请刷新后核对录音原文")
    if not body.ref_text.strip() or len(body.ref_text) > 1000:
        raise HTTPException(422, "录音原文须为 1–1000 字符")
    ref_path = Path(voice).with_suffix(".txt")
    ref_path.write_text(body.ref_text.strip(), encoding="utf-8")
    return {"ok": True, "voice_ref_text": body.ref_text.strip(), "voice_revision": revision}


@app.post("/api/face")
async def upload_face(file: UploadFile = File(...), u=Depends(auth)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov"}:
        raise HTTPException(400, "当前口型服务需要上传 MP4 或 MOV 人物视频")
    dest = FACES_DIR / f"u{u['id']}{ext}"
    with tempfile.TemporaryDirectory(dir=FACES_DIR) as tmp:
        pending = Path(tmp) / ("face" + ext)
        _copy_upload_limited(file, pending, MAX_FACE_UPLOAD_BYTES)
        try:
            validate_face(pending)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc
        pending.replace(dest)
    for old in FACES_DIR.glob(f"u{u['id']}.*"):
        if old != dest:
            old.unlink()
    return {"ok": True, "face_url": "/api/media/face",
            "is_video": ext in {".mp4", ".mov"}}


# ---------- 公共配置 ----------
@app.get("/api/public")
def public():
    cfg = load_config()
    return {
        "brand": cfg["brand"],
        "voices": VOICES,
        "avatars": [{"id": t["id"], "name": t["name"],
                     "img": f"/api/avatars/{t['id']}"}
                    for t in compose.AVATAR_THEMES],
        "credits_per_video": cfg["credits_per_video"],
        "llm_ready": bool(cfg["llm"]["api_key"] and cfg["llm"]["base_url"]),
        "dh_ready": cfg["digital_human"]["provider"] != "none",
        "lipsync_ready": lipsync.available(),
        "voiceclone_ready": voiceclone.available(),
    }


# ---------- 任务 ----------
@app.post("/api/jobs")
def create_job(body: JobIn, idempotency_key: str = Header(default="", alias="Idempotency-Key"),
               u=Depends(auth)):
    cfg = load_config()
    _validate_job_lengths(body)
    params = body.dict()
    idempotency_key = idempotency_key.strip()
    request_fingerprint = hashlib.sha256(
        json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if len(idempotency_key) > 128:
        raise HTTPException(400, "Idempotency-Key 不能超过 128 个字符")
    if idempotency_key:
        existing = db.get_job_by_request(u["id"], idempotency_key)
        if existing:
            if existing.get("request_fingerprint") != request_fingerprint:
                raise HTTPException(409, "Idempotency-Key 已用于另一组输入")
            return {"job_id": existing["id"], "credits": db.get_credits(u["id"]), "replayed": True}
    try:
        face, voice_sample = pipeline.check_inputs(params, u["id"])
        lipsync.check_ready()
        if body.use_clone:
            voiceclone.check_ready()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    cost = cfg["credits_per_video"]
    job_id = uuid.uuid4().hex[:12]
    input_dir = OUTPUTS_DIR / job_id / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    face_copy = input_dir / ("face" + Path(face).suffix)
    shutil.copyfile(face, face_copy)
    params["_face_path"] = str(face_copy)
    if body.use_clone:
        voice_copy = input_dir / ("reference" + Path(voice_sample).suffix)
        shutil.copyfile(voice_sample, voice_copy)
        params["_voice_path"] = str(voice_copy)
        ref_file = Path(voice_sample).with_suffix(".txt")
        params["voice_ref_text"] = body.voice_ref_text.strip() or (
            ref_file.read_text(encoding="utf-8").strip() if ref_file.exists() else "")
    params["_cost"] = cost
    try:
        existing_id, created, credits = db.create_charged_job(
            job_id, u["id"], params, cost, idempotency_key, request_fingerprint
        )
    except db.InsufficientCredits:
        shutil.rmtree(OUTPUTS_DIR / job_id, ignore_errors=True)
        raise HTTPException(402, f"点数不足（需 {cost} 点），请充值")
    except db.IdempotencyConflict:
        shutil.rmtree(OUTPUTS_DIR / job_id, ignore_errors=True)
        raise HTTPException(409, "Idempotency-Key 已用于另一组输入")
    if created:
        _start_pipeline(job_id)
    elif job_id != existing_id:
        shutil.rmtree(OUTPUTS_DIR / job_id, ignore_errors=True)
    return {"job_id": existing_id, "credits": credits, "replayed": not created}


def _job_response(job):
    result = dict(job["result"])
    if result.get("audio_url"):
        result["audio_url"] = f"/api/media/job/{job['id']}/audio"
    if result.get("video_url"):
        result["video_url"] = f"/api/media/job/{job['id']}/video"
    return {"id": job["id"], "status": job["status"], "step": job["step"],
            "progress": job["progress"], "message": job["message"],
            "result": result, "steps": pipeline.STEPS,
            "params": {k: v for k, v in job["params"].items() if k in JobIn.model_fields},
            "can_resume": job["status"] == "waiting"}


@app.get("/api/jobs")
def job_history(u=Depends(auth)):
    return db.list_jobs(u["id"])


@app.get("/api/jobs/latest")
def latest_job(u=Depends(auth)):
    job = db.latest_job(u["id"])
    return _job_response(job) if job else None


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, u=Depends(auth)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != u["id"]:
        raise HTTPException(404, "任务不存在")
    return _job_response(job)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, u=Depends(auth)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != u["id"]:
        raise HTTPException(404, "任务不存在")
    if job["status"] != "waiting":
        raise HTTPException(409, "该任务无需恢复")
    _start_pipeline(job_id)
    return {"job_id": job_id}


# ---------- 后台（管理员）----------
_SECRET_FIELDS = (
    ("llm", "api_key"),
    ("digital_human", "api_key"),
    ("voice_clone", "api_key"),
    ("voice_clone", "dashscope_api_key"),
    ("lipsync", "api_key"),
    ("asr", "api_key"),
)


def _admin_config_view(cfg):
    view = copy.deepcopy(cfg)
    for section, field in _SECRET_FIELDS:
        if section in view:
            view.setdefault(section, {})[field] = ""
    return view


def _merge_admin_config(incoming):
    current = load_config()
    merged = copy.deepcopy(current)

    def merge(base, update):
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = value

    merge(merged, incoming)
    for section, field in _SECRET_FIELDS:
        if not isinstance(merged.get(section), dict):
            merged[section] = {}
        replacement = str(merged[section].get(field) or "").strip()
        if not replacement:
            merged[section][field] = current.get(section, {}).get(field, "")
    return merged


@app.get("/api/admin/config")
def admin_get_config(u=Depends(admin_only)):
    return _admin_config_view(load_config())


@app.post("/api/admin/config")
def admin_set_config(cfg: dict, u=Depends(admin_only)):
    return _admin_config_view(save_config(_merge_admin_config(cfg)))


@app.get("/api/admin/users")
def admin_users(u=Depends(admin_only)):
    return db.list_users()


class CreditIn(BaseModel):
    user_id: int
    delta: int


@app.post("/api/admin/credits")
def admin_credits(body: CreditIn, u=Depends(admin_only)):
    balance = db.add_credits(body.user_id, body.delta)
    return {"ok": True, "credits": balance}


@app.get("/api/admin/credits/ledger")
def admin_credit_ledger(user_id: Optional[int] = Query(default=None),
                        limit: int = Query(default=100, ge=1, le=500), u=Depends(admin_only)):
    return db.list_credit_ledger(user_id=user_id, limit=limit)


# ---------- 受保护媒体 ----------
def _safe_file(path: Path, root: Path):
    try:
        path = path.resolve()
        root = root.resolve()
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "媒体不存在") from exc
    if not path.is_file():
        raise HTTPException(404, "媒体不存在")
    return path


@app.get("/api/media/face")
def media_face(u=Depends(auth)):
    face = find_user_face(u["id"])
    if not face:
        raise HTTPException(404, "尚未上传口型底版")
    return FileResponse(_safe_file(Path(face), FACES_DIR))


@app.get("/api/media/voice")
def media_voice(u=Depends(auth)):
    voice = find_user_voice(u["id"])
    if not voice:
        raise HTTPException(404, "尚未上传声音样本")
    return FileResponse(_safe_file(Path(voice), VOICES_DIR), media_type="audio/wav")


@app.get("/api/media/job/{job_id}/{kind}")
def media_job(job_id: str, kind: str, u=Depends(auth)):
    if kind not in {"audio", "video"}:
        raise HTTPException(404, "媒体不存在")
    job = db.get_job(job_id)
    if not job or job["user_id"] != u["id"]:
        raise HTTPException(404, "媒体不存在")
    root = OUTPUTS_DIR / job_id
    path = root / ("voice.wav" if kind == "audio" else "final/video.mp4")
    return FileResponse(_safe_file(path, root),
                        media_type="audio/wav" if kind == "audio" else "video/mp4")


@app.get("/api/avatars/{avatar_id}")
def avatar(avatar_id: str):
    allowed = {t["id"] for t in compose.AVATAR_THEMES}
    if avatar_id not in allowed:
        raise HTTPException(404, "形象不存在")
    return FileResponse(_safe_file(OUTPUTS_DIR / "avatars" / f"{avatar_id}.png", OUTPUTS_DIR / "avatars"))


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"),
                        headers={"Cache-Control": "no-store"})


@app.get("/admin")
def admin_page():
    return FileResponse(str(FRONTEND_DIR / "admin.html"))


@app.get("/app.js")
def frontend_script():
    return FileResponse(str(FRONTEND_DIR / "app.js"), headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
