"""Record shapes and path rules for the ``grades`` branch.

The branch is the durable fact store: one claim per (student, assignment) for the
pre-deadline correction window, one receipt per (student, assignment) for the
final outcome, teacher overrides kept separate, and reports that can always be
regenerated from those facts. Paths are built only from validated identifiers so
no student-controlled text can escape the directory it belongs to.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from typing import Any

SCHEMA_VERSION = 1
SEGMENT_RE = re.compile(r"^[0-9A-Za-z_-]{1,40}$")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,49}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RECORD_STATUSES = ("PENDING_ACCEPTANCE", "FINALIZED", "ERROR")
STUDENT_STATUSES = (
    "SCORED",
    "COMPLETED_UNGRADED",
    "NOT_SUBMITTED",
    "PENDING",
    "AFTER_FINAL_ACCEPT",
)

REPORTS = {
    "grades": "reports/grades.csv",
    "submissions": "reports/submissions.csv",
    "summary": "reports/summary.json",
    "errors": "reports/report_errors.json",
    "pending": "reports/pending.csv",
    "reconcile": "reports/reconcile.json",
}
METADATA_PATH = "metadata.json"
README_PATH = "README.md"


class RecordError(ValueError):
    """A claim or receipt violates the documented schema."""


def segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not SEGMENT_RE.fullmatch(value):
        raise RecordError(f"{label} 含有不安全字符：{value!r}")
    return value


def student_key(student_id: str) -> str:
    return segment(student_id, "student_id")


def assignment_key(assignment_id: str) -> str:
    if not isinstance(assignment_id, str) or not ASSIGNMENT_RE.fullmatch(
        assignment_id
    ):
        raise RecordError(f"assignment_id 无效：{assignment_id!r}")
    return assignment_id


def claim_path(assignment_id: str, student_id: str) -> str:
    return f"claims/{assignment_key(assignment_id)}/{student_key(student_id)}.json"


def receipt_path(assignment_id: str, student_id: str) -> str:
    return f"receipts/{assignment_key(assignment_id)}/{student_key(student_id)}.json"


def override_path(assignment_id: str, student_id: str) -> str:
    return f"overrides/{assignment_key(assignment_id)}/{student_key(student_id)}.json"


def audit_path(moment: dt.datetime, event_key: str) -> str:
    month = moment.astimezone(dt.UTC).strftime("%Y-%m")
    return f"audit/{month}/{segment(event_key, 'event_key')}.json"


def _iso(moment: dt.datetime | str) -> str:
    if isinstance(moment, str):
        return moment
    return moment.isoformat()


def build_claim(
    *,
    course_id: str,
    assignment_id: str,
    student_id: str,
    github_login: str,
    pr_number: int,
    head_sha: str,
    event_at: dt.datetime | str,
    files: Iterable[dict[str, str]],
    rule_version: str = "v1",
    workflow_run_id: int | None = None,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(str(head_sha)):
        raise RecordError("claim 的 head_sha 无效")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise RecordError("claim 的 pr_number 无效")
    record = {
        "schema_version": SCHEMA_VERSION,
        "course_id": course_id,
        "assignment_id": assignment_key(assignment_id),
        "student_id": student_key(student_id),
        "github_login": github_login,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "event_at": _iso(event_at),
        "eligibility": "ELIGIBLE",
        "eligibility_rule_version": rule_version,
        "files": [dict(item) for item in files],
        "verified_from": "trusted_gitHub_event_and_exact_sha",
        "source_workflow_run_id": workflow_run_id,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    return record


def claim_is_earlier(
    existing: dict[str, Any] | None, candidate: dict[str, Any]
) -> bool:
    """True when the existing claim must be kept (earliest evidence wins)."""
    if not isinstance(existing, dict):
        return False
    if existing.get("eligibility") != "ELIGIBLE":
        return False
    try:
        return dt.datetime.fromisoformat(str(existing["event_at"])) <= dt.datetime.fromisoformat(
            str(candidate["event_at"])
        )
    except (KeyError, ValueError):
        return False


def build_receipt(
    *,
    course_id: str,
    assignment_id: str,
    student_id: str,
    github_login: str,
    pr_number: int,
    reviewed_head_sha: str,
    grading: dict[str, Any],
    record_status: str,
    review_decision: str = "PASS",
    merge_commit_sha: str | None = None,
    accepted_at: dt.datetime | str | None = None,
    grading_config_sha256: str | None = None,
    source_workflow_run_id: int | None = None,
    backfilled: bool = False,
    merged: bool = False,
) -> dict[str, Any]:
    if record_status not in RECORD_STATUSES:
        raise RecordError(f"未知的 record_status：{record_status}")
    for label, value in (
        ("reviewed_head_sha", reviewed_head_sha),
        ("merge_commit_sha", merge_commit_sha),
    ):
        if value is not None and not SHA_RE.fullmatch(str(value)):
            raise RecordError(f"{label} 无效")
    if merge_commit_sha and record_status == "FINALIZED" and not merged:
        raise RecordError("FINALIZED 记录必须对应已合并的 PR")
    return {
        "schema_version": SCHEMA_VERSION,
        "course_id": course_id,
        "assignment_id": assignment_key(assignment_id),
        "student_id": student_key(student_id),
        "github_login": github_login,
        "pr_number": pr_number,
        "reviewed_head_sha": reviewed_head_sha,
        "merge_commit_sha": merge_commit_sha,
        "review_decision": review_decision,
        "record_status": record_status,
        "grading_mode": grading.get("mode"),
        "deadline": grading.get("deadline"),
        "effective_submission_at": grading.get("effective_submission_at"),
        "accepted_at": _iso(accepted_at) if accepted_at else None,
        "late_seconds": grading.get("late_seconds", 0),
        "time_status": grading.get("time_status"),
        "grace_applied": bool(grading.get("grace_applied", False)),
        "score_cap_percent": grading.get("cap_percent"),
        "max_points": grading.get("max_points"),
        "awarded_points": grading.get("awarded_points"),
        "policy_version": grading.get("policy_version"),
        "grading_config_sha256": grading_config_sha256,
        "source_workflow_run_id": source_workflow_run_id,
        "backfilled": bool(backfilled),
        "merged": bool(merged),
    }


REQUIRED_RECEIPT_FIELDS = (
    "schema_version",
    "course_id",
    "assignment_id",
    "student_id",
    "pr_number",
    "reviewed_head_sha",
    "review_decision",
    "record_status",
    "grading_mode",
    "policy_version",
)


def validate_receipt(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RecordError("收据必须是 JSON 对象")
    missing = [key for key in REQUIRED_RECEIPT_FIELDS if key not in record]
    if missing:
        raise RecordError("收据缺少字段：" + "、".join(missing))
    if record["schema_version"] != SCHEMA_VERSION:
        raise RecordError(f"收据 schema_version 不支持：{record['schema_version']}")
    assignment_key(str(record["assignment_id"]))
    student_key(str(record["student_id"]))
    if record["record_status"] not in RECORD_STATUSES:
        raise RecordError(f"收据 record_status 无效：{record['record_status']}")
    if record.get("grading_mode") not in {"legacy", "tiered"}:
        raise RecordError(f"收据 grading_mode 无效：{record.get('grading_mode')}")
    return record


def override_is_valid(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("approved_by"), str)
        and bool(record["approved_by"].strip())
        and isinstance(record.get("reason_code"), str)
        and bool(record["reason_code"].strip())
    )
