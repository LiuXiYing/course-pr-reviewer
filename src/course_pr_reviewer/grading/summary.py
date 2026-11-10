"""Per-student running score, for the PR comment after a merge.

Only reads what is already stored in ``grades``: finalized receipts plus the
current submission when its receipt is still ``PENDING_ACCEPTANCE``. The number
is therefore reproducible from the branch alone — it is not a separate ledger.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..config import CourseConfiguration
from . import records
from .store import GradeStore


def _points(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - unparsable points count as absent
        return Decimal("0.00")


def semester_summary(
    store: GradeStore | None,
    course: CourseConfiguration,
    student_id: str,
    *,
    extra: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Completed / graded / missing counts and the score rate so far.

    ``extra`` 描述本次正在审核的提交：收据还在 PENDING 时，评论也能显示本次得分
    与包含本次的累计得分率。
    """
    now = now or dt.datetime.now(dt.UTC)
    total = completed = graded = missing = not_due = pending = 0
    points = Decimal("0.00")
    possible = Decimal("0.00")
    current_assignment = (extra or {}).get("assignment_id")

    for assignment_id, assignment in course.assignments.items():
        if not assignment.get("enabled", True):
            continue
        mode = course.grade_mode(assignment_id)
        if mode is None:
            continue
        total += 1
        deadline = dt.datetime.fromisoformat(assignment["deadline"])
        record: dict[str, Any] | None = None
        if store is not None:
            try:
                record = store.read_json(records.receipt_path(assignment_id, student_id))
            except Exception:  # noqa: BLE001 - a bad receipt must not break the comment
                record = None
        if extra and assignment_id == current_assignment:
            record = {
                "record_status": "FINALIZED",
                "grading_mode": extra.get("mode", mode),
                "awarded_points": extra.get("awarded_points"),
                "max_points": extra.get("max_points"),
                "time_status": extra.get("time_status"),
            }

        status = record.get("record_status") if record else None
        if status == "PENDING_ACCEPTANCE":
            pending += 1
            completed += 1
            continue
        if record is None:
            if deadline <= now:
                missing += 1
            else:
                not_due += 1
            continue
        completed += 1
        awarded = record.get("awarded_points")
        if awarded in (None, ""):
            continue
        graded += 1
        points += _points(awarded)
        possible += _points(record.get("max_points") or "0")

    rate = None
    if possible > 0:
        rate = str((points / possible * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return {
        "assignments_total": total,
        "completed": completed,
        "graded": graded,
        "pending": pending,
        "missing": missing,
        "not_due": not_due,
        "points": f"{points.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
        "possible": f"{possible.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
        "rate_percent": rate,
    }


__all__ = ["semester_summary"]
