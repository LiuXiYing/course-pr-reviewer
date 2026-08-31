from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from course_pr_reviewer.config import load_course_config, load_student_roster
from course_pr_reviewer.exceptions import ConfigurationError

ROOT = Path(__file__).parents[1]


class CourseConfigurationTests(unittest.TestCase):
    def test_empty_semester_configuration_is_valid(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["assignments"] = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            course = load_course_config(path)
        self.assertEqual(course.assignments, {})

    def test_example_configuration_loads(self):
        course = load_course_config(ROOT / "examples/course-review.yml")
        self.assertEqual(course.name, "数据结构")
        self.assertEqual(sorted(course.assignments), ["Lab1", "Lab2"])
        self.assertTrue(course.feature_enabled("auto_merge"))
        self.assertTrue(course.feature_enabled("comment_review"))
        self.assertTrue(course.feature_enabled("close_late_pr"))
        self.assertEqual(course.ai["provider"], "glm")

    def test_gemini_provider_is_supported(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["ai"].update(
            provider="gemini", model="gemini-3.5-flash-lite"
        )
        data["vision"].update(
            provider="gemini", model="gemini-3.5-flash-lite"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            course = load_course_config(path)
        self.assertEqual(course.ai["provider"], "gemini")
        self.assertEqual(course.vision["provider"], "gemini")

    def test_expected_title_uses_roster_identity(self):
        course = load_course_config(ROOT / "examples/course-review.yml")
        roster = load_student_roster(ROOT / "examples/students.yml")
        student = roster.find_by_github("EXAMPLE-USER")
        self.assertIsNotNone(student)
        self.assertEqual(
            course.expected_title(student, "Lab1"),
            "[2023010102刘西莹]Lab1作业提交",
        )

    def test_deadline_without_timezone_is_rejected(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["assignments"]["Lab1"]["deadline"] = "2026-09-20T23:59:59"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_course_config(path)

    def test_unknown_iana_timezone_is_rejected(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["course"]["timezone"] = "Asia/Not-A-Real-City"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "IANA 时区"):
                load_course_config(path)

    def test_ai_enabled_assignment_requires_review_points(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["assignments"]["Lab1"]["review_points"] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "review_points"):
                load_course_config(path)

    def test_ocr_requires_vision(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["features"]["vision_review"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "ocr_review"):
                load_course_config(path)

    def test_vision_fail_confidence_cannot_be_lower_than_minimum(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["vision"]["min_confidence"] = 0.9
        data["vision"]["fail_confidence"] = 0.8
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "fail_confidence"):
                load_course_config(path)

    def test_parent_path_is_rejected(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["assignments"]["Lab1"]["required_files"] = ["../README.md"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "不安全文件路径"):
                load_course_config(path)

    def test_current_directory_is_not_a_file_requirement(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["assignments"]["Lab1"]["required_files"] = ["."]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "不安全文件路径"):
                load_course_config(path)

    def test_title_template_requires_all_identity_fields(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["course"]["title_template"] = "[{student_name}]{assignment_id}作业提交"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "title_template"):
                load_course_config(path)

    def test_escaped_braces_do_not_count_as_template_fields(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["course"]["title_template"] = (
            "[{{student_id}}{{student_name}}]{assignment_id}作业提交"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "student_identity"):
                load_course_config(path)

    def test_combined_student_identity_placeholder_is_supported(self):
        data = yaml.safe_load(
            (ROOT / "examples/course-review.yml").read_text(encoding="utf-8")
        )
        data["course"]["title_template"] = "[{student_identity}]{assignment_id}作业提交"
        data["course"]["submission_path_template"] = (
            "{student_identity}/{assignment_id}"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "course.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            course = load_course_config(path)
        roster = load_student_roster(ROOT / "examples/students.yml")
        student = roster.find_by_github("example-user")
        self.assertEqual(
            course.expected_title(student, "Lab1"), "[2023010102刘西莹]Lab1作业提交"
        )


class StudentRosterTests(unittest.TestCase):
    def test_empty_roster_is_valid_before_import(self):
        data = {"version": 1, "students": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.yml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            roster = load_student_roster(path)
        self.assertEqual(roster.by_student_id, {})

    def test_example_roster_loads_case_insensitively(self):
        roster = load_student_roster(ROOT / "examples/students.yml")
        student = roster.find_by_github("ZhangSan-GitHub")
        self.assertEqual(student.student_id, "2023010103")

    def test_duplicate_github_login_is_rejected(self):
        data = {
            "version": 1,
            "students": {
                "2023010102": {"name": "刘西莹", "github": "Same-User"},
                "2023010103": {"name": "张三", "github": "same-user"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "同时绑定"):
                load_student_roster(path)

    def test_invalid_student_id_is_rejected(self):
        data = {
            "version": 1,
            "students": {"20230102": {"name": "张三", "github": "zhangsan"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_student_roster(path)

    def test_name_cannot_inject_a_path(self):
        data = {
            "version": 1,
            "students": {"2023010102": {"name": "../刘西莹", "github": "example-user"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "students.yml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_student_roster(path)


if __name__ == "__main__":
    unittest.main()
