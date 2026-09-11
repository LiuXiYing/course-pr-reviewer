"""Dual-provider consensus review; disagreement is downweighted to pass."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .ai import AIOutcome
from .config import CourseConfiguration
from .exceptions import ProviderConfigurationError, ReviewSystemError
from .models import Decision, Issue, ReasonCode
from .snapshot import PullRequestSnapshot


ReviewCall = Callable[[], AIOutcome]


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
    ) -> None:
        if len(reviewers) != 2:
            raise ValueError("dual-provider consensus requires exactly two reviewers")
        if not 1 <= max_rounds <= 3:
            raise ValueError("max_rounds must be between 1 and 3")
        self.reviewers = reviewers
        self.stage = stage
        # 保留配置兼容：分歧不再触发多轮复核，max_rounds 仅用于元数据展示。
        self.max_rounds = max_rounds

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        raise NotImplementedError

    @staticmethod
    def _run_round(
        calls: dict[str, ReviewCall],
    ) -> tuple[dict[str, AIOutcome], dict[str, str]]:
        outcomes: dict[str, AIOutcome] = {}
        unavailable: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(calls)) as executor:
            futures = {
                provider: executor.submit(call) for provider, call in calls.items()
            }
            for provider, future in futures.items():
                try:
                    outcomes[provider] = future.result()
                except ProviderConfigurationError:
                    raise
                except ReviewSystemError as exc:
                    unavailable[provider] = str(exc)
        return outcomes, unavailable

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
                "provider_errors": {
                    provider: unavailable[provider] for provider in sorted(unavailable)
                },
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
        calls = self._calls(course, assignment_id, snapshot, *extra)
        outcomes, unavailable = self._run_round(calls)
        history = [
            {
                "round": 1,
                "decisions": {
                    provider: outcome.decision.value
                    for provider, outcome in outcomes.items()
                },
                "unavailable_providers": sorted(unavailable),
                "provider_errors": {
                    provider: unavailable[provider] for provider in sorted(unavailable)
                },
            }
        ]
        if not outcomes:
            details = "；".join(
                f"{provider}: {message}" for provider, message in unavailable.items()
            )
            raise ReviewSystemError(
                f"{self.stage}的两个审核通道均不可用，已暂停合并：{details}"
            )
        merged = list(outcomes.values())
        rounds_used = 1
        if len(outcomes) == 1:
            provider, outcome = next(iter(outcomes.items()))
            metadata = self._metadata(
                rounds_used=rounds_used,
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

        provider_names = " 与 ".join(provider.upper() for provider in outcomes)
        decisions = {outcome.decision for outcome in outcomes.values()}
        if len(decisions) == 1:
            decision = next(iter(decisions))
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
                    rounds_used=rounds_used,
                    outcomes=outcomes,
                    unavailable=unavailable,
                    history=history,
                    degraded=False,
                ),
            )

        # 分歧按课程配置降权处理：不再多轮复核或转人工，直接视为通过。
        # 各自的结论与理由保留在 consensus 元数据中供人工追溯。
        detail = "；".join(
            f"{provider.upper()}={outcome.decision.value}"
            for provider, outcome in outcomes.items()
        )
        return AIOutcome(
            decision=Decision.PASS,
            summary=(
                f"{provider_names} 意见不一致（{detail}），"
                f"{self.stage}按配置视为通过。"
            ),
            issues=(),
            confidence=_minimum_confidence(merged),
            metadata=self._metadata(
                rounds_used=rounds_used,
                outcomes=outcomes,
                unavailable=unavailable,
                history=history,
                degraded=False,
            ),
        )


class TextConsensusReviewer(_ConsensusReviewer):
    def __init__(self, reviewers: dict[str, Any], *, max_rounds: int) -> None:
        super().__init__(reviewers, stage="文本审核", max_rounds=max_rounds)

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        return {
            provider: (
                lambda reviewer=reviewer: reviewer.review(
                    course,
                    assignment_id,
                    snapshot,
                )
            )
            for provider, reviewer in self.reviewers.items()
        }


class VisionConsensusReviewer(_ConsensusReviewer):
    def __init__(self, reviewers: dict[str, Any], *, max_rounds: int) -> None:
        super().__init__(reviewers, stage="图片审核", max_rounds=max_rounds)

    def _calls(
        self,
        course: CourseConfiguration,
        assignment_id: str,
        snapshot: PullRequestSnapshot,
        *extra: str,
    ) -> dict[str, ReviewCall]:
        if len(extra) != 1:
            raise ValueError("vision consensus requires a submission directory")
        submission_dir = extra[0]
        return {
            provider: (
                lambda reviewer=reviewer: reviewer.review(
                    course,
                    assignment_id,
                    snapshot,
                    submission_dir,
                )
            )
            for provider, reviewer in self.reviewers.items()
        }
