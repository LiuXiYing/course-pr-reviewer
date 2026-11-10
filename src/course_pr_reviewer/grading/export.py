"""Rebuildable reports from the receipts in the ``grades`` branch.

``receipts/`` is the source of truth; every file under ``reports/`` can be
deleted and regenerated. Manual score changes go through ``overrides/`` and are
applied as a layer, so the original fact is never rewritten.

CSV conventions:

* UTF-8 with BOM so WPS/Excel read Chinese headers correctly;
* student numbers are emitted as text, never as numbers;
* ``awarded_points`` stays empty for legacy assignments that have no score, so a
  missing value can never be mistaken for 0.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from ..config import CourseConfiguration, StudentRoster
from . import records
from .store import GradeStore

BOM = "\ufeff"

STUDENT_STATUS_SCORED = "SCORED"
STUDENT_STATUS_UNGRADED = "COMPLETED_UNGRADED"
STUDENT_STATUS_NOT_SUBMITTED = "NOT_SUBMITTED"
STUDENT_STATUS_NOT_DUE = "NOT_DUE"
STUDENT_STATUS_PENDING = "PENDING"
STUDENT_STATUS_AFTER_FINAL = "AFTER_FINAL_ACCEPT"

SUBMISSION_COLUMNS = (
    "course_id",
    "student_id",
    "github_login",
    "assignment_id",
    "mode",
    "pr_number",
    "review_status",
    "deadline",
    "effective_submission_at",
    "late_seconds",
    "grace_applied",
    "cap_percent",
    "max_points",
    "awarded_points",
    "record_status",
    "rule_version",
)


def _csv_text(header: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    return BOM + buffer.getvalue()


def load_receipts(
    store: GradeStore, course: CourseConfiguration
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """Read every receipt; invalid files are reported, never silently dropped."""
    receipts: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for assignment_id in course.assignments:
        directory = f"receipts/{assignment_id}"
        for path in store.list_paths(directory):
            if not path.endswith(".json"):
                continue
            try:
                raw = store.read_json(path)
                if raw is None:
                    continue
                record = records.validate_receipt(raw)
            except (records.RecordError, ValueError) as exc:
                errors.append({"path": path, "error": str(exc)})
                continue
            key = (record["assignment_id"], record["student_id"])
            if key in receipts:
                errors.append(
                    {
                        "path": path,
                        "error": "同一学生同一作业存在多条收据",
                        "kept": records.receipt_path(*key),
                    }
                )
                continue
            receipts[key] = record
    return receipts, errors


def load_overrides(
    store: GradeStore, course: CourseConfiguration
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for assignment_id in course.assignments:
        for path in store.list_paths(f"overrides/{assignment_id}"):
            if not path.endswith(".json"):
                continue
            try:
                raw = store.read_json(path)
            except ValueError as exc:
                errors.append({"path": path, "error": str(exc)})
                continue
            if raw is None:
                continue
            if not records.override_is_valid(raw):
                errors.append(
                    {"path": path, "error": "教师覆盖记录缺少 approved_by 或 reason_code"}
                )
                continue
            overrides[(raw.get("assignment_id"), raw.get("student_id"))] = raw
    return overrides, errors


def effective_points(
    receipt: dict[str, Any] | None, override: dict[str, Any] | None
) -> str | None:
    if override is not None and override.get("new_points") is not None:
        return str(override["new_points"])
    if receipt is None:
        return None
    return receipt.get("awarded_points")


def student_status(
    receipt: dict[str, Any] | None,
    *,
    due: bool,
) -> str:
    if receipt is None:
        return STUDENT_STATUS_NOT_SUBMITTED if due else STUDENT_STATUS_NOT_DUE
    status = receipt.get("record_status")
    if status == "PENDING_ACCEPTANCE":
        return STUDENT_STATUS_PENDING
    if status == "ERROR":
        return STUDENT_STATUS_PENDING
    if receipt.get("time_status") == "AFTER_FINAL_ACCEPT":
        return STUDENT_STATUS_AFTER_FINAL
    if receipt.get("awarded_points") is None:
        return STUDENT_STATUS_UNGRADED
    return STUDENT_STATUS_SCORED


def build_submissions_csv(
    course: CourseConfiguration,
    receipts: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, Any]],
) -> str:
    rows = []
    for (assignment_id, student_id), record in sorted(receipts.items()):
        override = overrides.get((assignment_id, student_id))
        rows.append(
            [
                record.get("course_id"),
                student_id,
                record.get("github_login"),
                assignment_id,
                record.get("grading_mode"),
                record.get("pr_number"),
                record.get("review_decision"),
                record.get("deadline"),
                record.get("effective_submission_at"),
                record.get("late_seconds"),
                record.get("grace_applied"),
                record.get("score_cap_percent"),
                record.get("max_points"),
                effective_points(record, override),
                record.get("record_status"),
                record.get("policy_version"),
            ]
        )
    return _csv_text(SUBMISSION_COLUMNS, rows)


def build_pending_csv(
    receipts: dict[tuple[str, str], dict[str, Any]],
) -> str:
    header = (
        "student_id",
        "assignment_id",
        "pr_number",
        "record_status",
        "reviewed_head_sha",
    )
    rows = [
        [
            record.get("student_id"),
            record.get("assignment_id"),
            record.get("pr_number"),
            record.get("record_status"),
            record.get("reviewed_head_sha"),
        ]
        for record in receipts.values()
        if record.get("record_status") != "FINALIZED"
    ]
    return _csv_text(header, sorted(rows, key=lambda row: (row[1] or "", row[0] or "")))


def build_grades_csv(
    course: CourseConfiguration,
    roster: StudentRoster,
    receipts: dict[tuple[str, str], dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> str:
    now = now or dt.datetime.now(dt.UTC)
    assignment_ids = [
        key
        for key, value in course.assignments.items()
        if value.get("enabled", True)
    ]
    due_state: dict[str, bool] = {}
    max_points: dict[str, str | None] = {}
    for assignment_id in assignment_ids:
        deadline = dt.datetime.fromisoformat(course.assignments[assignment_id]["deadline"])
        due_state[assignment_id] = deadline <= now
        settings = course.assignment_grade_config(assignment_id)
        max_points[assignment_id] = (
            None
            if settings["mode"] == "legacy"
            else str(settings["max_points"] or "")
        )

    header = ["student_id", "github_login"]
    for assignment_id in assignment_ids:
        header.extend([f"{assignment_id}_status", f"{assignment_id}_points"])
    header.extend(
        [
            "graded_points_total",
            "graded_points_possible",
            "completion_count",
            "missing_count",
        ]
    )

    students = sorted(roster.by_student_id.values(), key=lambda item: item.student_id)
    rows: list[list[Any]] = []
    for student in students:
        row: list[Any] = [student.student_id, student.github]
        total = Decimal("0.00")
        possible = Decimal("0.00")
        completed = 0
        missing = 0
        for assignment_id in assignment_ids:
            receipt = receipts.get((assignment_id, student.student_id))
            override = overrides.get((assignment_id, student.student_id))
            status = student_status(receipt, due=due_state[assignment_id])
            points = effective_points(receipt, override)
            row.extend([status, points])
            if status in {STUDENT_STATUS_SCORED, STUDENT_STATUS_AFTER_FINAL}:
                completed += 1
                if points:
                    total += Decimal(str(points))
                    possible += Decimal(str(max_points[assignment_id] or "0"))
            elif status == STUDENT_STATUS_UNGRADED:
                completed += 1
            elif status == STUDENT_STATUS_NOT_SUBMITTED:
                missing += 1
        row.extend(
            [
                f"{total:.2f}",
                f"{possible:.2f}",
                completed,
                missing,
            ]
        )
        rows.append(row)
    return _csv_text(header, rows)


def build_summary(
    course: CourseConfiguration,
    receipts: dict[tuple[str, str], dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    per_assignment: dict[str, Any] = {}
    for assignment_id, assignment in course.assignments.items():
        configured = sum(1 for key in receipts if key[0] == assignment_id)
        finalized = sum(
            1
            for (aid, _), record in receipts.items()
            if aid == assignment_id and record.get("record_status") == "FINALIZED"
        )
        scored = sum(
            1
            for (aid, _), record in receipts.items()
            if aid == assignment_id
            and record.get("record_status") == "FINALIZED"
            and record.get("awarded_points") is not None
        )
        per_assignment[assignment_id] = {
            "mode": (assignment.get("grading") or {}).get("mode"),
            "deadline": assignment.get("deadline"),
            "receipts": configured,
            "finalized": finalized,
            "scored": scored,
        }
    return {
        "schema_version": records.SCHEMA_VERSION,
        "course_id": str(course.data.get("course", {}).get("id") or course.name),
        "generated_at": now.isoformat(),
        "assignments": per_assignment,
        "errors": len(errors),
    }


def rebuild_reports(
    store: GradeStore,
    course: CourseConfiguration,
    roster: StudentRoster,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    receipts, receipt_errors = load_receipts(store, course)
    overrides, override_errors = load_overrides(store, course)
    errors = receipt_errors + override_errors
    summary = build_summary(course, receipts, errors, now=now)
    store.write_text(
        records.REPORTS["submissions"],
        build_submissions_csv(course, receipts, overrides),
        "chore: 重新生成 submissions.csv",
    )
    store.write_text(
        records.REPORTS["grades"],
        build_grades_csv(course, roster, receipts, overrides, now=now),
        "chore: 重新生成 grades.csv",
    )
    store.write_text(
        records.REPORTS["pending"],
        build_pending_csv(receipts),
        "chore: 重新生成 pending.csv",
    )
    store.write_text(
        records.REPORTS["summary"],
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "chore: 重新生成 summary.json",
    )
    if errors:
        store.write_text(
            records.REPORTS["errors"],
            json.dumps(errors, ensure_ascii=False, indent=2) + "\n",
            "chore: 记录报表校验错误",
        )
    elif store.read_text(records.REPORTS["errors"]) is not None:
        store.delete(records.REPORTS["errors"], "chore: 清除已修复的报表错误")
    return {
        "receipts": len(receipts),
        "overrides": len(overrides),
        "errors": errors,
        "summary": summary,
    }


__all__ = [
    "build_grades_csv",
    "build_pending_csv",
    "build_submissions_csv",
    "build_summary",
    "load_overrides",
    "load_receipts",
    "rebuild_reports",
]
