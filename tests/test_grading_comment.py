from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture, course_data
from test_grading_export import receipt
from test_grading_publisher import HEAD, MERGE_COMMIT, open_pr

from course_pr_reviewer.exceptions import ReviewSystemError
from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.store import LocalDirStore
from course_pr_reviewer.grading.summary import semester_summary
from course_pr_reviewer.models import Decision, ReviewResult
from course_pr_reviewer.publisher import GitHubResultPublisher, render_comment

NOW = dt.datetime(2027, 1, 1, tzinfo=dt.UTC)


class CommentClient:
    """记录评论创建与更新顺序的 GitHub 替身。"""

    def __init__(self, pr: dict, *, fail_update: bool = False):
        self.pr = pr
        self.fail_update = fail_update
        self.comments: list[str] = []
        self.updates: list[tuple[int, str]] = []
        self.merges = 0
        self.closed = False

    def pull_request(self, repository, number):
        return self.pr

    def post_json(self, path, body):
        self.comments.append(body["body"])
        return {"id": 4242}

    def patch_json(self, path, body):
        if "/issues/comments/" in path:
            if self.fail_update:
                raise ReviewSystemError("HTTP 403")
            self.updates.append((int(path.rsplit("/", 1)[-1]), body["body"]))
            return {"id": 4242}
        self.closed = True
        return {}

    def put_json(self, path, body):
        self.merges += 1
        self.pr = {**self.pr, "merged": True, "state": "closed"}
        return {
            "merged": True,
            "sha": MERGE_COMMIT,
            "merged_at": "2026-11-24T09:31:00+08:00",
        }


def result_for(*, mode="tiered", awarded="7.50", assignment="Lab1"):
    grading = {
        "mode": mode,
        "time_status": "LATE" if mode == "tiered" else "ON_TIME",
        "deadline": "2026-11-21T00:00:00+08:00",
        "effective_submission_at": "2026-11-24T00:00:00+08:00",
        "late_seconds": 259200,
        "late_human": "3 天",
        "grace_applied": False,
        "cap_percent": 75 if mode == "tiered" else None,
        "max_points": "10.00" if mode == "tiered" else None,
        "awarded_points": awarded if mode == "tiered" else None,
        "policy_version": "2026-fall-v1",
        "band_label": "48 小时以上，7 天以内" if mode == "tiered" else None,
        "details": {
            "deadline_local": "2026-11-21 00:00",
            "submitted_local": "2026-11-24 00:00",
        },
    }
    return ReviewResult(
        decision=Decision.PASS,
        summary="审核通过",
        metadata={
            "repository": "teacher/course",
            "pr_number": 7,
            "head_sha": HEAD,
            "assignment_id": assignment,
            "student_id": "2023010102",
            "github_login": "example-user",
            "grading": grading,
        },
    ).to_dict()


class SemesterSummaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.course = self.fixture.course()
        self.root = Path(tempfile.mkdtemp(prefix="summary-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def seed(self, record):
        self.store.write_json(
            records.receipt_path(record["assignment_id"], record["student_id"]),
            record,
            "seed",
        )

    def test_counts_and_rate_include_the_current_submission(self):
        self.seed(receipt(assignment="Lab1", awarded="7.50"))
        summary = semester_summary(
            self.store,
            self.course,
            "2023010102",
            extra={
                "assignment_id": "Lab1",
                "mode": "tiered",
                "awarded_points": "9.00",
                "max_points": "10.00",
                "time_status": "LATE",
            },
            now=NOW,
        )
        self.assertEqual(summary["assignments_total"], 1)
        self.assertEqual(summary["graded"], 1)
        self.assertEqual(summary["points"], "9.00")
        self.assertEqual(summary["possible"], "10.00")
        self.assertEqual(summary["rate_percent"], "90.0")

    def test_missing_and_not_due_are_separated(self):
        summary = semester_summary(self.store, self.course, "2023010102", now=NOW)
        self.assertEqual(summary["missing"], 1)
        self.assertEqual(summary["not_due"], 0)
        future = semester_summary(
            self.store, self.course, "2023010102", now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        )
        self.assertEqual(future["missing"], 0)
        self.assertEqual(future["not_due"], 1)

    def test_legacy_completion_does_not_create_a_rate(self):
        summary = semester_summary(
            self.store,
            self.course,
            "2023010102",
            extra={
                "assignment_id": "Lab1",
                "mode": "legacy",
                "awarded_points": None,
                "max_points": None,
                "time_status": "ON_TIME",
            },
            now=NOW,
        )
        self.assertEqual(summary["graded"], 0)
        self.assertIsNone(summary["rate_percent"])
        self.assertEqual(summary["completed"], 1)

    def test_rate_uses_exact_decimal_arithmetic(self):
        course = GradingFixture(course_data(max_points="3.00")).course()
        record = records.build_receipt(
            course_id="test-course",
            assignment_id="Lab1",
            student_id="2023010102",
            github_login="example-user",
            pr_number=7,
            reviewed_head_sha="a" * 40,
            grading={
                "mode": "tiered",
                "deadline": "2026-11-21T00:00:00+08:00",
                "effective_submission_at": "2026-11-24T00:00:00+08:00",
                "time_status": "LATE",
                "late_seconds": 259200,
                "cap_percent": 75,
                "max_points": "3.00",
                "awarded_points": "2.00",
                "policy_version": "2026-fall-v1",
            },
            record_status="FINALIZED",
            merged=True,
            merge_commit_sha="b" * 40,
            accepted_at="2026-11-24T09:31:00+08:00",
        )
        self.seed(record)
        summary = semester_summary(self.store, course, "2023010102", now=NOW)
        self.assertEqual(summary["points"], "2.00")
        self.assertEqual(summary["rate_percent"], "66.7")

    def test_missing_store_does_not_raise(self):
        summary = semester_summary(None, self.course, "2023010102", now=NOW)
        self.assertEqual(summary["completed"], 0)


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.course = self.fixture.course()
        self.root = Path(tempfile.mkdtemp(prefix="comment-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def publish(self, client, result, *, course=None, store=None):
        publisher = GitHubResultPublisher(
            client,
            merge_github=client,
            grade_store=self.store if store is None else store,
            roster=self.fixture.roster(),
        )
        return publisher.publish(
            course or self.course, result, expected_repository="teacher/course"
        )

    def test_comment_contains_rate_and_is_refreshed_after_merge(self):
        client = CommentClient(open_pr())
        outcome = self.publish(client, result_for())
        self.assertTrue(outcome.merged)
        self.assertEqual(len(client.comments), 1)
        first = client.comments[0].replace("\\", "")
        self.assertIn("### 本学期成绩（截至本次提交）", first)
        self.assertIn("得分率 75.0%", first)
        self.assertIn("合并确认后写入 grades 分支", first)

        self.assertEqual(len(client.updates), 1)
        comment_id, body = client.updates[0]
        self.assertEqual(comment_id, 4242)
        body = body.replace("\\", "")
        self.assertIn("已写入 grades 分支", body)
        self.assertIn("得分率 75.0%", body)
        self.assertIn("7.50 / 10.00", body)

    def test_legacy_comment_does_not_fabricate_a_rate(self):
        legacy = GradingFixture(course_data(mode="legacy")).course()
        client = CommentClient(open_pr())
        self.publish(client, result_for(mode="legacy"), course=legacy)
        body = client.updates[0][1].replace("\\", "")
        self.assertIn("暂无得分率", body)
        self.assertNotIn("得分率 100.0%", body)

    def test_comment_refresh_failure_does_not_undo_the_merge(self):
        client = CommentClient(open_pr(), fail_update=True)
        outcome = self.publish(client, result_for())
        self.assertTrue(outcome.merged)
        self.assertEqual(outcome.receipt_status, "FINALIZED")
        record = self.store.read_json(records.receipt_path("Lab1", "2023010102"))
        self.assertEqual(record["record_status"], "FINALIZED")

    def test_no_refresh_when_merge_is_disabled(self):
        self.course.data["features"]["auto_merge"] = False
        client = CommentClient(open_pr())
        outcome = self.publish(client, result_for())
        self.assertFalse(outcome.merged)
        self.assertEqual(client.updates, [])
        self.assertIn("合并确认后写入 grades 分支", client.comments[0])

    def test_render_without_semester_is_unchanged(self):
        body = render_comment(result_for())
        self.assertNotIn("本学期成绩", body)


if __name__ == "__main__":
    unittest.main()
