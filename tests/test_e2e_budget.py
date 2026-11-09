"""Local validation budget tests; no network requests or production billing."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from serve_budgeted_e2e import BudgetStopped, SingleJobBudget


class E2EBudgetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "budget.json"
        self.budget = SingleJobBudget(self.path, ["old-job"])

    def test_old_job_and_other_account_cannot_consume_new_budget(self):
        for job, user in [("old-job", 3), ("new-job", 4)]:
            with self.assertRaises(BudgetStopped):
                self.budget.bind(job, user, "input-hash")
        self.assertIsNone(json.loads(self.path.read_text())["job_id"])

    def test_restart_preserves_single_job_and_audio_identity(self):
        self.budget.bind("new-job", 3, "input-hash")
        self.budget.check_audio("new-job", 134.32, "audio-hash")
        restored = SingleJobBudget(self.path)
        restored.bind("new-job", 3, "input-hash")
        restored.check_audio("new-job", 134.32, "audio-hash")
        with self.assertRaises(BudgetStopped):
            restored.bind("another-job", 3, "input-hash")
        with self.assertRaises(BudgetStopped):
            restored.check_audio("new-job", 134.32, "changed-audio")
        self.assertLess(json.loads(self.path.read_text())["total_reserved_cny"], 12)

    def test_over_budget_stops_and_records_full_duration_without_clipping(self):
        self.budget.bind("new-job", 3, "input-hash")
        with self.assertRaisesRegex(BudgetStopped, "超出"):
            self.budget.check_audio("new-job", 180, "audio-hash")
        state = json.loads(self.path.read_text())
        self.assertEqual(state["audio_duration"], 180)
        self.assertEqual(state["status"], "budget_stopped")
        self.assertGreater(state["total_reserved_cny"], 12)

    def test_confirmed_asr_rejection_replacement_keeps_budget_and_audit(self):
        self.budget.bind("failed-job", 3, "input-hash")
        job = {"id": "failed-job", "user_id": 3, "status": "failed", "step": 1, "result": {}}
        asr = {"status": "rejected", "http_status": 404}
        for unsafe in [{"status": "submitting"}, {"status": "rejected", "http_status": 500}]:
            with self.assertRaises(BudgetStopped):
                self.budget.replace_rejected_asr(job, unsafe, False)
        with self.assertRaises(BudgetStopped):
            self.budget.replace_rejected_asr(job, asr, True)
        self.budget.replace_rejected_asr(job, asr, False)
        state = json.loads(self.path.read_text())
        self.assertEqual(state["limit_cny"], 12)
        self.assertEqual(state["earlier_stages_reserved_cny"], 1)
        self.assertEqual(state["retired_jobs"][0]["job_id"], "failed-job")
        self.budget.bind("replacement-job", 3, "fixed-input")
        with self.assertRaises(BudgetStopped):
            self.budget.bind("failed-job", 3, "input-hash")
        with self.assertRaises(BudgetStopped):
            self.budget.replace_rejected_asr(dict(job, id="replacement-job"), asr, False)


if __name__ == "__main__":
    unittest.main()
