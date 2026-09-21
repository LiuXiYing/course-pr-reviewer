from __future__ import annotations

import copy
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import Mock

from course_pr_reviewer.ai import AIOutcome
from course_pr_reviewer.config import (
    CourseConfiguration,
    load_course_config,
    load_student_roster,
)
from course_pr_reviewer.models import Decision, Issue, ReasonCode
from course_pr_reviewer.path_utils import canonical_filename, resolve_filename
from course_pr_reviewer.reviewer import review_pull_request
from course_pr_reviewer.exceptions import ContentLimitExceeded, ReviewSystemError
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

    def test_assignment_case_error_gives_the_exact_title_without_accepting_it(self):
        for template in (
            "[{student_id}{student_name}]{assignment_id}作业提交",
            "[{student_identity}]{assignment_id}作业提交",
        ):
            self.course.data["course"]["title_template"] = template
            for assignment_id in ("lab1", "LAB1", "lAb1"):
                with self.subTest(template=template, assignment=assignment_id):
                    result = review_pull_request(
                        self.course, self.roster,
                        self.snapshot(title=f"[2023010102刘西莹]{assignment_id}作业提交"),
                    )
                    self.assertEqual(result.decision, Decision.FAIL)
                    self.assertEqual(result.reason_codes, ("TITLE_MISMATCH",))
                    self.assertIn("大小写", result.issues[0].message)
                    self.assertIn(f"`{assignment_id}`", result.issues[0].message)
                    self.assertIn("`[2023010102刘西莹]Lab1作业提交`", result.issues[0].message)
                    self.assertNotIn("assignment_id", result.metadata)

    def test_missing_assignment_gives_enabled_titles_without_guessing(self):
        result = review_pull_request(
            self.course, self.roster,
            self.snapshot(title="[2023010102刘西莹]作业提交"),
        )
        self.assertEqual(result.reason_codes, ("TITLE_MISMATCH",))
        message = result.issues[0].message
        self.assertIn("缺少作业编号", message)
        self.assertIn("`[2023010102刘西莹]Lab1作业提交`", message)
        self.assertIn("`[2023010102刘西莹]Lab2作业提交`", message)
        self.assertNotIn("正确标题应为", message)
        self.assertNotIn("assignment_id", result.metadata)

    def test_disabled_assignment_is_not_reported_as_just_a_case_error(self):
        self.course.data["assignments"]["Lab1"]["enabled"] = False
        for assignment_id in ("Lab1", "lab1"):
            with self.subTest(assignment=assignment_id):
                result = review_pull_request(
                    self.course, self.roster,
                    self.snapshot(title=f"[2023010102刘西莹]{assignment_id}作业提交"),
                )
                self.assertEqual(result.reason_codes, ("ASSIGNMENT_NOT_CONFIGURED",))
                self.assertIn("未启用", result.issues[0].message)
                self.assertIn("联系教师", result.issues[0].message)
                self.assertNotIn("正确标题应为", result.issues[0].message)

    def test_ambiguous_case_matches_do_not_select_an_assignment(self):
        self.course.data["assignments"]["LAB1"] = copy.deepcopy(
            self.course.data["assignments"]["Lab1"]
        )
        result = review_pull_request(
            self.course, self.roster,
            self.snapshot(title="[2023010102刘西莹]lab1作业提交"),
        )
        self.assertEqual(result.reason_codes, ("ASSIGNMENT_NOT_CONFIGURED",))
        self.assertIn("多个", result.issues[0].message)
        self.assertNotIn("正确标题应为", result.issues[0].message)
        self.assertNotIn("assignment_id", result.metadata)

    def test_exact_disabled_assignment_takes_precedence_over_enabled_case_match(self):
        self.course.data["assignments"]["lab1"] = {
            **self.course.data["assignments"]["Lab1"], "enabled": False,
        }
        result = review_pull_request(
            self.course, self.roster,
            self.snapshot(title="[2023010102刘西莹]lab1作业提交"),
        )
        self.assertEqual(result.reason_codes, ("ASSIGNMENT_NOT_CONFIGURED",))
        self.assertIn("`lab1` 已配置但未启用", result.issues[0].message)

    def test_title_examples_do_not_offer_disabled_assignments(self):
        self.course.data["assignments"]["Lab1"]["enabled"] = False
        result = review_pull_request(
            self.course, self.roster,
            self.snapshot(title="[2023010102刘西莹]作业提交"),
        )
        self.assertNotIn(
            "`[2023010102刘西莹]Lab1作业提交`", result.issues[0].message,
        )
        self.assertIn("`[2023010102刘西莹]Lab2作业提交`", result.issues[0].message)
        self.course.data["assignments"]["Lab2"]["enabled"] = False
        result = review_pull_request(
            self.course, self.roster,
            self.snapshot(title="[2023010102刘西莹]作业提交"),
        )
        self.assertIn("当前没有已启用的作业", result.issues[0].message)

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

    def report(self, lines, filename="2023010102刘西莹/Lab1/Lab1.md"):
        return ChangedFile(filename, "added", content="\n".join(lines))

    def test_report_content_is_fetched_from_github_before_ai_review(self):
        class FakeGitHub:
            def __init__(self, blobs):
                self.blobs = blobs
                self.calls = []

            def text_blob(self, repository, sha, *, max_bytes):
                self.calls.append((repository, sha, max_bytes))
                return self.blobs[sha]

        github = FakeGitHub({"e" * 40: ""})
        files = (
            ChangedFile(
                "2023010102刘西莹/Lab1/Lab1.md", "added", blob_sha="e" * 40
            ),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added", blob_sha="f" * 40),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files), github=github
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))
        self.assertIn("空文件", result.issues[0].message)
        self.assertEqual(
            github.calls, [("teacher/course", "e" * 40, 200_000)]
        )

    def test_unfetchable_report_content_still_reaches_the_ai_stage(self):
        class NoneGitHub:
            def text_blob(self, repository, sha, *, max_bytes):
                return None

        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added", blob_sha="e" * 40),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files),
            github=NoneGitHub(),
        )
        self.assertEqual(result.decision, Decision.PASS)
        self.assertNotIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))

    def test_empty_report_file_fails_before_ai_review(self):
        files = (
            self.report([]),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))
        message = result.issues[0].message
        self.assertIn("空文件", message)
        self.assertIn("Lab1.md", message)
        self.assertNotIn("SERVICE_ERROR", result.reason_codes)

    def test_whitespace_only_report_counts_as_empty(self):
        files = (self.report(["   ", "\t", "", "  "]),)
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))

    def test_short_report_reports_the_actual_and_required_line_counts(self):
        files = (self.report([f"内容 {i}" for i in range(3)]),)
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertEqual(result.decision, Decision.FAIL)
        issue = next(
            issue for issue in result.issues
            if issue.code is ReasonCode.CONTENT_TOO_SHORT
        )
        self.assertIn("3 行", issue.message)
        self.assertIn("10 行", issue.message)

    def test_report_meeting_min_nonempty_lines_passes(self):
        files = (
            self.report([f"内容 {i}" for i in range(10)]),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertEqual(result.decision, Decision.PASS)

    def test_unloaded_file_content_is_left_to_the_vision_stage(self):
        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
        )
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertEqual(result.decision, Decision.PASS)
        self.assertNotIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))

    def test_oversized_report_content_is_not_downloaded_twice(self):
        class LimitedGitHub:
            def __init__(self):
                self.calls = 0

            def text_blob(self, repository, sha, *, max_bytes):
                self.calls += 1
                raise ReviewSystemError("GitHub API GET blob 返回 HTTP 413")

        files = (ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added", blob_sha="e" * 40),)
        github = LimitedGitHub()
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files), github=github
        )
        self.assertEqual(github.calls, 1)
        self.assertNotIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))

    def test_min_nonempty_lines_can_be_disabled_per_assignment(self):
        self.course.data["assignments"]["Lab1"]["min_nonempty_lines"] = 0
        files = (self.report([]),)
        result = review_pull_request(
            self.course, self.roster, self.snapshot(files=files)
        )
        self.assertNotIn(ReasonCode.CONTENT_TOO_SHORT, self.codes(result))

    def address_check_snapshot(self, value="（填写）", *, loaded=True):
        assignment = self.course.data["assignments"]["Lab1"]
        assignment["min_nonempty_lines"] = 0
        assignment["required_address_cells"] = [{
            "file": "Lab1.md", "section": "3.6.4", "row": "data 地址",
            "columns": [1, 2, 3],
        }]
        content = f"#### 3.6.4 观察记录\n| data 地址 | {value} | {value} | {value} |\n"
        files = (
            ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added",
                        content=content if loaded else None, blob_sha="e" * 40),
            ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
        )
        return self.snapshot(files=files)

    def test_required_addresses_fail_before_ai_can_approve(self):
        snapshot = self.address_check_snapshot()
        self.course.data["features"].update(ai_review=True, vision_review=True)
        ai = Mock()
        vision = Mock()
        result = review_pull_request(self.course, self.roster, snapshot, ai, vision)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(len(result.issues), 3)
        self.assertIn(ReasonCode.REQUIRED_ADDRESS_MISSING, self.codes(result))
        ai.review.assert_not_called()
        vision.review.assert_not_called()

    def test_filled_addresses_allow_ai_and_vision_to_review(self):
        snapshot = self.address_check_snapshot("`0x123abc`")
        self.course.data["features"].update(ai_review=True, vision_review=True)
        ai = Mock()
        vision = Mock()
        ai.review.return_value = vision.review.return_value = AIOutcome(
            decision=Decision.PASS, summary="通过", confidence=1.0,
        )
        result = review_pull_request(self.course, self.roster, snapshot, ai, vision)
        self.assertEqual(result.decision, Decision.PASS)
        ai.review.assert_called_once()
        vision.review.assert_called_once()

    def test_required_addresses_are_fetched_when_line_check_is_disabled(self):
        snapshot = self.address_check_snapshot(loaded=False)
        github = Mock()
        github.text_blob.return_value = "#### 3.6.4 观察记录\n| data 地址 | | 填写 | （填写） |\n"
        result = review_pull_request(self.course, self.roster, snapshot, github=github)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(len(result.issues), 3)
        github.text_blob.assert_called_once_with("teacher/course", "e" * 40, max_bytes=200_000)

    def test_unreadable_required_address_report_cannot_pass(self):
        snapshot = self.address_check_snapshot(loaded=False)
        github = Mock()
        for error in (None, ReviewSystemError("HTTP 403"), ContentLimitExceeded("too large")):
            with self.subTest(error=error):
                github.text_blob.return_value = None
                github.text_blob.side_effect = error
                with self.assertRaises((ReviewSystemError, ContentLimitExceeded)):
                    review_pull_request(self.course, self.roster, snapshot, github=github)
        with self.assertRaises(ReviewSystemError):
            review_pull_request(self.course, self.roster, snapshot)

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


class LateSubmissionOrderTests(unittest.TestCase):
    """截止时间判定排在内容审核之后，且不再短路任何内容审核。

    规则：内容问题照常返回，超时只作为附加结论跟在后面；内容全部通过、只是超时
    时才需要单独说明，避免学生以为作业内容不合格。
    """

    PLUS_8 = dt.timezone(dt.timedelta(hours=8))
    # 比截止时间 2026-09-01T23:59:59+08:00 晚 36 小时 1 秒：已超时，但没到关闭阈值。
    LATE = dt.datetime(2026, 9, 3, 12, tzinfo=PLUS_8)
    # 超出截止时间 18 天，越过默认的 7 天关闭阈值。
    VERY_LATE = dt.datetime(2026, 9, 20, tzinfo=PLUS_8)

    def setUp(self):
        loaded = load_course_config(ROOT / "examples/course-review.yml")
        data = copy.deepcopy(loaded.data)
        data["features"].update(
            ai_review=True, vision_review=True, close_late_pr=True
        )
        data["assignments"]["Lab1"]["deadline"] = "2026-09-01T23:59:59+08:00"
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
            "event_at": self.LATE,
            "files": (
                ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),
                ChangedFile("2023010102刘西莹/Lab1/result.png", "added"),
            ),
        }
        values.update(overrides)
        return PullRequestSnapshot(**values)

    def passing_reviewer(self):
        reviewer = Mock()
        reviewer.review.return_value = AIOutcome(
            decision=Decision.PASS, summary="通过", confidence=1.0,
        )
        return reviewer

    def codes(self, result):
        return {issue.code for issue in result.issues}

    def test_late_submission_still_runs_ai_and_vision_review(self):
        ai, vision = self.passing_reviewer(), self.passing_reviewer()
        result = review_pull_request(
            self.course, self.roster, self.snapshot(), ai, vision
        )
        ai.review.assert_called_once()
        vision.review.assert_called_once()
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(result.reason_codes, ("DEADLINE_EXCEEDED",))

    def test_only_late_says_content_passed(self):
        ai, vision = self.passing_reviewer(), self.passing_reviewer()
        result = review_pull_request(
            self.course, self.roster, self.snapshot(), ai, vision
        )
        self.assertEqual(len(result.issues), 1)
        self.assertIn("作业内容审核已全部通过", result.summary)
        self.assertIn("晚于截止时间", result.summary)
        self.assertIn("36 小时", result.issues[0].message)
        self.assertEqual(result.metadata["late_seconds"], 129_601)
        self.assertNotIn("close_pr", result.metadata)

    def test_late_with_ai_rejection_keeps_content_summary_and_reports_both(self):
        ai = Mock()
        ai.review.return_value = AIOutcome(
            decision=Decision.FAIL,
            summary="AI 发现内容问题",
            issues=(
                Issue(code=ReasonCode.AI_REJECTED, message="学生内容不符合评分点"),
            ),
            confidence=0.95,
        )
        vision = self.passing_reviewer()
        result = review_pull_request(
            self.course, self.roster, self.snapshot(), ai, vision
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(result.summary, "AI 发现内容问题")
        self.assertIn(ReasonCode.AI_REJECTED, self.codes(result))
        self.assertIn(ReasonCode.DEADLINE_EXCEEDED, self.codes(result))
        self.assertEqual(result.issues[-1].code, ReasonCode.DEADLINE_EXCEEDED)
        vision.review.assert_not_called()

    def test_very_late_submission_still_requests_automatic_close(self):
        ai, vision = self.passing_reviewer(), self.passing_reviewer()
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(event_at=self.VERY_LATE),
            ai,
            vision,
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(result.reason_codes, ("LATE_PR_CLOSE_REQUIRED",))
        self.assertTrue(result.metadata["close_pr"])
        self.assertIn("作业内容审核已全部通过", result.summary)
        self.assertIn("关闭", result.summary)

    def test_deterministic_content_error_still_reports_the_late_close(self):
        ai, vision = self.passing_reviewer(), self.passing_reviewer()
        files = (ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),)
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(event_at=self.VERY_LATE, files=files),
            ai,
            vision,
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn(ReasonCode.REQUIRED_FILE_MISSING, self.codes(result))
        self.assertIn(ReasonCode.LATE_PR_CLOSE_REQUIRED, self.codes(result))
        self.assertTrue(result.metadata["close_pr"])
        ai.review.assert_not_called()

    def test_empty_late_pull_request_is_still_reported_as_late(self):
        result = review_pull_request(
            self.course, self.roster, self.snapshot(event_at=self.LATE, files=())
        )
        self.assertIn(ReasonCode.NO_FILES_CHANGED, self.codes(result))
        self.assertIn(ReasonCode.DEADLINE_EXCEEDED, self.codes(result))

    def test_late_manual_review_never_requests_automatic_close(self):
        ai = Mock()
        ai.review.return_value = AIOutcome(
            decision=Decision.MANUAL_REVIEW,
            summary="AI 无法确认",
            issues=(Issue(code=ReasonCode.AI_UNCERTAIN, message="证据不足"),),
        )
        vision = self.passing_reviewer()
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(event_at=self.VERY_LATE),
            ai,
            vision,
        )
        self.assertEqual(result.decision, Decision.MANUAL_REVIEW)
        self.assertNotIn("close_pr", result.metadata)
        self.assertNotIn(ReasonCode.LATE_PR_CLOSE_REQUIRED, self.codes(result))
        self.assertIn(ReasonCode.DEADLINE_EXCEEDED, self.codes(result))

    def test_unidentifiable_pull_request_is_not_judged_on_time(self):
        # 标题里认不出作业编号时拿不到 deadline，超时无从判定。
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(event_at=self.VERY_LATE, title="Lab1"),
        )
        self.assertIn(ReasonCode.TITLE_MISMATCH, self.codes(result))
        self.assertNotIn(ReasonCode.DEADLINE_EXCEEDED, self.codes(result))
        self.assertNotIn("close_pr", result.metadata)
        self.assertNotIn("late_seconds", result.metadata)

    def test_on_time_submission_is_untouched_by_the_late_step(self):
        ai, vision = self.passing_reviewer(), self.passing_reviewer()
        result = review_pull_request(
            self.course,
            self.roster,
            self.snapshot(
                event_at=dt.datetime(2026, 8, 20, tzinfo=self.PLUS_8)
            ),
            ai,
            vision,
        )
        self.assertEqual(result.decision, Decision.PASS)
        self.assertNotIn("late_seconds", result.metadata)
        self.assertNotIn("close_pr", result.metadata)


if __name__ == "__main__":
    unittest.main()
