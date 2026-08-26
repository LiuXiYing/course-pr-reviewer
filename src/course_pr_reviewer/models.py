"""Stable decisions and result objects shared by all course repositories."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    ERROR = "ERROR"


class ReasonCode(str, Enum):
    UNKNOWN_GITHUB_USER = "UNKNOWN_GITHUB_USER"
    INACTIVE_STUDENT = "INACTIVE_STUDENT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TITLE_MISMATCH = "TITLE_MISMATCH"
    ASSIGNMENT_NOT_CONFIGURED = "ASSIGNMENT_NOT_CONFIGURED"
    NO_FILES_CHANGED = "NO_FILES_CHANGED"
    FILE_DELETED = "FILE_DELETED"
    FILE_RENAMED = "FILE_RENAMED"
    PATH_OUT_OF_SCOPE = "PATH_OUT_OF_SCOPE"
    OLD_ASSIGNMENT_MODIFIED = "OLD_ASSIGNMENT_MODIFIED"
    REQUIRED_FILE_MISSING = "REQUIRED_FILE_MISSING"
    EXTRA_FILE = "EXTRA_FILE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    LATE_PR_CLOSE_REQUIRED = "LATE_PR_CLOSE_REQUIRED"
    INVALID_FILE = "INVALID_FILE"
    CONTENT_TOO_SHORT = "CONTENT_TOO_SHORT"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    OCR_LOW_CONFIDENCE = "OCR_LOW_CONFIDENCE"
    OCR_REJECTED = "OCR_REJECTED"
    VISION_UNCERTAIN = "VISION_UNCERTAIN"
    VISION_REJECTED = "VISION_REJECTED"
    AI_REJECTED = "AI_REJECTED"
    AI_UNCERTAIN = "AI_UNCERTAIN"
    SERVICE_ERROR = "SERVICE_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    STALE_HEAD_SHA = "STALE_HEAD_SHA"
    MERGE_FAILED = "MERGE_FAILED"


@dataclass(frozen=True)
class Issue:
    code: ReasonCode
    message: str
    file: str | None = None
    location: str | None = None
    rule: str | None = None
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["code"] = self.code.value
        return {key: value for key, value in result.items() if value is not None}


@dataclass(frozen=True)
class ReviewResult:
    decision: Decision
    summary: str
    issues: tuple[Issue, ...] = ()
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("summary must not be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if self.decision is Decision.PASS and self.issues:
            raise ValueError("PASS must not contain issues")
        if (
            self.decision in {Decision.FAIL, Decision.MANUAL_REVIEW, Decision.ERROR}
            and not self.issues
        ):
            raise ValueError(f"{self.decision.value} must contain at least one issue")

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(issue.code.value for issue in self.issues))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "decision": self.decision.value,
            "summary": self.summary,
            "reason_codes": list(self.reason_codes),
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": self.metadata,
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        return result

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True
        )
