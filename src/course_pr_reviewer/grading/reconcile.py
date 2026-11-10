"""Compensate interrupted grading writes.

A review may stop between "reviewed PASS and reserved a receipt" and "the PR was
actually merged". This module walks the ``grades`` branch, asks GitHub for the
real merge state and only then promotes ``PENDING_ACCEPTANCE`` to ``FINALIZED``.
It never merges, never re-reviews and never guesses a score.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..config import CourseConfiguration, StudentRoster
from ..snapshot import GitHubClient
from . import records
from .export import load_receipts, rebuild_reports
from .store import GradeStore

MAX_RECEIPTS_PER_RUN = 500


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _audit(
    store: GradeStore,
    *,
    pr_number: int,
    head_sha: str,
    action: str,
    detail: dict[str, Any],
) -> None:
    moment = dt.datetime.now(dt.UTC)
    key = f"pr{pr_number}-{head_sha[:12]}-{action.lower()}"
    store.write_json(
        records.audit_path(moment, key),
        {
            "at": moment.isoformat(),
            "action": action,
            "pr_number": pr_number,
            "head_sha": head_sha,
            **detail,
        },
        f"chore: 记录 {action} 审计（PR #{pr_number}）",
    )


def _reports_are_stale(store: GradeStore, receipts: dict) -> bool:
    """报表是否落后于收据：落后才重建，避免每次对账都读几百份文件。"""
    summary = store.read_json(records.REPORTS["summary"]) or {}
    generated = summary.get("generated_at")
    if not isinstance(generated, str):
        return True
    try:
        stamp = dt.datetime.fromisoformat(generated)
    except ValueError:
        return True
    for record in receipts.values():
        for key in ("accepted_at", "effective_submission_at"):
            value = record.get(key)
            if not isinstance(value, str):
                continue
            try:
                moment = dt.datetime.fromisoformat(value)
            except ValueError:
                continue
            if moment > stamp:
                return True
    return False


def reconcile(
    store: GradeStore,
    course: CourseConfiguration,
    roster: StudentRoster,
    github: GitHubClient,
    *,
    repository: str,
    since: dt.datetime | None = None,
    only_pr: int | None = None,
    rebuild: bool = True,
) -> dict[str, Any]:
    receipts, errors = load_receipts(store, course)
    finalized: list[dict[str, Any]] = []
    still_pending: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = list(errors)
    checked = 0

    for key, record in sorted(receipts.items()):
        if record.get("record_status") == "FINALIZED":
            continue
        if only_pr is not None and record.get("pr_number") != only_pr:
            continue
        if since is not None:
            stamp = record.get("effective_submission_at") or record.get("accepted_at")
            try:
                if stamp and dt.datetime.fromisoformat(str(stamp)) < since:
                    continue
            except ValueError:
                pass
        if checked >= MAX_RECEIPTS_PER_RUN:
            anomalies.append({"error": "单次对账达到收据上限，剩余记录下次继续"})
            break
        checked += 1
        number = record.get("pr_number")
        if not isinstance(number, int):
            anomalies.append(
                {"path": records.receipt_path(*key), "error": "收据缺少有效的 PR 编号"}
            )
            continue
        try:
            pr = github.pull_request(repository, number)
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            anomalies.append(
                {"path": records.receipt_path(*key), "error": f"无法读取 PR #{number}：{exc}"}
            )
            continue

        head_sha = str(pr.get("head", {}).get("sha", "")).lower()
        reviewed = str(record.get("reviewed_head_sha", "")).lower()
        merged = pr.get("merged") is True
        if merged and head_sha == reviewed:
            record = {
                **record,
                "record_status": "FINALIZED",
                "merged": True,
                "merge_commit_sha": pr.get("merge_commit_sha"),
                "accepted_at": pr.get("merged_at"),
            }
            store.write_json(
                records.receipt_path(*key),
                record,
                f"chore: 对账补记 {record['student_id']} {record['assignment_id']} 成绩",
            )
            _audit(
                store,
                pr_number=number,
                head_sha=reviewed,
                action="finalize",
                detail={"assignment_id": record.get("assignment_id"), "pr_number": number},
            )
            finalized.append(record)
            continue

        reason = (
            "PR 已关闭但未合并"
            if pr.get("state") == "closed" and not merged
            else "PR 当前 head 与已审核 head 不一致"
            if head_sha != reviewed
            else "PR 仍未合并"
        )
        still_pending.append(
            {
                "assignment_id": record.get("assignment_id"),
                "student_id": record.get("student_id"),
                "pr_number": number,
                "reason": reason,
            }
        )

    summary: dict[str, Any] | None = None
    if rebuild and (finalized or _reports_are_stale(store, receipts)):
        result = rebuild_reports(store, course, roster)
        summary = result["summary"]

    report = {
        "checked": checked,
        "finalized": len(finalized),
        "pending": still_pending,
        "anomalies": anomalies,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    if anomalies or still_pending:
        store.write_text(
            records.REPORTS["reconcile"],
            json_text(report),
            "chore: 记录对账异常与待处理记录",
        )
    elif store.read_text(records.REPORTS["reconcile"]) is not None:
        store.delete(records.REPORTS["reconcile"], "chore: 清除已修复的对账异常")

    return {**report, "reports": summary}


__all__ = ["reconcile"]
