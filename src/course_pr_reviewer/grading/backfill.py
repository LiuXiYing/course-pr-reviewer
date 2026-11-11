"""补录制度上线前已经合并的作业完成情况。

上线前的 PR 没有收据，所以评论里的"已完成 N 个"只数得到上线后的提交。这里按
默认分支上真实存在的 ``学号姓名/LabN/`` 目录补一条 ``backfilled`` 收据：只记
"完成了"，不猜分数、不猜提交时间，也不改任何历史 PR。

只对 ``legacy`` 作业补录：``tiered`` 作业的分数必须来自真实审核与合并，
不能用"目录存在"推断。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import CourseConfiguration, StudentRoster
from ..snapshot import GitHubClient
from . import records
from .store import GradeStore


def _parent_dir(course: CourseConfiguration, student, assignment_id: str) -> str:
    directory = course.expected_submission_dir(student, assignment_id)
    return directory.rsplit("/", 1)[0] if "/" in directory else ""


def backfill_completions(
    store: GradeStore,
    course: CourseConfiguration,
    roster: StudentRoster,
    github: GitHubClient,
    repository: str,
    *,
    ref: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """为默认分支上已有的 legacy 作业目录补收据（幂等）。"""
    if not course.grading_enabled:
        raise ValueError("该课程未启用 grading，无需补录")
    if ref is None:
        branch = github.default_branch(repository)
        ref = github.get_json(f"/repos/{repository}/git/ref/heads/{branch}")["object"]["sha"]
    mode_of = {key: course.grade_mode(key) for key in course.assignments}
    legacy = [
        key
        for key, assignment in course.assignments.items()
        if assignment.get("enabled", True) and mode_of.get(key) == "legacy"
    ]
    created: list[dict[str, str]] = []
    existing = 0
    listed: dict[str, set[str]] = {}

    for student in sorted(roster.by_student_id.values(), key=lambda item: item.student_id):
        if not student.active:
            continue
        parent = _parent_dir(course, student, legacy[0]) if legacy else ""
        if parent and parent not in listed:
            listed[parent] = set(github.list_dir(repository, parent, ref))
        if not parent or parent not in listed:
            continue
        children = listed[parent]
        for assignment_id in legacy:
            if assignment_id not in children:
                continue
            path = records.receipt_path(assignment_id, student.student_id)
            if store.read_json(path) is not None:
                existing += 1
                continue
            record = records.build_receipt(
                course_id=str(
                    course.data.get("course", {}).get("id")
                    or course.name.strip().replace(" ", "-")
                ),
                assignment_id=assignment_id,
                student_id=student.student_id,
                github_login=student.github,
                pr_number=0,
                reviewed_head_sha=None,
                grading={
                    "mode": "legacy",
                    "deadline": course.assignments[assignment_id]["deadline"],
                    "effective_submission_at": None,
                    "time_status": "BACKFILL",
                    "late_seconds": 0,
                    "cap_percent": None,
                    "max_points": None,
                    "awarded_points": None,
                    "policy_version": course.grading_policy_version,
                },
                record_status="FINALIZED",
                review_decision="BACKFILL",
                merged=True,
                backfilled=True,
            )
            if not dry_run:
                store.write_json(
                    path,
                    record,
                    f"chore: 补录 {student.student_id} {assignment_id} 历史完成情况",
                )
            created.append(
                {"student_id": student.student_id, "assignment_id": assignment_id}
            )
    return {
        "ref": ref,
        "legacy_assignments": len(legacy),
        "students": len(listed),
        "created": len(created),
        "existing": existing,
        "dry_run": dry_run,
        "sample": created[:5],
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }


__all__ = ["backfill_completions"]
