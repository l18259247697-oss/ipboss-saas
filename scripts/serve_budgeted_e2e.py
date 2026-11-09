"""Run the unchanged web pipeline with a one-job, 12 CNY validation ceiling.

This is a local E2E harness, not product billing. It does not mock providers,
edit scripts/media, retry a failed job, or manufacture a successful result.
"""
import fcntl
import argparse
import hashlib
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class BudgetStopped(RuntimeError):
    pass


class SingleJobBudget:
    def __init__(self, path, baseline_ids=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.edit() as state:
            if not state:
                state.update(limit_cny=12, earlier_stages_reserved_cny=1,
                             allowed_user_id=3, baseline_job_ids=list(baseline_ids),
                             job_id=None, created_at=time.time(), status="ready")
            if state.get("limit_cny") != 12 or state.get("allowed_user_id") != 3:
                raise BudgetStopped("验收授权记录不匹配，未扩大预算")

    @contextmanager
    def edit(self):
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            yield state
            pending = self.path.with_suffix(".tmp")
            pending.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            pending.replace(self.path)

    def bind(self, job_id, user_id, identity):
        with self.edit() as state:
            if user_id != state["allowed_user_id"] or job_id in state["baseline_job_ids"]:
                raise BudgetStopped("本轮预算仅用于指定验收账号的一条新任务")
            if state["job_id"] not in {None, job_id}:
                raise BudgetStopped("本轮预算已绑定另一条任务，未启动新的付费流程")
            if state.get("identity") not in {None, identity}:
                raise BudgetStopped("本轮任务输入或模型账号已改变，未重复执行")
            state.update(job_id=job_id, identity=identity, status="bound")

    def check_audio(self, job_id, duration, audio_sha256):
        if not math.isfinite(duration) or duration <= 2:
            raise BudgetStopped("配音时长异常，未提交口型")
        parts = math.ceil(duration / 110) if duration >= 120 else 1
        # Include all context and conservative per-request rounding overhead.
        billed_seconds = Decimal(str(duration)) + Decimal("0.5") * (parts - 1) + Decimal("0.1") * parts
        lip_cost = billed_seconds * Decimal("0.08")
        with self.edit() as state:
            if state["job_id"] != job_id:
                raise BudgetStopped("口型任务与预算记录不符")
            if state.get("audio_sha256") not in {None, audio_sha256}:
                raise BudgetStopped("本轮配音发生变化，未自动重复生成口型")
            planned = lip_cost + Decimal(str(state["earlier_stages_reserved_cny"]))
            state.update(audio_duration=duration, audio_sha256=audio_sha256,
                         planned_parts=parts, lipsync_reserved_cny=float(lip_cost),
                         total_reserved_cny=float(planned), status="allowed" if planned <= 12 else "budget_stopped")
        if planned > 12:
            raise BudgetStopped("完整配音预计超出本轮12元预算，已保留配音；未缩稿、加速或提交口型")

    def replace_rejected_asr(self, job, asr, has_downstream_artifacts):
        # One explicit replacement after the confirmed first-stage 404, never an
        # unknown request or a paid TTS/lipsync retry. Keep the same 12 CNY ledger.
        with self.edit() as state:
            if (state.get("retired_jobs") or state.get("audio_sha256")
                    or state["job_id"] != job["id"] or job["user_id"] != 3
                    or job["status"] != "failed" or job["step"] != 1
                    or job.get("result") or has_downstream_artifacts
                    or asr.get("status") != "rejected" or asr.get("http_status") != 404):
                raise BudgetStopped("不满足首步明确拒绝的单次替换条件，未释放预算绑定")
            state["retired_jobs"] = [{"job_id": job["id"], "reason": "ASR HTTP 404",
                                      "asr_state": asr, "replaced_at": time.time()}]
            state["baseline_job_ids"].append(job["id"])
            state.pop("product_status", None)
            state.pop("last_finished_at", None)
            state.update(job_id=None, identity=None, status="ready_after_asr_fix")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replace-rejected-asr")
    args = parser.parse_args()
    os.environ.update(APP_DATA_DIR=str(ROOT / "data/e2e-20260928"),
                      APP_OUTPUTS_DIR=str(ROOT / "outputs/e2e-20260928"),
                      APP_CONFIG_FILE=str(ROOT / "data/config.json"),
                      OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
    sys.path.insert(0, str(ROOT / "backend"))
    import app
    import db
    import pipeline
    import uvicorn
    from config import load_config
    from services import media
    from services.videoretalk import PendingTask

    baseline = [j["id"] for u in db.list_users() for j in db.list_jobs(u["id"])]
    budget = SingleJobBudget(ROOT / "data/e2e-20260928/full-e2e-20261014/budget.json", baseline)
    if args.replace_rejected_asr:
        job = db.get_job(args.replace_rejected_asr)
        folder = ROOT / "outputs/e2e-20260928" / job["id"]
        asr = json.loads((folder / "prep/source-asr.json").read_text())
        downstream = any(p.name not in {"inputs", "prep"} for p in folder.iterdir())
        budget.replace_rejected_asr(job, asr, downstream)
    original_run, original_lipsync = pipeline.run_pipeline, pipeline.lipsync.lipsync

    def guarded_run(job_id):
        job = db.get_job(job_id)
        if not job or job["status"] in {"done", "failed"}:
            return
        cfg, p = load_config(), job["params"]
        try:
            if (cfg["lipsync"]["provider"] != "dashscope"
                    or cfg["voice_clone"]["provider"] != "dashscope"
                    or cfg["voice_clone"]["model"] != "qwen-audio-3.0-tts-plus"
                    or cfg["asr"]["provider"] != "dashscope"
                    or cfg["asr"]["model"] != "qwen-audio-3.0-asr-flash"
                    or cfg["llm"]["model"] != "deepseek-flash"
                    or urlsplit(cfg["llm"].get("base_url", "")).hostname != "api.deepseek.com"
                    or not p.get("use_clone")
                    or len(p.get("extra", "")) > 4000 or len(p.get("industry", "")) > 200):
                raise BudgetStopped("验收范围或模型已改变，未启动付费流程")
            identity = hashlib.sha256(json.dumps({"params": p, "config": cfg}, sort_keys=True).encode()).hexdigest()
            budget.bind(job_id, job["user_id"], identity)
        except BudgetStopped as exc:
            db.update_job(job_id, status="waiting", message=str(exc))
            return
        original_run(job_id)
        with budget.edit() as state:
            state["product_status"] = db.get_job(job_id)["status"]
            state["last_finished_at"] = time.time()

    def guarded_lipsync(face, audio, output, *args, **kwargs):
        try:
            budget.check_audio(Path(output).parent.parent.name, media.probe(audio, "audio")["duration"],
                               hashlib.sha256(Path(audio).read_bytes()).hexdigest())
        except BudgetStopped as exc:
            raise PendingTask(str(exc)) from exc
        return original_lipsync(face, audio, output, *args, **kwargs)

    pipeline.run_pipeline = guarded_run
    pipeline.lipsync.lipsync = guarded_lipsync
    uvicorn.run(app.app, host="127.0.0.1", port=8077, workers=1)


if __name__ == "__main__":
    main()
