"""Glue between a reviewed PR snapshot and the grading policy.

The reviewer decides whether the *content* is acceptable. This module decides
what the submission is worth, using only trusted inputs:

* the server-side time of the reviewed head (``snapshot.event_at``);
* the course configuration from the default branch;
* the claims already stored in the ``grades`` branch.

Nothing here may look at commit author dates, PR comments or student-provided
files for timing or scoring.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..config import CourseConfiguration, Student
from ..exceptions import ContentLimitExceeded, ReviewSystemError
from ..snapshot import PullRequestSnapshot
from . import records
from .eligibility import EligibilityVerdict, FileFact, judge_claim
from .policy import GradeDecision, compute_grade
from .store import GradeStore

CLAIM_RULE_VERSION = "v1"


def config_digest(course: CourseConfiguration) -> str:
    """Stable hash of the grading-relevant configuration snapshot."""
    payload = json.dumps(
        {
            "course": course.data.get("course"),
            "grading": course.grading,
            "assignments": {
                key: value.get("grading")
                for key, value in course.assignments.items()
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GradingOutcome:
    decision: GradeDecision
    claim_status: str | None = None
    claim_written: bool = False
    claim_event_at: str | None = None
    blocking_issue: tuple[str, str] | None = None


class GradingContext:
    """Per-review grading helper; cheap to construct, safe to reuse."""

    def __init__(
        self,
        *,
        course: CourseConfiguration,
        assignment_id: str,
        assignment: dict[str, Any],
        student: Student,
        snapshot: PullRequestSnapshot,
        github=None,
        store: GradeStore | None = None,
        workflow_run_id: int | None = None,
    ) -> None:
        self.course = course
        self.assignment_id = assignment_id
        self.assignment = assignment
        self.student = student
        self.snapshot = snapshot
        self.github = github
        self.store = store
        self.workflow_run_id = workflow_run_id
        self.settings = course.assignment_grade_config(assignment_id)
        self.course_id = str(
            course.data.get("course", {}).get("id")
            or course.name.strip().replace(" ", "-")
        )

    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        return str(self.settings["mode"])

    @property
    def deadline(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.assignment["deadline"])

    def _grade(self, claim: dict[str, Any] | None) -> GradeDecision:
        return compute_grade(
            mode=self.mode,
            max_points=self.settings["max_points"],
            deadline=self.deadline,
            effective_submission_at=self.snapshot.event_at,
            grace_claim=claim,
            grace_hours=int(self.settings["grace_hours"]),
            final_accept_at=self.course.final_accept_at,
            policy=self.settings["policy"],
        )

    def preliminary(self) -> GradingOutcome:
        """Score without the correction window, for early failure comments."""
        return GradingOutcome(decision=self._grade(None))

    # ------------------------------------------------------------------
    def _blob_loader(self):
        github = self.github
        if github is None:
            return None
        limit = max(
            int(self.course.ai.get("max_file_bytes", 200_000)),
            int(self.course.vision.get("max_image_bytes", 5_000_000)),
        )
        total_limit = int(self.course.vision.get("max_total_bytes", 12_000_000))
        spent = {"bytes": 0}

        def load(sha: str) -> bytes:
            raw = github.blob_bytes(
                self.snapshot.repository, sha, max_bytes=limit
            )
            spent["bytes"] += len(raw)
            if spent["bytes"] > total_limit:
                raise ContentLimitExceeded("资格核验读取的文件总大小超过课程上限")
            return raw

        return load

    def _template_texts(self) -> dict[str, str]:
        template = self.assignment.get("report_template")
        github = self.github
        if not template or github is None or self.snapshot.base_sha is None:
            return {}
        try:
            text = github.text_file(
                self.snapshot.repository,
                template,
                self.snapshot.base_sha,
                max_bytes=int(self.course.ai.get("max_file_bytes", 200_000)),
            )
        except ReviewSystemError:
            return {}
        relative = template.rsplit("/", 1)[-1]
        return {f"{self.course.expected_submission_dir(self.student, self.assignment_id)}/{relative}": text} if text else {}

    def _verdict(self) -> EligibilityVerdict:
        expected_dir = self.course.expected_submission_dir(
            self.student, self.assignment_id
        )
        files = tuple(
            FileFact(path=item.filename, status=item.status, blob_sha=item.blob_sha)
            for item in self.snapshot.files
        )
        return judge_claim(
            files=files,
            assignment=self.assignment,
            expected_prefix=expected_dir,
            template_texts=self._template_texts(),
            load_blob=self._blob_loader(),
            require_substantive=bool(self.settings["require_substantive_snapshot"]),
        )

    def _stored_claim(self) -> dict[str, Any] | None:
        if self.store is None:
            raise ReviewSystemError("缺少 grades 存储，无法确认修正期资格")
        return self.store.read_json(
            records.claim_path(self.assignment_id, self.student.student_id)
        )

    def _write_claim(self, verdict: EligibilityVerdict) -> dict[str, Any]:
        if self.store is None or not verdict.eligible:
            raise ReviewSystemError("无法写入修正期 claim")
        claim = records.build_claim(
            course_id=self.course_id,
            assignment_id=self.assignment_id,
            student_id=self.student.student_id,
            github_login=self.student.github,
            pr_number=self.snapshot.number,
            head_sha=self.snapshot.current_head_sha,
            event_at=self.snapshot.event_at,
            files=verdict.files,
            rule_version=CLAIM_RULE_VERSION,
            workflow_run_id=self.workflow_run_id,
        )
        self.store.write_json(
            records.claim_path(self.assignment_id, self.student.student_id),
            claim,
            f"feat: 记录 {self.student.student_id} {self.assignment_id} 截止前有效提交",
        )
        return claim

    def _outcome(
        self,
        claim: dict[str, Any] | None,
        claim_status: str | None,
        claim_written: bool,
    ) -> GradingOutcome:
        decision = self._grade(claim)
        blocking = None
        if decision.merge_blocked:
            blocking = (
                "AFTER_FINAL_ACCEPT",
                "本次提交晚于课程最终收件截止时间，不再计分，也不会自动合并。",
            )
        return GradingOutcome(
            decision=decision,
            claim_status=claim_status,
            claim_written=claim_written,
            claim_event_at=str(claim.get("event_at")) if claim else None,
            blocking_issue=blocking,
        )

    def finalise(self) -> GradingOutcome:
        """Score the passing head, creating a claim when the rules allow it."""
        claim: dict[str, Any] | None = None
        claim_status: str | None = None
        claim_written = False

        # 读已有 claim 与创建新 claim 是两件事：迟交的修正提交必须能用上
        # 截止前留下的 claim，但它自己不能制造 claim。
        if (
            self.mode == "tiered"
            and self.settings["grace_enabled"]
            and int(self.settings["grace_hours"]) > 0
        ):
            if self.store is None:
                # 没有存储就不能凭空认定资格：截止前的提交必须能留下证据，
                # 否则学生之后会莫名丢掉 100%，这里宁可报错重试。
                if self.snapshot.event_at <= self.deadline:
                    raise ReviewSystemError("缺少 grades 存储，无法记录截止前的有效提交")
                return self._outcome(None, "UNAVAILABLE", False)
            stored = self._stored_claim()
            if stored is not None:
                claim = stored
                claim_status = str(stored.get("eligibility", "UNKNOWN"))
            elif self.snapshot.event_at <= self.deadline:
                verdict = self._verdict()
                claim_status = verdict.status
                if verdict.eligible:
                    claim = self._write_claim(verdict)
                    claim_written = True
                # UNKNOWN：无法证明截止前的提交是实质性的，本次不给修正期，
                # 按真实迟交档计分，但不伪造资格。
        return self._outcome(claim, claim_status, claim_written)


__all__ = ["GradingContext", "GradingOutcome", "config_digest"]
