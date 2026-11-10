from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import FakeGitHub, GradingFixture, course_data

from course_pr_reviewer.grading import records
from course_pr_reviewer.grading.store import LocalDirStore
from course_pr_reviewer.models import Decision, ReasonCode
from course_pr_reviewer.publisher import render_comment
from course_pr_reviewer.reviewer import review_pull_request
from course_pr_reviewer.snapshot import ChangedFile

REPORT_PATH = "2023010102刘西莹/Lab1/Lab1.md"
REPORT_TEXT = "实验目的\n实验步骤\n实验结果\n实验总结\n"


def blob_github(text: str = REPORT_TEXT) -> FakeGitHub:
    return FakeGitHub(blobs={"1" * 40: text.encode("utf-8")})


class GradingReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.store_root = Path(tempfile.mkdtemp(prefix="grades-store-"))
        self.addCleanup(shutil.rmtree, self.store_root, True)
        self.store = LocalDirStore(self.store_root)

    def review(self, *, at, course=None, snapshot_kwargs=None, github=None, store=None):
        return review_pull_request(
            course or self.fixture.course(),
            self.fixture.roster(),
            self.fixture.snapshot(at=at, **(snapshot_kwargs or {})),
            github=github,
            grade_store=(
                self.store if store is None else (None if store is False else store)
            ),
        )

    # ------------------------------------------------------------------
    def test_legacy_late_submission_passes_without_score(self):
        course = GradingFixture(course_data(mode="legacy")).course()
        result = self.review(at="2026-12-20T00:00:00+08:00", course=course)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertNotIn(ReasonCode.DEADLINE_EXCEEDED, result.reason_codes)
        grading = result.metadata["grading"]
        self.assertEqual(grading["mode"], "legacy")
        self.assertIsNone(grading["awarded_points"])
        self.assertGreater(grading["late_seconds"], 20 * 86400)

    def test_legacy_late_never_requests_close(self):
        course = GradingFixture(course_data(mode="legacy")).course()
        result = self.review(at="2026-12-20T00:00:00+08:00", course=course)
        self.assertNotIn("close_pr", result.metadata)
        self.assertNotIn(ReasonCode.LATE_PR_CLOSE_REQUIRED, result.reason_codes)

    def test_tiered_on_time_gets_full_marks(self):
        result = self.review(at="2026-11-20T20:30:00+08:00")
        self.assertEqual(result.decision, Decision.PASS)
        grading = result.metadata["grading"]
        self.assertEqual(grading["time_status"], "ON_TIME")
        self.assertEqual(grading["awarded_points"], "10.00")

    def test_tiered_three_days_late_still_merges(self):
        result = self.review(at="2026-11-24T00:00:00+08:00")
        self.assertEqual(result.decision, Decision.PASS)
        grading = result.metadata["grading"]
        self.assertEqual(grading["cap_percent"], 75)
        self.assertEqual(grading["awarded_points"], "7.50")
        self.assertNotIn(ReasonCode.DEADLINE_EXCEEDED, result.reason_codes)

    def test_pre_deadline_head_creates_a_claim(self):
        result = self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
        self.assertEqual(result.decision, Decision.PASS)
        claim = self.store.read_json(records.claim_path("Lab1", "2023010102"))
        self.assertIsNotNone(claim)
        self.assertEqual(claim["eligibility"], "ELIGIBLE")
        self.assertEqual(claim["head_sha"], "a" * 40)
        self.assertTrue(result.metadata["grading"].get("claim_written"))

    def test_stored_claim_gives_full_marks_after_a_late_fix(self):
        self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
        result = self.review(at="2026-11-22T06:00:00+08:00")
        self.assertEqual(result.decision, Decision.PASS)
        grading = result.metadata["grading"]
        self.assertEqual(grading["time_status"], "ON_TIME_CORRECTED")
        self.assertTrue(grading["grace_applied"])
        self.assertEqual(grading["awarded_points"], "10.00")

    def test_without_claim_a_late_fix_is_only_90_percent(self):
        result = self.review(at="2026-11-22T06:00:00+08:00")
        grading = result.metadata["grading"]
        self.assertEqual(grading["cap_percent"], 90)
        self.assertEqual(grading["awarded_points"], "9.00")
        self.assertFalse(grading["grace_applied"])

    def test_fix_after_48_hours_loses_grace(self):
        self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
        result = self.review(at="2026-11-23T01:00:00+08:00")
        grading = result.metadata["grading"]
        self.assertEqual(grading["cap_percent"], 75)
        self.assertFalse(grading["grace_applied"])

    def test_empty_pr_cannot_get_grace(self):
        result = self.review(
            at="2026-11-20T20:30:00+08:00",
            github=blob_github(),
            snapshot_kwargs={"files": ()},
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIsNone(self.store.read_json(records.claim_path("Lab1", "2023010102")))

    def test_template_copy_cannot_get_grace(self):
        github = FakeGitHub(
            blobs={"1" * 40: "# Lab1\n## 1. 实验目的\n刘西莹\n".encode()},
            files={"homework/Lab1/Lab1.md": "# Lab1\n## 1. 实验目的\n（填写）\n"},
        )
        result = self.review(at="2026-11-20T20:30:00+08:00", github=github)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertIsNone(self.store.read_json(records.claim_path("Lab1", "2023010102")))
        self.assertEqual(result.metadata["grading"].get("claim_status"), "NOT_ELIGIBLE")

    def test_unreadable_snapshot_gives_no_claim_and_no_full_marks(self):
        github = FakeGitHub(files={})  # no blobs registered -> loader raises KeyError
        result = self.review(at="2026-11-20T20:30:00+08:00", github=github)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertIsNone(self.store.read_json(records.claim_path("Lab1", "2023010102")))
        self.assertEqual(result.metadata["grading"]["awarded_points"], "10.00")

    def test_missing_store_is_a_retryable_error(self):
        result = self.review(at="2026-11-20T20:30:00+08:00", github=blob_github(), store=False)
        self.assertEqual(result.decision, Decision.ERROR)
        self.assertIn(ReasonCode.SERVICE_ERROR, result.reason_codes)

    def test_existing_earlier_claim_is_kept(self):
        self.store.write_json(
            records.claim_path("Lab1", "2023010102"),
            {
                "schema_version": 1,
                "eligibility": "ELIGIBLE",
                "event_at": "2026-11-19T09:00:00+08:00",
                "head_sha": "e" * 40,
            },
            "seed",
        )
        result = self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
        claim = self.store.read_json(records.claim_path("Lab1", "2023010102"))
        self.assertEqual(claim["event_at"], "2026-11-19T09:00:00+08:00")
        self.assertFalse(result.metadata["grading"].get("claim_written"))

    def test_after_final_accept_blocks_merge(self):
        course = GradingFixture(
            course_data(final_accept_at="2026-12-31T23:59:59+08:00")
        ).course()
        result = self.review(at="2027-01-03T00:00:00+08:00", course=course)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.AFTER_FINAL_ACCEPT, result.reason_codes)
        self.assertEqual(result.metadata["grading"]["awarded_points"], "0.00")

    def test_reruns_are_idempotent(self):
        first = self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
        snapshot_claim = self.store.read_json(records.claim_path("Lab1", "2023010102"))
        scored_fields = (
            "mode",
            "time_status",
            "late_seconds",
            "cap_percent",
            "max_points",
            "awarded_points",
        )
        for _ in range(10):
            again = self.review(at="2026-11-20T20:30:00+08:00", github=blob_github())
            self.assertEqual(again.decision, first.decision)
            for field in scored_fields:
                self.assertEqual(
                    again.metadata["grading"][field], first.metadata["grading"][field]
                )
        self.assertEqual(
            self.store.read_json(records.claim_path("Lab1", "2023010102")),
            snapshot_claim,
        )

    def test_legacy_does_not_create_claims(self):
        course = GradingFixture(course_data(mode="legacy")).course()
        self.review(at="2026-11-20T20:30:00+08:00", course=course, github=blob_github())
        self.assertIsNone(self.store.read_json(records.claim_path("Lab1", "2023010102")))

    def test_grace_disabled_does_not_create_claims(self):
        course = GradingFixture(course_data(grace_enabled=False)).course()
        self.review(at="2026-11-20T20:30:00+08:00", course=course, github=blob_github())
        self.assertIsNone(self.store.read_json(records.claim_path("Lab1", "2023010102")))

    def test_late_review_still_lists_content_problems(self):
        result = self.review(
            at="2026-11-24T00:00:00+08:00",
            snapshot_kwargs={
                "files": (ChangedFile(REPORT_PATH, "added", blob_sha="1" * 40, content=""),)
            },
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.CONTENT_TOO_SHORT, result.reason_codes)
        self.assertEqual(result.metadata["grading"]["cap_percent"], 75)


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()

    def comment_for(self, *, at: str, mode: str = "tiered"):
        course = self.fixture.course() if mode == "tiered" else GradingFixture(
            course_data(mode="legacy")
        ).course()
        result = review_pull_request(
            course, self.fixture.roster(), self.fixture.snapshot(at=at)
        )
        return render_comment(result.to_dict())

    def test_comment_shows_timeouts_and_score(self):
        body = self.comment_for(at="2026-11-24T09:30:00+08:00").replace("\\", "")
        self.assertIn("### 成绩记录", body)
        self.assertIn("2026-11-21 00:00", body)
        self.assertIn("75%", body)
        self.assertIn("7.50", body)
        self.assertIn("48 小时以上，7 天以内", body)
        self.assertIn("无需向教师私聊申请补交", body)

    def test_legacy_comment_does_not_invent_a_score(self):
        body = self.comment_for(at="2026-11-24T09:30:00+08:00", mode="legacy").replace(
            "\\", ""
        )
        self.assertIn("不实行分段迟交计分", body)
        self.assertNotIn("本次得分", body)

    def test_failed_content_shows_estimate_only(self):
        course = self.fixture.course()
        result = review_pull_request(
            course,
            self.fixture.roster(),
            self.fixture.snapshot(
                at="2026-11-24T00:00:00+08:00",
                files=(ChangedFile(REPORT_PATH, "added", content=""),),
            ),
        )
        body = render_comment(result.to_dict()).replace("\\", "")
        self.assertIn("本次未产生成绩记录", body)
        self.assertIn("仅提示", body)


if __name__ == "__main__":
    unittest.main()
