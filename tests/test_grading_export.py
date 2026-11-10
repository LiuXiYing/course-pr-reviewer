from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture, course_data

from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.export import (
    build_grades_csv,
    build_submissions_csv,
    effective_points,
    load_overrides,
    load_receipts,
    rebuild_reports,
    student_status,
)
from course_pr_reviewer.grading.store import LocalDirStore

NOW = dt.datetime(2027, 1, 1, tzinfo=dt.UTC)


def grading_block(
    *,
    mode="tiered",
    awarded="7.50",
    cap=75,
    time_status="LATE",
    deadline="2026-11-21T00:00:00+08:00",
    submitted="2026-11-24T00:00:00+08:00",
):
    return {
        "mode": mode,
        "deadline": deadline,
        "effective_submission_at": submitted,
        "time_status": time_status,
        "late_seconds": 259200,
        "grace_applied": False,
        "cap_percent": cap,
        "max_points": None if mode == "legacy" else "10.00",
        "awarded_points": awarded,
        "policy_version": "2026-fall-v1",
    }


def receipt(
    *,
    assignment="Lab1",
    student="2023010102",
    login="example-user",
    mode="tiered",
    awarded="7.50",
    status="FINALIZED",
    time_status="LATE",
    pr=7,
):
    return records.build_receipt(
        course_id="test-course",
        assignment_id=assignment,
        student_id=student,
        github_login=login,
        pr_number=pr,
        reviewed_head_sha="a" * 40,
        grading=grading_block(
            mode=mode, awarded=awarded, time_status=time_status
        ),
        record_status=status,
        merged=status == "FINALIZED",
        merge_commit_sha="b" * 40 if status == "FINALIZED" else None,
        accepted_at="2026-11-24T09:31:00+08:00" if status == "FINALIZED" else None,
    )


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.course = self.fixture.course()
        self.roster = self.fixture.roster()
        self.root = Path(tempfile.mkdtemp(prefix="reports-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def rows(self, text: str) -> list[list[str]]:
        import csv
        import io

        return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))

    def test_grades_csv_marks_unscored_and_not_submitted_differently(self):
        receipts = {("Lab1", "2023010102"): receipt()}
        text = build_grades_csv(self.course, self.roster, receipts, {}, now=NOW)
        rows = self.rows(text)
        self.assertEqual(rows[0][:2], ["student_id", "github_login"])
        self.assertIn("Lab1_status", rows[0])
        student_row = next(row for row in rows[1:] if row[0] == "2023010102")
        other_row = next(row for row in rows[1:] if row[0] == "2023010103")
        self.assertEqual(student_row[2], "SCORED")
        self.assertEqual(student_row[3], "7.50")
        self.assertEqual(other_row[2], "NOT_SUBMITTED")
        self.assertEqual(other_row[3], "")

    def test_legacy_completion_has_no_points(self):
        receipts = {
            ("Lab1", "2023010102"): receipt(
                mode="legacy", awarded=None, time_status="LATE"
            )
        }
        text = build_grades_csv(self.course, self.roster, receipts, {}, now=NOW)
        row = next(
            item for item in self.rows(text)[1:] if item[0] == "2023010102"
        )
        self.assertEqual(row[2], "COMPLETED_UNGRADED")
        self.assertEqual(row[3], "")
        self.assertEqual(row[-3], "0.00")  # graded_points_total
        self.assertEqual(row[-2], "1")  # completion_count

    def test_null_is_not_written_as_zero(self):
        receipts = {("Lab1", "2023010102"): receipt(mode="legacy", awarded=None)}
        text = build_grades_csv(self.course, self.roster, receipts, {}, now=NOW)
        row = next(item for item in self.rows(text)[1:] if item[0] == "2023010102")
        self.assertEqual(row[3], "")
        self.assertNotEqual(row[3], "0")

    def test_not_due_assignment_is_not_counted_as_missing(self):
        course = GradingFixture(
            course_data(deadline="2027-06-01T00:00:00+08:00")
        ).course()
        text = build_grades_csv(course, self.roster, {}, {}, now=NOW)
        row = next(item for item in self.rows(text)[1:] if item[0] == "2023010102")
        self.assertEqual(row[2], "NOT_DUE")
        self.assertEqual(row[-1], "0")  # missing_count

    def test_pending_receipts_are_not_scored(self):
        receipts = {("Lab1", "2023010102"): receipt(status="PENDING_ACCEPTANCE")}
        text = build_grades_csv(self.course, self.roster, receipts, {}, now=NOW)
        row = next(item for item in self.rows(text)[1:] if item[0] == "2023010102")
        self.assertEqual(row[2], "PENDING")
        self.assertEqual(row[-3], "0.00")

    def test_override_layer_changes_export_only(self):
        receipts = {("Lab1", "2023010102"): receipt()}
        overrides = {
            ("Lab1", "2023010102"): {
                "assignment_id": "Lab1",
                "student_id": "2023010102",
                "approved_by": "teacher",
                "reason_code": "SYSTEM_ERROR",
                "new_points": "10.00",
            }
        }
        self.assertEqual(effective_points(receipts[("Lab1", "2023010102")], overrides[("Lab1", "2023010102")]), "10.00")
        text = build_grades_csv(self.course, self.roster, receipts, overrides, now=NOW)
        row = next(item for item in self.rows(text)[1:] if item[0] == "2023010102")
        self.assertEqual(row[3], "10.00")
        self.assertEqual(receipts[("Lab1", "2023010102")]["awarded_points"], "7.50")

    def test_submissions_csv_columns(self):
        receipts = {("Lab1", "2023010102"): receipt()}
        text = build_submissions_csv(self.course, receipts, {})
        rows = self.rows(text)
        self.assertEqual(list(rows[0]), list(__import__("course_pr_reviewer.grading.export", fromlist=["SUBMISSION_COLUMNS"]).SUBMISSION_COLUMNS))
        self.assertEqual(rows[1][1], "2023010102")
        self.assertEqual(rows[1][13], "7.50")

    def test_csv_is_utf8_bom_encoded(self):
        text = build_submissions_csv(self.course, {}, {})
        self.assertTrue(text.startswith("\ufeff"))

    def test_invalid_receipt_is_reported_not_ignored(self):
        path = self.root / "receipts/Lab1/bad.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        receipts, errors = load_receipts(self.store, self.course)
        self.assertEqual(receipts, {})
        self.assertEqual(len(errors), 1)

    def test_invalid_override_is_reported(self):
        path = self.root / "overrides/Lab1/bad.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"new_points": "10"}), encoding="utf-8")
        overrides, errors = load_overrides(self.store, self.course)
        self.assertEqual(overrides, {})
        self.assertEqual(len(errors), 1)

    def test_rebuild_is_reproducible(self):
        self.store.write_json(
            records.receipt_path("Lab1", "2023010102"), receipt(), "seed"
        )
        first = rebuild_reports(self.store, self.course, self.roster, now=NOW)
        payloads = {
            name: (self.root / records.REPORTS[name]).read_text(encoding="utf-8")
            for name in ("grades", "submissions", "pending")
        }
        for name in ("grades", "submissions", "pending"):
            (self.root / records.REPORTS[name]).unlink()
        second = rebuild_reports(self.store, self.course, self.roster, now=NOW)
        self.assertEqual(first["receipts"], second["receipts"])
        for name, text in payloads.items():
            self.assertEqual((self.root / records.REPORTS[name]).read_text(encoding="utf-8"), text)

    def test_summary_counts(self):
        self.store.write_json(
            records.receipt_path("Lab1", "2023010102"), receipt(), "seed"
        )
        result = rebuild_reports(self.store, self.course, self.roster, now=NOW)
        per_assignment = result["summary"]["assignments"]["Lab1"]
        self.assertEqual(per_assignment["mode"], "tiered")
        self.assertEqual(per_assignment["finalized"], 1)
        self.assertEqual(per_assignment["scored"], 1)

    def test_student_status_helper(self):
        self.assertEqual(student_status(None, due=True), "NOT_SUBMITTED")
        self.assertEqual(student_status(None, due=False), "NOT_DUE")
        self.assertEqual(
            student_status({"record_status": "PENDING_ACCEPTANCE"}, due=True), "PENDING"
        )
        self.assertEqual(
            student_status({"record_status": "FINALIZED", "awarded_points": None}, due=True),
            "COMPLETED_UNGRADED",
        )


if __name__ == "__main__":
    unittest.main()
