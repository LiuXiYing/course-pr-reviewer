from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture
from test_grading_export import receipt

from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.reconcile import reconcile
from course_pr_reviewer.grading.store import LocalDirStore

HEAD = "a" * 40


class FakePRSource:
    def __init__(self, prs: dict[int, dict]):
        self.prs = prs
        self.calls = 0

    def pull_request(self, repository, number):
        self.calls += 1
        return self.prs[number]


def pr(
    *,
    merged=False,
    state="open",
    head_sha=HEAD,
    merge_commit_sha="b" * 40,
    merged_at="2026-11-24T09:31:00+08:00",
):
    return {
        "merged": merged,
        "state": state,
        "head": {"sha": head_sha},
        "base": {"ref": "main"},
        "merge_commit_sha": merge_commit_sha,
        "merged_at": merged_at,
    }


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.course = self.fixture.course()
        self.roster = self.fixture.roster()
        self.root = Path(tempfile.mkdtemp(prefix="reconcile-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def seed(self, record):
        self.store.write_json(
            records.receipt_path(record["assignment_id"], record["student_id"]),
            record,
            "seed",
        )

    def reconcile(self, prs):
        return reconcile(
            self.store,
            self.course,
            self.roster,
            FakePRSource(prs),
            repository="teacher/course",
        )

    def test_pending_becomes_finalized_after_real_merge(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        report = self.reconcile({7: pr(merged=True, state="closed")})
        self.assertEqual(report["finalized"], 1)
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertEqual(record["record_status"], "FINALIZED")
        self.assertTrue(record["merged"])
        self.assertEqual(record["merge_commit_sha"], "b" * 40)
        self.assertEqual(record["accepted_at"], "2026-11-24T09:31:00+08:00")
        self.assertTrue(any(p.name.endswith(".json") for p in (self.root / "audit").rglob("*")))

    def test_open_pr_stays_pending(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        report = self.reconcile({7: pr()})
        self.assertEqual(report["finalized"], 0)
        self.assertEqual(report["pending"][0]["reason"], "PR 仍未合并")
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertEqual(record["record_status"], "PENDING_ACCEPTANCE")
        self.assertTrue((self.root / records.REPORTS["reconcile"]).is_file())

    def test_closed_without_merge_stays_pending(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        report = self.reconcile({7: pr(state="closed")})
        self.assertEqual(report["finalized"], 0)
        self.assertEqual(report["pending"][0]["reason"], "PR 已关闭但未合并")

    def test_changed_head_is_not_finalized(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        report = self.reconcile({7: pr(merged=True, state="closed", head_sha="c" * 40)})
        self.assertEqual(report["finalized"], 0)
        self.assertEqual(report["pending"][0]["reason"], "PR 当前 head 与已审核 head 不一致")

    def test_finalized_records_are_never_touched(self):
        self.seed(receipt(status="FINALIZED"))
        source = FakePRSource({7: pr(merged=True, state="closed")})
        report = reconcile(
            self.store, self.course, self.roster, source, repository="teacher/course"
        )
        self.assertEqual(report["checked"], 0)
        self.assertEqual(source.calls, 0)

    def test_only_pr_filter(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE", pr=7))
        source = FakePRSource({7: pr(merged=True, state="closed"), 9: pr()})
        report = reconcile(
            self.store, self.course, self.roster, source,
            repository="teacher/course", only_pr=9,
        )
        self.assertEqual(report["checked"], 0)

    def test_since_filter_skips_old_records(self):
        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        source = FakePRSource({7: pr()})
        report = reconcile(
            self.store, self.course, self.roster, source,
            repository="teacher/course",
            since=dt.datetime(2027, 1, 1, tzinfo=dt.UTC),
        )
        self.assertEqual(report["checked"], 0)
        self.assertEqual(source.calls, 0)

    def test_github_failure_is_reported_not_fatal(self):
        class Broken:
            def pull_request(self, repository, number):
                raise RuntimeError("HTTP 502")

        self.seed(receipt(status="PENDING_ACCEPTANCE"))
        report = reconcile(
            self.store, self.course, self.roster, Broken(), repository="teacher/course"
        )
        self.assertEqual(report["finalized"], 0)
        self.assertIn("无法读取 PR", report["anomalies"][0]["error"])


if __name__ == "__main__":
    unittest.main()
