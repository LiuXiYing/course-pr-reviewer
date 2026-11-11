from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture, course_data

from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.backfill import backfill_completions
from course_pr_reviewer.grading.store import LocalDirStore
from course_pr_reviewer.grading.summary import semester_summary

STUDENT_DIR = "2023010102刘西莹"


class FakeTree:
    """按目录返回子条目，模拟 contents API。"""

    def __init__(self, layout: dict[str, list[str]]):
        self.layout = layout

    def default_branch(self, repository):
        return "main"

    def get_json(self, path, **kwargs):
        return {"object": {"sha": "b" * 40}}

    def list_dir(self, repository, path, ref):
        return sorted(self.layout.get(path, []))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture(course_data(mode="legacy"))
        self.course = self.fixture.course()
        self.roster = self.fixture.roster()
        self.root = Path(tempfile.mkdtemp(prefix="backfill-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def tree(self, labs=("Lab1",), extra=None):
        layout = {STUDENT_DIR: list(labs)}
        layout.update(extra or {})
        return FakeTree(layout)

    def run_backfill(self, *, labs=("Lab1",), dry_run=False, course=None):
        return backfill_completions(
            self.store,
            course or self.course,
            self.roster,
            self.tree(labs),
            "teacher/course",
            dry_run=dry_run,
        )

    def test_creates_receipts_for_existing_legacy_dirs(self):
        summary = self.run_backfill(labs=("Lab1",))
        self.assertEqual(summary["created"], 1)
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertTrue(record["backfilled"])
        self.assertEqual(record["record_status"], "FINALIZED")
        self.assertEqual(record["review_decision"], "BACKFILL")
        self.assertIsNone(record["awarded_points"])
        self.assertEqual(record["grading_mode"], "legacy")

    def test_is_idempotent(self):
        self.run_backfill(labs=("Lab1",))
        again = self.run_backfill(labs=("Lab1",))
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["existing"], 1)

    def test_skips_students_without_directory(self):
        summary = backfill_completions(
            self.store,
            self.course,
            self.roster,
            FakeTree({}),
            "teacher/course",
        )
        self.assertEqual(summary["created"], 0)
        self.assertIsNone(
            self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        )

    def test_tiered_assignments_are_never_backfilled(self):
        course = GradingFixture(course_data(mode="tiered")).course()
        summary = self.run_backfill(labs=("Lab1",), course=course)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["legacy_assignments"], 0)
        self.assertIsNone(
            self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        )

    def test_dry_run_writes_nothing(self):
        summary = self.run_backfill(labs=("Lab1",), dry_run=True)
        self.assertEqual(summary["created"], 1)
        self.assertIsNone(
            self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        )

    def test_summary_counts_backfilled_completions(self):
        self.run_backfill(labs=("Lab1",))
        summary = semester_summary(self.store, self.course, "2023010102")
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["graded"], 0)
        self.assertIsNone(summary["rate_percent"])

    def test_does_not_overwrite_a_real_receipt(self):
        path = records.receipt_path("Lab1", "2023010102")
        self.store.write_json(
            path,
            {
                "schema_version": 1,
                "course_id": "test-course",
                "assignment_id": "Lab1",
                "student_id": "2023010102",
                "pr_number": 9,
                "reviewed_head_sha": "a" * 40,
                "review_decision": "PASS",
                "record_status": "FINALIZED",
                "grading_mode": "tiered",
                "policy_version": "2026-fall-v1",
                "awarded_points": "9.00",
            },
            "seed",
        )
        summary = self.run_backfill(labs=("Lab1",))
        self.assertEqual(summary["created"], 0)
        self.assertEqual(self.store.read_json(path)["awarded_points"], "9.00")


if __name__ == "__main__":
    unittest.main()
