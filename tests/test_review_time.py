from __future__ import annotations

import copy
import datetime as dt
import json
import unittest
from dataclasses import replace
from pathlib import Path

from course_pr_reviewer.config import CourseConfiguration, load_course_config
from course_pr_reviewer.review_time import review_time_context, review_time_prompt
from course_pr_reviewer.snapshot import PullRequestSnapshot, snapshot_from_dict

ROOT = Path(__file__).parents[1]


class ReviewTimeTests(unittest.TestCase):
    def setUp(self):
        self.course = CourseConfiguration(copy.deepcopy(
            load_course_config(ROOT / "examples/course-review.yml").data
        ))
        self.snapshot = PullRequestSnapshot(
            repository="teacher/course", number=70,
            title="[2023010102刘西莹]Lab1作业提交", author_login="example-user",
            captured_head_sha="a" * 40, current_head_sha="a" * 40,
            event_at=dt.datetime.fromisoformat("2026-09-10T02:00:00+00:00"),
            reviewed_at=dt.datetime.fromisoformat("2026-09-11T01:00:00+00:00"),
            files=(),
        )

    def test_current_year_comes_from_runtime_with_submission_time_kept_separate(self):
        context = review_time_context(self.course, self.snapshot, "Lab1")
        self.assertEqual(context["current_year"], 2026)
        self.assertEqual(context["current_date"], "2026-09-11")
        self.assertEqual(context["submission_date"], "2026-09-10")
        self.assertEqual(context["head_pushed_at_local"], "2026-09-10T10:00:00+08:00")
        prompt = review_time_prompt(self.course, self.snapshot, "Lab1")
        provided = json.loads(prompt.split("\n")[2])
        self.assertEqual(provided, context)

    def test_year_rollover_uses_the_course_timezone_without_a_hardcoded_year(self):
        snapshot = replace(
            self.snapshot,
            reviewed_at=dt.datetime.fromisoformat("2026-12-31T16:05:00+00:00"),
            event_at=dt.datetime.fromisoformat("2026-12-31T15:59:00+00:00"),
        )
        local = review_time_context(self.course, snapshot)
        self.assertEqual(local["current_year"], 2027)
        self.assertEqual(local["current_date"], "2027-01-01")
        self.assertEqual(local["submission_date"], "2026-12-31")
        self.course.data["course"]["timezone"] = "UTC"
        utc = review_time_context(self.course, snapshot)
        self.assertEqual(utc["current_year"], 2026)
        self.assertEqual(utc["current_date"], "2026-12-31")

    def test_later_rerun_preserves_submission_time_and_assignment_deadline(self):
        first = review_time_context(self.course, self.snapshot, "Lab1")
        later = replace(self.snapshot, reviewed_at=self.snapshot.reviewed_at + dt.timedelta(days=30))
        rerun = review_time_context(self.course, later, "Lab1")
        self.assertNotEqual(first["current_date"], rerun["current_date"])
        for field in ("submission_date", "head_pushed_at_utc", "assignment_deadline_local"):
            self.assertEqual(first[field], rerun[field])
        self.assertEqual(rerun, review_time_context(self.course, later, "Lab1"))

    def test_snapshot_input_cannot_override_the_review_clock(self):
        before = dt.datetime.now(dt.UTC)
        snapshot = snapshot_from_dict({
            "repository": "teacher/course", "number": 1,
            "title": "Lab1", "author_login": "example-user",
            "captured_head_sha": "a" * 40, "current_head_sha": "a" * 40,
            "event_at": "2026-09-10T10:00:00+08:00", "files": [],
            "reviewed_at": "2000-01-01T00:00:00+00:00",
            "current_year": 2000,
        })
        after = dt.datetime.now(dt.UTC)
        self.assertLessEqual(before, snapshot.reviewed_at)
        self.assertLessEqual(snapshot.reviewed_at, after)
        self.assertEqual(snapshot.reviewed_at.utcoffset(), dt.timedelta())


if __name__ == "__main__":
    unittest.main()
