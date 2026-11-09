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
from config import DATA_DIR

DB_FILE = DATA_DIR / "app.db"


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
                request_id TEXT,
                request_fingerprint TEXT,
                status TEXT NOT NULL,          -- queued/running/done/failed
                step INTEGER NOT NULL DEFAULT 0,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                params TEXT,                   -- json: link/industry/voice/mode/avatar
                result TEXT,                   -- json: transcript/rewritten/audio_url/video_url...
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credit_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                delta INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                job_id TEXT,
                request_id TEXT,
                note TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        # Keep existing local databases usable while adding the server-side
        # idempotency key used by paid job creation.
        columns = {row[1] for row in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "request_id" not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN request_id TEXT")
        if "request_fingerprint" not in columns:
            c.execute("ALTER TABLE jobs ADD COLUMN request_fingerprint TEXT")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_user_request "
            "ON jobs(user_id, request_id) "
            "WHERE request_id IS NOT NULL AND request_id <> ''"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON credit_ledger(user_id, id DESC)")


# ---------- 密码 ----------
def _hash(pwd, salt):
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 120000).hex()


def create_user(username, password, credits=0, is_admin=0):
    salt = secrets.token_hex(8)
    with closing(_conn()) as c, c:
        now = int(time.time())
        cur = c.execute(
            "INSERT INTO users(username,pwd_hash,salt,credits,is_admin,created_at) VALUES(?,?,?,?,?,?)",
            (username, _hash(password, salt), salt, credits, is_admin, now),
        )
        if credits:
            _insert_ledger(c, cur.lastrowid, "register_bonus", credits, credits,
                           note="注册赠送点数")
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


def _insert_ledger(c, user_id, kind, delta, balance_after, *, job_id=None,
                   request_id=None, note=""):
    c.execute(
        "INSERT INTO credit_ledger(user_id,kind,delta,balance_after,job_id,request_id,note,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id, kind, delta, balance_after, job_id, request_id, note, int(time.time())),
    )


def add_credits(user_id, delta, *, kind="admin_adjustment", note="管理员调整"):
    with closing(_conn()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("用户不存在")
        balance = row["credits"] + delta
        c.execute("UPDATE users SET credits=? WHERE id=?", (balance, user_id))
        if delta:
            _insert_ledger(c, user_id, kind, delta, balance, note=note)
        return balance


def refund_job_once(job_id, user_id, amount):
    """Refund a failed job once, including after an explicit resume."""
    if amount <= 0:
        return False
    with closing(_conn()) as c, c:
        # Lock the short refund transaction before reading the marker so two
        # recovery workers cannot both observe an unrefunded job.
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT result FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)).fetchone()
        if not row:
            return False
        result = json.loads(row["result"] or "{}")
        if result.get("credits_refunded"):
            return False
        result["credits_refunded"] = True
        c.execute("UPDATE jobs SET result=? WHERE id=? AND user_id=?",
                  (json.dumps(result, ensure_ascii=False), job_id, user_id))
        c.execute("UPDATE users SET credits=credits+? WHERE id=?", (amount, user_id))
        balance = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()["credits"]
        _insert_ledger(c, user_id, "job_refund", amount, balance, job_id=job_id,
                       note="任务失败退款")
        return True


def get_credits(user_id):
    with closing(_conn()) as c, c:
        r = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        return r["credits"] if r else 0


def list_users():
    with closing(_conn()) as c, c:
        return [dict(r) for r in c.execute(
            "SELECT id,username,credits,is_admin,created_at FROM users ORDER BY id DESC"
        ).fetchall()]


def list_credit_ledger(user_id=None, limit=100):
    limit = max(1, min(int(limit), 500))
    with closing(_conn()) as c:
        if user_id is None:
            rows = c.execute(
                "SELECT l.*,u.username FROM credit_ledger l JOIN users u ON u.id=l.user_id "
                "ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = c.execute(
                "SELECT l.*,u.username FROM credit_ledger l JOIN users u ON u.id=l.user_id "
                "WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(row) for row in rows]


# ---------- 任务 ----------
def create_job(job_id, user_id, params, request_id="", request_fingerprint=""):
    with closing(_conn()) as c, c:
        c.execute(
            "INSERT INTO jobs(id,user_id,request_id,request_fingerprint,status,step,progress,message,params,result,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, user_id, request_id or None, request_fingerprint or None, "queued", 0, 0, "排队中",
             json.dumps(params, ensure_ascii=False), "{}", int(time.time())),
        )


class InsufficientCredits(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


def get_job_by_request(user_id, request_id):
    request_id = (request_id or "").strip()
    if not request_id:
        return None
    with closing(_conn()) as c:
        row = c.execute(
            "SELECT id,request_fingerprint FROM jobs WHERE user_id=? AND request_id=?",
            (user_id, request_id),
        ).fetchone()
    return dict(row) if row else None


def create_charged_job(job_id, user_id, params, cost, request_id="", request_fingerprint=""):
    """Atomically charge credits and create a job.

    Returns (job_id, created, remaining_credits). A repeated non-empty
    request_id for the same account returns the original job without charging
    or inserting another row.
    """
    if cost < 0:
        raise ValueError("job cost cannot be negative")
    request_id = (request_id or "").strip()
    with closing(_conn()) as c, c:
        c.execute("BEGIN IMMEDIATE")
        if request_id:
            existing = c.execute(
                "SELECT id,request_fingerprint FROM jobs WHERE user_id=? AND request_id=?",
                (user_id, request_id),
            ).fetchone()
            if existing:
                if (existing["request_fingerprint"] or "") != (request_fingerprint or ""):
                    raise IdempotencyConflict
                credits = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
                return existing["id"], False, (credits["credits"] if credits else 0)
        user = c.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["credits"] < cost:
            raise InsufficientCredits
        c.execute("UPDATE users SET credits=credits-? WHERE id=?", (cost, user_id))
        balance = user["credits"] - cost
        _insert_ledger(c, user_id, "job_charge", -cost, balance, job_id=job_id,
                       request_id=request_id, note="任务扣点")
        c.execute(
            "INSERT INTO jobs(id,user_id,request_id,request_fingerprint,status,step,progress,message,params,result,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, user_id, request_id or None, request_fingerprint or None, "queued", 0, 0, "排队中",
             json.dumps(params, ensure_ascii=False), "{}", int(time.time())),
        )
        return job_id, True, balance


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


def latest_job(user_id):
    with closing(_conn()) as c:
        row = c.execute("SELECT id FROM jobs WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                        (user_id,)).fetchone()
    return get_job(row["id"]) if row else None


def list_jobs(user_id):
    with closing(_conn()) as c:
        rows = c.execute("SELECT id,status,created_at,params,result FROM jobs WHERE user_id=? "
                         "ORDER BY created_at DESC,rowid DESC", (user_id,)).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["result"] or "{}")
        result.append({"id": row["id"], "status": row["status"], "created_at": row["created_at"],
                       "industry": json.loads(row["params"] or "{}").get("industry", ""),
                       "quality_review": payload.get("quality_review"),
                       "media_checks_passed": bool(payload.get("media_checks_passed"))})
    return result


def claim_job(job_id):
    with closing(_conn()) as c, c:
        updated = c.execute("UPDATE jobs SET status='running' WHERE id=? AND status IN ('queued','waiting')",
                            (job_id,))
        return updated.rowcount == 1


def recover_interrupted_jobs():
    """Single-worker startup only: require an explicit resume, never replay writes."""
    with closing(_conn()) as c, c:
        c.execute("UPDATE jobs SET status='waiting', message=? WHERE status IN ('queued','running')",
                  ("服务已重启，任务进度已保留；请继续查询原任务，不要重复创建",))
