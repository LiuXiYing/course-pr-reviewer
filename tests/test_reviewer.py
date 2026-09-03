from __future__ import annotations

import copy
import datetime as dt
import unittest
from pathlib import Path

from course_pr_reviewer.ai import AIOutcome
from course_pr_reviewer.config import (
    CourseConfiguration,
    load_course_config,
    load_student_roster,
)
from course_pr_reviewer.models import Decision, Issue, ReasonCode
from course_pr_reviewer.path_utils import canonical_filename, resolve_filename
from course_pr_reviewer.reviewer import review_pull_request
from course_pr_reviewer.snapshot import ChangedFile, PullRequestSnapshot

ROOT = Path(__file__).parents[1]
SHA = "a" * 40


class DeterministicReviewerTests(unittest.TestCase):
    def setUp(self):
        loaded = load_course_config(ROOT / "examples/course-review.yml")
        data = copy.deepcopy(loaded.data)
        data["features"].update(ai_review=False, ocr_review=False, vision_review=False)
        data["assignments"]["Lab1"]["deadline"] = "2099-09-20T23:59:59+08:00"
        self.course = CourseConfiguration(data)
        self.roster = load_student_roster(ROOT / "examples/students.yml")

    def snapshot(self, **overrides):
        values = {
            "repository": "teacher/course",
            "number": 12,
            "title": "[2023010102刘西莹]Lab1作业提交",
            "author_login": "example-user",
            "captured_head_sha": SHA,
            "current_head_sha": SHA,
            "event_at": dt.datetime(
                2026, 9, 1, tzinfo=dt.timezone(dt.timedelta(hours=8))
            ),
            "files": (
                ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),
                ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
            ),
        }
        values.update(overrides)
        return PullRequestSnapshot(**values)

    def codes(self, result):
        return {issue.code for issue in result.issues}

    def test_clean_pull_request_passes(self):
        result = review_pull_request(self.course, self.roster, self.snapshot())
        self.assertEqual(result.decision, Decision.PASS)

    def test_combined_student_identity_template_passes(self):
        self.course.data["course"]["title_template"] = (
            "[{student_identity}]{assignment_id}作业提交"
        )
        self.course.data["course"]["submission_path_template"] = (
            "{student_identity}/{assignment_id}"
        )
        result = review_pull_request(self.course, self.roster, self.snapshot())
        self.assertEqual(result.decision, Decision.PASS)

    def test_unknown_account_requires_manual_review(self):
        result = review_pull_request(
            self.course, self.roster, self.snapshot(author_login="not-registered")
        )
        self.assertEqual(result.decision, Decision.MANUAL_REVIEW)
        self.assertIn(ReasonCode.UNKNOWN_GITHUB_USER, self.codes(result))

    def test_stale_sha_is_error(self):
        result = review_pull_request(
            self.course, self.roster, self.snapshot(current_head_sha="b" * 40)
        )
        self.assertEqual(result.decision, Decision.ERROR)
        self.assertIn(ReasonCode.STALE_HEAD_SHA, self.codes(result))

    def test_malformed_title_fails(self):
        result = review_pull_request(
            self.course, self.roster, self.snapshot(title="Lab1")
        )
        self.assertIn(ReasonCode.TITLE_MISMATCH, self.codes(result))

    def test_title_cannot_impersonate_another_student(self):
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(title="[2023010103张三]Lab1作业提交"),
        )
        self.assertIn(ReasonCode.IDENTITY_MISMATCH, self.codes(result))

    def test_unconfigured_assignment_fails(self):
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(title="[2023010102刘西莹]Lab99作业提交"),
        )
        self.assertIn(ReasonCode.ASSIGNMENT_NOT_CONFIGURED, self.codes(result))

    def test_empty_pull_request_fails(self):
        result = review_pull_request(self.course, self.roster, self.snapshot(files=()))
        self.assertIn(ReasonCode.NO_FILES_CHANGED, self.codes(result))

    def test_delete_rename_scope_and_old_assignment_are_reported(self):
        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "removed"),
            ChangedFile(
                "2023010102刘西莹/Lab1/result.png",
                "renamed",
                "2023010102刘西莹/Lab1/old.png",
            ),
            ChangedFile("README.md", "modified"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertTrue(
            {
                ReasonCode.FILE_DELETED,
                ReasonCode.FILE_RENAMED,
                ReasonCode.PATH_OUT_OF_SCOPE,
                ReasonCode.REQUIRED_FILE_MISSING,
            }.issubset(self.codes(result))
        )

        old_files = (ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "modified"),)
        lab2 = self.snapshot(title="[2023010102刘西莹]Lab2作业提交", files=old_files)
        old_result = review_pull_request(self.course, self.roster, lab2)
        self.assertIn(ReasonCode.OLD_ASSIGNMENT_MODIFIED, self.codes(old_result))

    def test_missing_and_extra_files_are_reported(self):
        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),
            ChangedFile("2023010102刘西莹/Lab1/notes.txt", "added"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertIn(ReasonCode.REQUIRED_FILE_MISSING, self.codes(result))
        self.assertIn(ReasonCode.EXTRA_FILE, self.codes(result))

    def test_filename_hyphen_variants_match_ascii_requirements(self):
        self.course.data["assignments"]["Lab1"]["required_files"] = [
            "Lab1.md",
            "imgs/lab1-vmware-version.png",
        ]
        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),
            ChangedFile(
                "2023010102刘西莹/Lab1/imgs/lab1‑vmware‑version.png",
                "added",
            ),
        )

        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )

        self.assertEqual(result.decision, Decision.PASS)

    def test_filename_normalization_is_narrow_and_ambiguous_aliases_are_safe(self):
        for variant in ("‐", "‑", "－"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    canonical_filename(f"lab1{variant}result.png"),
                    "lab1-result.png",
                )
        self.assertNotEqual(canonical_filename("lab1–result.png"), "lab1-result.png")
        self.assertIsNone(
            resolve_filename(
                "lab1‐result.png",
                {"lab1-result.png", "lab1‑result.png"},
            )
        )

    def test_late_event_fails(self):
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(
                event_at=dt.datetime(
                    2099,
                    9,
                    21,
                    tzinfo=dt.timezone(dt.timedelta(hours=8)),
                )
            ),
        )
        self.assertIn(ReasonCode.DEADLINE_EXCEEDED, self.codes(result))

    def test_very_late_event_requests_automatic_close(self):
        self.course.data["features"]["close_late_pr"] = True
        self.course.data["defaults"]["late_close_after_days"] = 7
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(event_at=dt.datetime(2100, 1, 1, tzinfo=dt.UTC)),
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertTrue(result.metadata["close_pr"])
        self.assertIn(ReasonCode.LATE_PR_CLOSE_REQUIRED, self.codes(result))

    def test_enabled_ai_without_configured_reviewer_is_error(self):
        self.course.data["features"]["ai_review"] = True
        result = review_pull_request(self.course, self.roster, self.snapshot())
        self.assertEqual(result.decision, Decision.ERROR)
        self.assertIn(ReasonCode.SERVICE_ERROR, self.codes(result))

    def test_ai_fail_is_returned_by_pipeline(self):
        class RejectingAI:
            def review(self, course, assignment_id, snapshot):
                return AIOutcome(
                    decision=Decision.FAIL,
                    summary="AI 发现内容问题",
                    issues=(
                        Issue(
                            code=ReasonCode.AI_REJECTED,
                            message="学生内容不符合评分点",
                        ),
                    ),
                    confidence=0.95,
                    metadata={"total_tokens": 120},
                )

        self.course.data["features"]["ai_review"] = True
        result = review_pull_request(
            self.course, self.roster, self.snapshot(), ai_reviewer=RejectingAI()
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(result.metadata["ai_total_tokens"], 120)

    def test_vision_pass_is_returned_by_pipeline(self):
        class PassingVision:
            def review(self, course, assignment_id, snapshot, submission_dir):
                self.submission_dir = submission_dir
                return AIOutcome(
                    decision=Decision.PASS,
                    summary="图片通过",
                    confidence=0.98,
                    metadata={"image_count": 1},
                )

        self.course.data["features"]["vision_review"] = True
        reviewer = PassingVision()
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(),
            vision_reviewer=reviewer,
        )
        self.assertEqual(result.decision, Decision.PASS)
        self.assertEqual(result.metadata["vision_image_count"], 1)
        self.assertEqual(reviewer.submission_dir, "2023010102刘西莹/Lab1")

    def test_enabled_vision_without_reviewer_is_error(self):
        self.course.data["features"]["vision_review"] = True
        result = review_pull_request(self.course, self.roster, self.snapshot())
        self.assertEqual(result.decision, Decision.ERROR)
        self.assertIn(ReasonCode.SERVICE_ERROR, self.codes(result))


if __name__ == "__main__":
    unittest.main()
