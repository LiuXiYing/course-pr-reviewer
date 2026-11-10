from __future__ import annotations

import copy
import unittest

from grading_fixtures import GradingFixture, course_data

from course_pr_reviewer.config import load_course_config
from course_pr_reviewer.exceptions import ConfigurationError


class GradingConfigTests(unittest.TestCase):
    def test_grading_block_is_optional_for_existing_courses(self):
        data = course_data()
        data["features"]["grading"] = False
        data.pop("grading")
        for assignment in data["assignments"].values():
            assignment.pop("grading")
        fixture = GradingFixture(data)
        course = fixture.course()
        self.assertFalse(course.grading_enabled)
        self.assertIsNone(course.grade_mode("Lab1"))

    def test_enabled_grading_requires_the_block(self):
        data = course_data()
        data.pop("grading")
        with self.assertRaises(ConfigurationError) as caught:
            GradingFixture(data).course()
        self.assertIn("缺少 grading 配置块", str(caught.exception))

    def test_every_assignment_must_declare_a_mode(self):
        data = course_data()
        data["assignments"]["Lab1"].pop("grading")
        with self.assertRaises(ConfigurationError) as caught:
            GradingFixture(data).course()
        self.assertIn("缺少 grading.mode", str(caught.exception))

    def test_legacy_must_not_carry_max_points(self):
        data = course_data(mode="legacy")
        data["assignments"]["Lab1"]["grading"]["max_points"] = "10.00"
        with self.assertRaises(ConfigurationError) as caught:
            GradingFixture(data).course()
        self.assertIn("legacy", str(caught.exception))

    def test_unknown_field_is_rejected(self):
        data = course_data()
        data["grading"]["defaults"]["tiered"]["grace"]["hour"] = 48
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_missing_timezone_is_rejected(self):
        data = course_data(deadline="2026-11-21T00:00:00")
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_final_accept_must_be_after_every_tiered_deadline(self):
        data = course_data(final_accept_at="2026-11-01T00:00:00+08:00")
        with self.assertRaises(ConfigurationError) as caught:
            GradingFixture(data).course()
        self.assertIn("final_accept_at", str(caught.exception))

    def test_final_accept_requires_timezone(self):
        data = course_data(final_accept_at="2027-01-10T23:59:59")
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_bands_need_infinite_tail(self):
        data = course_data()
        data["grading"]["defaults"]["tiered"]["bands"] = [
            {"max_late_hours": 0, "cap_percent": 100},
            {"max_late_hours": 48, "cap_percent": 90},
        ]
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_band_cap_cannot_exceed_100(self):
        data = course_data()
        data["grading"]["defaults"]["tiered"]["bands"][1]["cap_percent"] = 120
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_legacy_fixed_points_cannot_exceed_default_max(self):
        data = course_data(legacy_points="12.00")
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_grace_hours_must_be_sane(self):
        data = course_data(grace_hours=-1)
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_duplicate_yaml_keys_are_rejected(self):
        fixture = GradingFixture()
        text = fixture.config_path.read_text(encoding="utf-8")
        # 整个 course 段被复制两次：YAML 默认会静默保留最后一个。
        fixture.config_path.write_text(
            text + "\ncourse:\n  name: 另一个课程\n  timezone: Asia/Shanghai\n",
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError) as caught:
            load_course_config(fixture.config_path)
        self.assertIn("重复的键", str(caught.exception))

    def test_storage_branch_must_not_be_default_branch(self):
        data = course_data()
        data["grading"]["storage"]["branch"] = "main"
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_assignment_grade_config_resolves_defaults(self):
        fixture = GradingFixture()
        course = fixture.course()
        settings = course.assignment_grade_config("Lab1")
        self.assertEqual(settings["mode"], "tiered")
        self.assertEqual(settings["max_points"], "10.00")
        self.assertEqual(settings["grace_hours"], 48)
        self.assertTrue(settings["grace_enabled"])
        self.assertEqual(settings["policy"]["policy_version"], "2026-fall-v1")
        self.assertEqual(len(settings["policy"]["bands"]), 6)

    def test_grace_can_be_disabled(self):
        data = course_data(grace_enabled=False)
        course = GradingFixture(data).course()
        settings = course.assignment_grade_config("Lab1")
        self.assertFalse(settings["grace_enabled"])
        self.assertEqual(settings["grace_hours"], 0)

    def test_after_final_accept_values_are_restricted(self):
        data = course_data(after_final_accept="ignore")
        with self.assertRaises(ConfigurationError):
            GradingFixture(data).course()

    def test_mixed_modes_coexist(self):
        data = course_data()
        legacy = copy.deepcopy(data["assignments"]["Lab1"])
        legacy["deadline"] = "2026-10-01T00:00:00+08:00"
        legacy["grading"] = {"mode": "legacy"}
        data["assignments"]["Lab0"] = legacy
        course = GradingFixture(data).course()
        self.assertEqual(course.grade_mode("Lab0"), "legacy")
        self.assertEqual(course.grade_mode("Lab1"), "tiered")


class ExampleConfigTests(unittest.TestCase):
    def test_shipped_example_still_validates(self):
        from pathlib import Path

        root = Path(__file__).parents[1]
        course = load_course_config(root / "examples/course-review.yml")
        self.assertFalse(course.grading_enabled)

    def test_grading_example_validates(self):
        from pathlib import Path

        root = Path(__file__).parents[1]
        course = load_course_config(root / "examples/grading-review.yml")
        self.assertTrue(course.grading_enabled)
        self.assertEqual(course.grade_mode("Lab1"), "legacy")
        self.assertEqual(course.grade_mode("Lab9"), "tiered")


if __name__ == "__main__":
    unittest.main()
