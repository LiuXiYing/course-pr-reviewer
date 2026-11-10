from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture, course_data

from course_pr_reviewer.exceptions import ReviewSystemError
from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.store import LocalDirStore
from course_pr_reviewer.models import Decision, Issue, ReasonCode, ReviewResult
from course_pr_reviewer.publisher import GitHubResultPublisher

HEAD = "a" * 40
MERGE_COMMIT = "b" * 40


class FakeClient:
    def __init__(self, pr: dict):
        self.pr = pr
        self.comments: list[str] = []
        self.merges: list[dict] = []
        self.closed = False

    def pull_request(self, repository, number):
        return self.pr

    def post_json(self, path, body):
        self.comments.append(body["body"])
        return {"id": 1}

    def put_json(self, path, body):
        self.merges.append(body)
        self.pr = {**self.pr, "merged": True, "state": "closed"}
        return {"merged": True, "sha": MERGE_COMMIT, "merged_at": "2026-11-24T09:31:00+08:00"}

    def patch_json(self, path, body):
        self.closed = True
        return {}


def open_pr(**overrides):
    values = {
        "merged": False,
        "state": "open",
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
    }
    values.update(overrides)
    return values


def result(*, decision=Decision.PASS, mode="tiered", issues=()):
    grading = {
        "mode": mode,
        "time_status": "LATE",
        "deadline": "2026-11-21T00:00:00+08:00",
        "effective_submission_at": "2026-11-24T00:00:00+08:00",
        "late_seconds": 259200,
        "late_human": "3 天",
        "grace_applied": False,
        "cap_percent": 75 if mode == "tiered" else None,
        "max_points": "10.00" if mode == "tiered" else None,
        "awarded_points": "7.50" if mode == "tiered" else None,
        "policy_version": "2026-fall-v1",
        "band_label": "48 小时以上，7 天以内" if mode == "tiered" else None,
        "details": {
            "deadline_local": "2026-11-21 00:00",
            "submitted_local": "2026-11-24 00:00",
        },
        "config_sha256": "c" * 64,
    }
    return ReviewResult(
        decision=decision,
        summary="审核结果摘要",
        issues=tuple(issues),
        metadata={
            "repository": "teacher/course",
            "pr_number": 7,
            "head_sha": HEAD,
            "assignment_id": "Lab1",
            "student_id": "2023010102",
            "github_login": "example-user",
            "grading": grading,
        },
    ).to_dict()


class PublisherReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.course = self.fixture.course()
        self.root = Path(tempfile.mkdtemp(prefix="publish-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def publisher(self, client, *, store=None, course=None, roster=None):
        return GitHubResultPublisher(
            client, merge_github=client, grade_store=self.store if store is None else store,
            roster=roster,
        )

    def test_receipt_is_finalized_after_merge(self):
        client = FakeClient(open_pr())
        outcome = self.publisher(client).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertTrue(outcome.merged)
        self.assertEqual(outcome.receipt_status, "FINALIZED")
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertEqual(record["record_status"], "FINALIZED")
        self.assertEqual(record["merge_commit_sha"], MERGE_COMMIT)
        self.assertEqual(record["awarded_points"], "7.50")
        self.assertEqual(client.merges[0]["sha"], HEAD)

    def test_reports_are_rebuilt_when_roster_is_available(self):
        self.course.data["grading"]["storage"]["export_on_finalize"] = True
        client = FakeClient(open_pr())
        self.publisher(client, roster=self.fixture.roster()).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertTrue((self.root / records.REPORTS["grades"]).is_file())
        self.assertIn("7.50", (self.root / records.REPORTS["grades"]).read_text())

    def test_comment_mentions_grading(self):
        client = FakeClient(open_pr())
        self.publisher(client).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        body = client.comments[0].replace("\\", "")
        self.assertIn("### 成绩记录", body)
        self.assertIn("7.50", body)

    def test_tiered_does_not_merge_when_receipt_cannot_be_written(self):
        class BrokenStore:
            branch = "grades"

            def write_json(self, *args, **kwargs):
                raise ReviewSystemError("写不进去")

        client = FakeClient(open_pr())
        with self.assertRaises(ReviewSystemError):
            self.publisher(client, store=BrokenStore()).publish(
                self.course, result(), expected_repository="teacher/course"
            )
        self.assertEqual(client.merges, [])

    def test_legacy_still_merges_when_receipt_write_fails(self):
        class BrokenStore:
            branch = "grades"

            def write_json(self, *args, **kwargs):
                raise ReviewSystemError("写不进去")

        legacy = GradingFixture(course_data(mode="legacy")).course()
        client = FakeClient(open_pr())
        outcome = self.publisher(client, store=BrokenStore()).publish(
            legacy, result(mode="legacy"), expected_repository="teacher/course"
        )
        self.assertTrue(outcome.merged)
        self.assertEqual(outcome.receipt_status, "FAILED")

    def test_failed_review_writes_no_receipt(self):
        client = FakeClient(open_pr())
        failed = result(
            decision=Decision.FAIL,
            issues=(Issue(code=ReasonCode.CONTENT_TOO_SHORT, message="太短"),),
        )
        outcome = self.publisher(client).publish(
            self.course, failed, expected_repository="teacher/course"
        )
        self.assertFalse(outcome.merged)
        self.assertIsNone(self.store.read_json(records.receipt_path("Lab1", "2023010102")))

    def test_no_auto_merge_leaves_pending_receipt(self):
        self.course.data["features"]["auto_merge"] = False
        client = FakeClient(open_pr())
        outcome = self.publisher(client).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertFalse(outcome.merged)
        self.assertEqual(outcome.receipt_status, "PENDING_ACCEPTANCE")
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertEqual(record["record_status"], "PENDING_ACCEPTANCE")

    def test_already_merged_pr_writes_nothing(self):
        client = FakeClient(open_pr(merged=True))
        outcome = self.publisher(client).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertEqual(outcome.skipped, "already_merged")
        self.assertIsNone(self.store.read_json(records.receipt_path("Lab1", "2023010102")))

    def test_head_change_skips_everything(self):
        client = FakeClient(open_pr(head={"sha": "d" * 40}))
        outcome = self.publisher(client).publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertEqual(outcome.skipped, "head_changed")
        self.assertEqual(client.comments, [])
        self.assertIsNone(self.store.read_json(records.receipt_path("Lab1", "2023010102")))

    def test_publishing_twice_does_not_duplicate_records(self):
        client = FakeClient(open_pr())
        publisher = self.publisher(client)
        publisher.publish(self.course, result(), expected_repository="teacher/course")
        second = publisher.publish(
            self.course, result(), expected_repository="teacher/course"
        )
        self.assertEqual(second.skipped, "already_merged")
        files = list((self.root / "receipts" / "Lab1").iterdir())
        self.assertEqual(len(files), 1)


if __name__ == "__main__":
    unittest.main()
