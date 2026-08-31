"""Dual-provider consensus review with bounded reconsideration and safe fallback."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .ai import AIOutcome
from .config import CourseConfiguration
from .exceptions import ProviderConfigurationError, ReviewSystemError
from .models import Decision, Issue, ReasonCode
from .snapshot import PullRequestSnapshot


ReviewCall = Callable[[dict[str, Any] | None], AIOutcome]


def _deduplicate_issues(outcomes: list[AIOutcome]) -> tuple[Issue, ...]:
    unique: dict[
        tuple[ReasonCode, str, str | None, str | None, str | None, str | None],
        Issue,
    ] = {}
    for outcome in outcomes:
        for issue in outcome.issues:
            key = (
                issue.code,
                issue.message,
                issue.file,
                issue.location,
                issue.rule,
                issue.evidence,
            )
            unique.setdefault(key, issue)
    return tuple(unique.values())


def _minimum_confidence(outcomes: list[AIOutcome]) -> float | None:
    values = [
        outcome.confidence
        for outcome in outcomes
        if outcome.confidence is not None
    ]
    return min(values) if values else None


class _ConsensusReviewer:
    def __init__(
        self,
        reviewers: dict[str, Any],
        *,
        stage: str,
        max_rounds: int,
        uncertain_code: ReasonCode,
    ) -> None:
        if len(reviewers) != 2:
            raise ValueError("dual-provider consensus requires exactly two reviewers")
        if not 1 <= max_rounds <= 3:
            raise ValueError("max_rounds must be between 1 and 3")
        self.reviewers = reviewers
        self.stage = stage
        self.max_rounds = max_rounds
        self.uncertain_code = uncertain_code

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        reconsideration: dict[str, Any] | None,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        raise NotImplementedError

    @staticmethod
    def _run_round(
        calls: dict[str, ReviewCall],
        reconsideration: dict[str, Any] | None,
    ) -> tuple[dict[str, AIOutcome], dict[str, str]]:
        outcomes: dict[str, AIOutcome] = {}
        unavailable: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = {
                provider: executor.submit(call, reconsideration)
                for provider, call in calls.items()
            }
            for provider, future in futures.items():
                try:
                    outcomes[provider] = future.result()
                except ProviderConfigurationError:
                    raise
                except ReviewSystemError as exc:
                    unavailable[provider] = str(exc)
        return outcomes, unavailable

    @staticmethod
    def _reconsideration(
        round_number: int, outcomes: dict[str, AIOutcome]
    ) -> dict[str, Any]:
        return {
            "previous_round": round_number,
            "instruction": "重新核对下列分歧及证据，不要直接服从先前结论。",
            "findings": [
                {
                    "reviewer": f"reviewer_{index}",
                    "decision": outcome.decision.value,
                    "summary": outcome.summary,
                    "issues": [issue.to_dict() for issue in outcome.issues],
                }
                for index, outcome in enumerate(outcomes.values(), start=1)
            ],
        }

    def _metadata(
        self,
        *,
        rounds_used: int,
        outcomes: dict[str, AIOutcome],
        unavailable: dict[str, str],
        history: list[dict[str, Any]],
        degraded: bool,
    ) -> dict[str, Any]:
        return {
            "consensus": {
                "stage": self.stage,
                "rounds_used": rounds_used,
                "max_rounds": self.max_rounds,
                "degraded": degraded,
                "provider_decisions": {
                    provider: outcome.decision.value
                    for provider, outcome in outcomes.items()
                },
                "unavailable_providers": sorted(unavailable),
                "history": history,
                "provider_metadata": {
                    provider: outcome.metadata
                    for provider, outcome in outcomes.items()
                },
            }
        }

    def review(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        *extra: str,
    ) -> AIOutcome:
        history: list[dict[str, Any]] = []
        reconsideration: dict[str, Any] | None = None
        last_outcomes: dict[str, AIOutcome] = {}
        for round_number in range(1, self.max_rounds + 1):
            calls = self._calls(
                course, assignment_id, snapshot, reconsideration, *extra
            )
            outcomes, unavailable = self._run_round(calls, reconsideration)
            history.append(
                {
                    "round": round_number,
                    "decisions": {
                        provider: outcome.decision.value
                        for provider, outcome in outcomes.items()
                    },
                    "unavailable_providers": sorted(unavailable),
                }
            )
            if not outcomes:
                details = "；".join(
                    f"{provider}: {message}"
                    for provider, message in unavailable.items()
                )
                raise ReviewSystemError(
                    f"{self.stage}的两个审核通道均不可用，已暂停合并：{details}"
                )
            if len(outcomes) == 1:
                provider, outcome = next(iter(outcomes.items()))
                metadata = self._metadata(
                    rounds_used=round_number,
                    outcomes=outcomes,
                    unavailable=unavailable,
                    history=history,
                    degraded=True,
                )
                return AIOutcome(
                    decision=outcome.decision,
                    summary=(
                        f"{self.stage}采用降级审核：仅 {provider.upper()} 可用。"
                        f"{outcome.summary}"
                    ),
                    issues=outcome.issues,
                    confidence=outcome.confidence,
                    metadata=metadata,
                )

            last_outcomes = outcomes
            decisions = {outcome.decision for outcome in outcomes.values()}
            if len(decisions) == 1:
                decision = next(iter(decisions))
                merged = list(outcomes.values())
                provider_names = " 与 ".join(
                    provider.upper() for provider in outcomes
                )
                summaries = {
                    Decision.PASS: f"{provider_names} 均认为{self.stage}可以通过。",
                    Decision.FAIL: f"{provider_names} 均认为{self.stage}不能通过。",
                    Decision.MANUAL_REVIEW: (
                        f"{provider_names} 均无法自动确认{self.stage}，需要人工审核。"
                    ),
                }
                return AIOutcome(
                    decision=decision,
                    summary=summaries[decision],
                    issues=() if decision is Decision.PASS else _deduplicate_issues(merged),
                    confidence=_minimum_confidence(merged),
                    metadata=self._metadata(
                        rounds_used=round_number,
                        outcomes=outcomes,
                        unavailable=unavailable,
                        history=history,
                        degraded=False,
                    ),
                )
            reconsideration = self._reconsideration(round_number, outcomes)

        merged = list(last_outcomes.values())
        disagreement = Issue(
            code=self.uncertain_code,
            message=(
                f"两个模型连续 {self.max_rounds} 轮意见不一致，已转交人工审核"
            ),
        )
        return AIOutcome(
            decision=Decision.MANUAL_REVIEW,
            summary=(
                f"{self.stage}经过 {self.max_rounds} 轮双模型审核仍未达成一致。"
            ),
            issues=(disagreement, *_deduplicate_issues(merged)),
            confidence=_minimum_confidence(merged),
            metadata=self._metadata(
                rounds_used=self.max_rounds,
                outcomes=last_outcomes,
                unavailable={},
                history=history,
                degraded=False,
            ),
        )


class TextConsensusReviewer(_ConsensusReviewer):
    def __init__(self, reviewers: dict[str, Any], *, max_rounds: int) -> None:
        super().__init__(
            reviewers,
            stage="文本审核",
            max_rounds=max_rounds,
            uncertain_code=ReasonCode.AI_UNCERTAIN,
        )

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        reconsideration: dict[str, Any] | None,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        return {
            provider: (
                lambda context, reviewer=reviewer: reviewer.review(
                    course,
                    assignment_id,
                    snapshot,
                    reconsideration=context,
                )
            )
            for provider, reviewer in self.reviewers.items()
        }


class VisionConsensusReviewer(_ConsensusReviewer):
    def __init__(self, reviewers: dict[str, Any], *, max_rounds: int) -> None:
        super().__init__(
            reviewers,
            stage="图片审核",
            max_rounds=max_rounds,
            uncertain_code=ReasonCode.VISION_UNCERTAIN,
        )

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        reconsideration: dict[str, Any] | None,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        if len(extra) != 1:
            raise ValueError("vision consensus requires a submission directory")
        submission_dir = extra[0]
        return {
            provider: (
                lambda context, reviewer=reviewer: reviewer.review(
                    course,
                    assignment_id,
                    snapshot,
                    submission_dir,
                    reconsideration=context,
                )
            )
            for provider, reviewer in self.reviewers.items()
        }
