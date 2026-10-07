from __future__ import annotations

import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from course_pr_reviewer.ai import AIOutcome
from course_pr_reviewer.config import CourseConfiguration, load_course_config, load_student_roster
from course_pr_reviewer.exceptions import ConfigurationError, ContentLimitExceeded, ReviewSystemError
from course_pr_reviewer.models import Decision, Issue, ReasonCode
from course_pr_reviewer.reviewer import review_pull_request
from course_pr_reviewer.snapshot import ChangedFile, PullRequestSnapshot
from course_pr_reviewer.template_metrics import DEFAULT_CHARACTERS, measure

ROOT = Path(__file__).parents[1]
TEMPLATE = "# 实验\n\n- **任务**\n\n> `命令`\n\n| 字段 | 答案 |\n| --- | --- |\n"
REPORT = "2023010102刘西莹/Lab1/Lab1.md"


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.course = load_course_config(ROOT / "examples/course-review.yml")
        self.course.data["features"].update(ai_review=False, vision_review=False, ocr_review=False)
        self.assignment = self.course.assignments["Lab1"]
        self.assignment.update(
            deadline="2099-01-01T00:00:00+08:00", min_nonempty_lines=0,
            report_template="homework/Lab1/Lab1.md",
            template_metrics={"enabled": True, "report_file": "Lab1.md"},
        )
        self.roster = load_student_roster(ROOT / "examples/students.yml")
        self.github = Mock()
        self.github.text_file.return_value = TEMPLATE
        self.github.text_blob.return_value = TEMPLATE
        self.ai = Mock()
        self.vision = Mock()

    def review(self, content=TEMPLATE, **overrides):
        fields = dict(
            repository="teacher/course", number=1,
            title="[2023010102刘西莹]Lab1作业提交", author_login="example-user",
            captured_head_sha="a" * 40, current_head_sha="a" * 40, base_sha="b" * 40,
            event_at=dt.datetime(2026, 10, 7, tzinfo=dt.UTC),
            files=(ChangedFile(REPORT, "added", blob_sha="c" * 40, content=content),
                   ChangedFile("2023010102刘西莹/Lab1/result.png", "added")),
        )
        fields.update(overrides)
        return review_pull_request(
            self.course, self.roster, PullRequestSnapshot(**fields),
            github=self.github, ai_reviewer=self.ai, vision_reviewer=self.vision,
        )

    def test_normalization_and_line_count_boundaries(self):
        for text, total, nonempty in (
            ("", 0, 0), ("\ufeff", 0, 0), ("\n", 1, 0),
            ("a", 1, 1), ("a\n", 1, 1), ("a\n\n", 2, 1),
            ("a\r\nb\rc\n", 3, 3), ("a\u2028b\v", 1, 1),
        ):
            with self.subTest(text=text):
                counts = measure(text, DEFAULT_CHARACTERS)
                self.assertEqual((counts["total_lines"], counts["nonempty_lines"]), (total, nonempty))
        self.assertEqual(measure(TEMPLATE, DEFAULT_CHARACTERS),
                         measure("\ufeff" + TEMPLATE.replace("\n", "\r\n"), DEFAULT_CHARACTERS))

    def test_equal_or_expanded_report_passes_metrics_not_content_grading(self):
        for content in (TEMPLATE, TEMPLATE + "\n补充实际答案\n"):
            with self.subTest(content=content):
                result = self.review(content)
                self.assertEqual(result.decision, Decision.PASS)
                self.assertEqual(result.metadata["template_metrics"]["character_check"], "PASS")
        self.github.text_file.assert_called_with(
            "teacher/course", "homework/Lab1/Lab1.md", "b" * 40, max_bytes=200_000,
        )

    def test_short_report_stops_before_characters_and_ai(self):
        self.course.data["features"].update(ai_review=True, vision_review=True)
        result = self.review("# 少量内容\n")
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(set(result.reason_codes), {"TEMPLATE_LINES_SHORT", "TEMPLATE_NONEMPTY_LINES_SHORT"})
        self.assertEqual(result.metadata["template_metrics"]["character_check"], "NOT_RUN")
        self.ai.review.assert_not_called()
        self.vision.review.assert_not_called()

    def test_blank_padding_does_not_satisfy_nonempty_lines(self):
        result = self.review("# 少量内容\n" + "\n" * 20)
        self.assertEqual(result.reason_codes, ("TEMPLATE_NONEMPTY_LINES_SHORT",))

    def test_only_total_line_shortfall_still_fails(self):
        result = self.review(TEMPLATE.replace("\n\n", "\n"))
        self.assertEqual(result.reason_codes, ("TEMPLATE_LINES_SHORT",))

    def test_each_symbol_is_compared_separately_and_fullwidth_is_not_equal(self):
        for character in DEFAULT_CHARACTERS:
            with self.subTest(character=character):
                result = self.review(TEMPLATE.replace(character, "＃") + "\n" + "!" * 200)
                self.assertEqual(result.reason_codes, ("TEMPLATE_CHARACTERS_SHORT",))
                self.assertEqual(len(result.issues), 1)
                self.assertIn(repr(character), result.issues[0].message)
                self.assertEqual(result.metadata["template_metrics"]["line_check"], "PASS")

    def test_custom_symbols_and_raw_code_comment_characters(self):
        self.assignment["template_metrics"]["characters"] = ["#", "*"]
        result = self.review(TEMPLATE.replace("-", "+"))
        self.assertEqual(result.decision, Decision.PASS)
        counts = measure("<!-- # -->\n```\n# cmd --flag\n```", ["#", "-", "`"])
        self.assertEqual(counts["characters"], {"#": 2, "-": 6, "`": 6})

    def test_disabled_or_absent_setting_performs_no_fetch(self):
        for settings in (None, {"enabled": False}):
            with self.subTest(settings=settings):
                if settings is None:
                    self.assignment.pop("template_metrics", None)
                else:
                    self.assignment["template_metrics"] = settings
                result = self.review("x", base_sha=None)
                self.assertEqual(result.decision, Decision.PASS)
                self.assertNotIn("template_metrics", result.metadata)
        self.github.text_file.assert_not_called()
        self.github.text_blob.assert_not_called()

    def test_other_assignment_and_defaults_cannot_enable_this_one(self):
        settings = self.assignment.pop("template_metrics")
        self.course.assignments["Lab2"]["template_metrics"] = settings
        # Defense in depth for callers constructing CourseConfiguration directly;
        # the YAML schema rejects this global configuration.
        self.course.data["defaults"]["template_metrics"] = settings
        self.assertEqual(self.review("x").decision, Decision.PASS)
        self.github.text_file.assert_not_called()

    def test_missing_or_unreadable_template_is_config_error_not_student_failure(self):
        for template in (None, "", "\ufeff \n", "abc\0"):
            with self.subTest(template=template):
                self.github.text_file.return_value = template
                result = self.review()
                self.assertEqual(result.decision, Decision.ERROR)
                self.assertEqual(result.reason_codes, ("CONFIG_ERROR",))
        for error in (ReviewSystemError("404"), ContentLimitExceeded("too large")):
            self.github.text_file.side_effect = error
            self.assertEqual(self.review().reason_codes, ("CONFIG_ERROR",))

    def test_missing_base_sha_fails_closed_without_fetch(self):
        result = self.review(base_sha=None)
        self.assertEqual(result.decision, Decision.ERROR)
        self.github.text_file.assert_not_called()

    def test_lazy_student_blob_is_loaded_by_immutable_sha(self):
        self.assertEqual(self.review(None).decision, Decision.PASS)
        self.github.text_blob.assert_called_once_with("teacher/course", "c" * 40, max_bytes=200_000)

    def test_student_encoding_limit_and_service_errors_remain_distinct(self):
        self.github.text_blob.return_value = None
        self.assertEqual(self.review(None).reason_codes, ("INVALID_FILE",))
        self.github.text_blob.side_effect = ContentLimitExceeded("large")
        self.assertEqual(self.review(None).reason_codes, ("INVALID_FILE",))
        self.github.text_blob.side_effect = ReviewSystemError("network")
        result = self.review(None)
        self.assertEqual(result.decision, Decision.ERROR)
        self.assertEqual(result.reason_codes, ("SERVICE_ERROR",))
        self.assertEqual(self.review("x" * 200_001).reason_codes, ("INVALID_FILE",))

    def test_ambiguous_report_names_cannot_skip_metrics(self):
        self.assignment["required_files"] = ["lab-1.md"]
        self.assignment["template_metrics"]["report_file"] = "lab-1.md"
        files = tuple(ChangedFile(REPORT.replace("Lab1.md", name), "added", content=TEMPLATE)
                      for name in ("lab-1.md", "lab‑1.md"))
        self.assertEqual(self.review(files=files).reason_codes, ("INVALID_FILE",))

    def test_passing_metrics_does_not_override_ai_failure(self):
        self.course.data["features"]["ai_review"] = True
        self.ai.review.return_value = AIOutcome(
            decision=Decision.FAIL, summary="内容未完成",
            issues=(Issue(code=ReasonCode.AI_REJECTED, message="未作答"),),
        )
        result = self.review()
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertIn("AI_REJECTED", result.reason_codes)
        self.ai.review.assert_called_once()

    def test_stale_head_does_not_run_metrics(self):
        self.assertEqual(self.review(current_head_sha="d" * 40).reason_codes, ("STALE_HEAD_SHA",))
        self.github.text_file.assert_not_called()


class MetricsConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = yaml.safe_load((ROOT / "examples/course-review.yml").read_text())
        self.assignment = self.data["assignments"]["Lab1"]
        self.assignment["report_template"] = "homework/Lab1/Lab1.md"
        self.assignment["template_metrics"] = {"enabled": True, "report_file": "Lab1.md"}

    def load(self, data=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(self.data if data is None else data))
            return load_course_config(path)

    def test_enabled_and_disabled_valid_configs(self):
        self.load()
        self.assignment["template_metrics"] = {"enabled": False}
        self.load()

    def test_global_setting_is_rejected(self):
        for scope in ("defaults", "features"):
            data = copy.deepcopy(self.data)
            data[scope]["template_metrics"] = {"enabled": True, "report_file": "Lab1.md"}
            with self.subTest(scope=scope), self.assertRaises(ConfigurationError):
                self.load(data)

    def test_unsafe_missing_or_non_markdown_configuration_is_rejected(self):
        for field, values in (
            ("report_file", [None, "../Lab1.md", "/Lab1.md", "Lab1.txt", "other.md"]),
            ("characters", [[], ["#", "#"], ["＃"], ["##"], [" "], "#"]),
            ("enabled", ["true", 1]),
            ("max_file_bytes", [0, 1_000_001]),
        ):
            for value in values:
                data = copy.deepcopy(self.data)
                settings = data["assignments"]["Lab1"]["template_metrics"]
                if value is None:
                    settings.pop(field)
                else:
                    settings[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                    self.load(data)
        self.assignment.pop("report_template")
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_shipped_phase_one_example_is_valid(self):
        load_course_config(ROOT / "examples/template-metrics.yml")


if __name__ == "__main__":
    unittest.main()
