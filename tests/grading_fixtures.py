from __future__ import annotations

import atexit
import copy
import datetime as dt
import shutil
import tempfile
from pathlib import Path

import yaml

from course_pr_reviewer.config import load_course_config, load_student_roster
from course_pr_reviewer.snapshot import ChangedFile, PullRequestSnapshot

TZ = dt.timezone(dt.timedelta(hours=8))
DEADLINE = "2026-11-21T00:00:00+08:00"
SHA = "a" * 40

BANDS = [
    {"max_late_hours": 0, "cap_percent": 100},
    {"max_late_hours": 48, "cap_percent": 90},
    {"max_late_hours": 168, "cap_percent": 75},
    {"max_late_hours": 336, "cap_percent": 60},
    {"max_late_hours": 672, "cap_percent": 40},
    {"max_late_hours": None, "cap_percent": 20},
]


def course_data(
    *,
    mode: str = "tiered",
    deadline: str = DEADLINE,
    max_points: str = "10.00",
    legacy_points: str | None = None,
    grace_enabled: bool = True,
    grace_hours: int = 48,
    final_accept_at: str | None = None,
    bands=None,
    after_final_accept: str = "reject_merge",
) -> dict:
    course = {
        "name": "测试课程",
        "id": "test-course",
        "timezone": "Asia/Shanghai",
        "title_template": "[{student_id}{student_name}]{assignment_id}作业提交",
        "submission_path_template": "{student_id}{student_name}/{assignment_id}",
        "merge_method": "merge",
    }
    if final_accept_at is not None:
        course["final_accept_at"] = final_accept_at
    return {
        "version": 1,
        "course": course,
        "features": {
            "auto_merge": True,
            "comment_review": True,
            "close_late_pr": False,
            "ai_review": False,
            "ai_feedback": False,
            "ocr_review": False,
            "vision_review": False,
            "metrics": False,
            "grading": True,
        },
        "defaults": {"min_nonempty_lines": 3, "allow_extra_files": True},
        "grading": {
            "storage": {
                "branch": "grades",
                "schema_version": 1,
                "write_receipts": True,
                "export_on_finalize": True,
            },
            "policy_version": "2026-fall-v1",
            "defaults": {
                "max_points": max_points,
                "legacy": {
                    "record_completion": True,
                    "fixed_points": legacy_points,
                },
                "tiered": {
                    "grace": {
                        "enabled": grace_enabled,
                        "hours": grace_hours,
                        "require_substantive_snapshot": True,
                    },
                    "bands": copy.deepcopy(bands or BANDS),
                    "after_final_accept": after_final_accept,
                },
            },
        },
        "assignments": {
            "Lab1": {
                "enabled": True,
                "deadline": deadline,
                "required_files": ["Lab1.md"],
                "report_template": "homework/Lab1/Lab1.md",
                "min_nonempty_lines": 3,
                "allow_extra_files": True,
                "grading": (
                    {"mode": mode, "max_points": max_points}
                    if mode == "tiered"
                    else {"mode": mode}
                ),
            }
        },
    }


ROSTER = {
    "version": 1,
    "students": {
        "2023010102": {"name": "刘西莹", "github": "example-user"},
        "2023010103": {"name": "张三", "github": "second-user"},
    }
}


class GradingFixture:
    """Builds a real CourseConfiguration through the normal loader."""

    def __init__(self, data: dict | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cpr-grading-"))
        atexit.register(shutil.rmtree, self.tmp, True)
        root = self.tmp
        (root / "course-review.yml").write_text(
            yaml.safe_dump(data if data is not None else course_data(), allow_unicode=True),
            encoding="utf-8",
        )
        (root / "students.yml").write_text(
            yaml.safe_dump(ROSTER, allow_unicode=True), encoding="utf-8"
        )
        self.config_path = root / "course-review.yml"
        self.students_path = root / "students.yml"
        self.root = root

    def course(self):
        return load_course_config(self.config_path)

    def roster(self):
        return load_student_roster(self.students_path)

    def snapshot(self, *, at: str = "2026-11-20T20:30:00+08:00", files=None, **overrides):
        values = {
            "repository": "teacher/course",
            "number": 7,
            "title": "[2023010102刘西莹]Lab1作业提交",
            "author_login": "example-user",
            "captured_head_sha": SHA,
            "current_head_sha": SHA,
            "event_at": dt.datetime.fromisoformat(at),
            "files": files
            if files is not None
            else (
                ChangedFile(
                    "2023010102刘西莹/Lab1/Lab1.md",
                    "added",
                    blob_sha="1" * 40,
                    content="第一行\n第二行\n第三行\n第四行\n",
                ),
            ),
            "base_sha": "b" * 40,
        }
        values.update(overrides)
        return PullRequestSnapshot(**values)


class FakeGitHub:
    """Minimal stand-in for the read-only GitHub calls the grader makes."""

    def __init__(
        self,
        blobs: dict[str, bytes] | None = None,
        files: dict[str, str] | None = None,
        present: dict[str, bool] | None = None,
    ):
        self.blobs = blobs or {}
        self.files = files or {}
        self.present = present or {}

    def blob_bytes(self, repository, sha, *, max_bytes):
        return self.blobs[sha]

    def text_file(self, repository, path, ref, *, max_bytes):
        return self.files.get(path)

    def path_present(self, repository, path, ref):
        return self.present.get(path, False)


def teammate(fixture: GradingFixture, name: str = "second-user"):
    return fixture.roster().find_by_github(name)
