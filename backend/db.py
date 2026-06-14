"""极简 SQLite 数据层：用户 / 点数 / 任务。
   并发：WAL 模式 + busy_timeout，后台流水线线程与前端轮询同时读写不撞锁。"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent.parent / "data" / "app.db"


def _conn():
    c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    with closing(_conn()) as c, c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pwd_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                token TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,          -- queued/running/done/failed
                step INTEGER NOT NULL DEFAULT 0,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                params TEXT,                   -- json: link/industry/voice/mode/avatar
                result TEXT,                   -- json: transcript/rewritten/audio_url/video_url...
                created_at INTEGER NOT NULL
            );
            """
        )


# ---------- 密码 ----------
def _hash(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 120000).hex()


def create_user(username, password, credits=0, is_admin=0):
    salt = secrets.token_hex(8)
    with closing(_conn()) as c, c:
        cur = c.execute(
            "INSERT INTO users(username,pwd_hash,salt,credits,is_admin,created_at) VALUES(?,?,?,?,?,?)",
            (username, _hash(password, salt), salt, credits, is_admin, int(time.time())),
        )
        return cur.lastrowid


def verify_login(username, password):
    with closing(_conn()) as c, c:
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u or _hash(password, u["salt"]) != u["pwd_hash"]:
            return None
        token = secrets.token_urlsafe(24)
        c.execute("UPDATE users SET token=? WHERE id=?", (token, u["id"]))
        return token


def user_by_token(token):
    if not token:
        return None
    with closing(_conn()) as c, c:
        return c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()


def user_by_name(username):
    with closing(_conn()) as c, c:
        return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def add_credits(user_id, delta):
    with closing(_conn()) as c, c:
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (delta, user_id))


def get_credits(user_id):
    with closing(_conn()) as c, c:
        r = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        return r["credits"] if r else 0


def list_users():
    with closing(_conn()) as c, c:
        return [dict(r) for r in c.execute(
            "SELECT id,username,credits,is_admin,created_at FROM users ORDER BY id DESC"
        ).fetchall()]


# ---------- 任务 ----------
def create_job(job_id, user_id, params):
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO jobs(id,user_id,status,step,progress,message,params,result,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, user_id, "queued", 0, 0, "排队中", json.dumps(params, ensure_ascii=False),
             "{}", int(time.time())),
        )


def update_job(job_id, **fields):
    if not fields:
        return
    if "result" in fields and isinstance(fields["result"], dict):
        fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
    cols = ",".join(f"{k}=?" for k in fields)
    with closing(_conn()) as c, c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id):
    with closing(_conn()) as c, c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["params"] = json.loads(d.get("params") or "{}")
        d["result"] = json.loads(d.get("result") or "{}")
        return d
