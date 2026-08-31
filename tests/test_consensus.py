from __future__ import annotations

import unittest

from course_pr_reviewer.ai import AIOutcome
from course_pr_reviewer.consensus import TextConsensusReviewer
from course_pr_reviewer.exceptions import (
    ProviderConfigurationError,
    ProviderUnavailableError,
    ReviewSystemError,
)
from course_pr_reviewer.models import Decision, Issue, ReasonCode


def outcome(decision: Decision, provider: str) -> AIOutcome:
    issues = ()
    if decision is Decision.FAIL:
        issues = (
            Issue(
                code=ReasonCode.AI_REJECTED,
                message=f"{provider} 发现明确问题",
                file="Lab1.md",
                evidence=f"{provider}-evidence",
            ),
        )
    elif decision is Decision.MANUAL_REVIEW:
        issues = (
            Issue(
                code=ReasonCode.AI_UNCERTAIN,
                message=f"{provider} 无法确认",
            ),
        )
    return AIOutcome(
        decision=decision,
        summary=f"{provider}: {decision.value}",
        issues=issues,
        confidence=0.95,
        metadata={"provider": provider},
    )


class ScriptedReviewer:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def review(self, *args, **kwargs):
        self.calls.append(kwargs.get("reconsideration"))
        index = min(len(self.calls) - 1, len(self.script) - 1)
        value = self.script[index]
        if isinstance(value, Exception):
            raise value
        return value


class ConsensusTests(unittest.TestCase):
    def review(self, glm, gemini, *, rounds=3):
        reviewer = TextConsensusReviewer(
            {"glm": glm, "gemini": gemini}, max_rounds=rounds
        )
        return reviewer.review(object(), "Lab1", object())

    def test_both_pass(self):
        result = self.review(
            ScriptedReviewer(outcome(Decision.PASS, "glm")),
            ScriptedReviewer(outcome(Decision.PASS, "gemini")),
        )
        self.assertEqual(result.decision, Decision.PASS)
        self.assertFalse(result.metadata["consensus"]["degraded"])

    def test_both_fail_merge_reasons(self):
        result = self.review(
            ScriptedReviewer(outcome(Decision.FAIL, "glm")),
            ScriptedReviewer(outcome(Decision.FAIL, "gemini")),
        )
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(len(result.issues), 2)

    def test_both_manual_go_directly_to_manual_review(self):
        result = self.review(
            ScriptedReviewer(outcome(Decision.MANUAL_REVIEW, "glm")),
            ScriptedReviewer(outcome(Decision.MANUAL_REVIEW, "gemini")),
        )
        self.assertEqual(result.decision, Decision.MANUAL_REVIEW)
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 1)

    def test_disagreement_can_converge_on_second_round(self):
        glm = ScriptedReviewer(
            outcome(Decision.PASS, "glm"), outcome(Decision.FAIL, "glm")
        )
        gemini = ScriptedReviewer(
            outcome(Decision.FAIL, "gemini"), outcome(Decision.FAIL, "gemini")
        )
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 2)
        self.assertIsNone(glm.calls[0])
        self.assertEqual(glm.calls[1]["previous_round"], 1)

    def test_three_round_disagreement_requires_manual_review(self):
        glm = ScriptedReviewer(outcome(Decision.PASS, "glm"))
        gemini = ScriptedReviewer(outcome(Decision.FAIL, "gemini"))
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.MANUAL_REVIEW)
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 3)
        self.assertIn("3 轮", result.issues[0].message)
        self.assertEqual(len(glm.calls), 3)
        self.assertEqual(len(gemini.calls), 3)

    def test_every_mixed_decision_pair_retries_then_requires_manual_review(self):
        pairs = (
            (Decision.PASS, Decision.FAIL),
            (Decision.FAIL, Decision.PASS),
            (Decision.PASS, Decision.MANUAL_REVIEW),
            (Decision.MANUAL_REVIEW, Decision.PASS),
            (Decision.FAIL, Decision.MANUAL_REVIEW),
            (Decision.MANUAL_REVIEW, Decision.FAIL),
        )
        for glm_decision, gemini_decision in pairs:
            with self.subTest(glm=glm_decision, gemini=gemini_decision):
                glm = ScriptedReviewer(outcome(glm_decision, "glm"))
                gemini = ScriptedReviewer(outcome(gemini_decision, "gemini"))
                result = self.review(glm, gemini)
                self.assertEqual(result.decision, Decision.MANUAL_REVIEW)
                self.assertEqual(len(glm.calls), 3)
                self.assertEqual(len(gemini.calls), 3)

    def test_one_temporarily_unavailable_uses_working_result(self):
        result = self.review(
            ScriptedReviewer(ProviderUnavailableError("glm timeout")),
            ScriptedReviewer(outcome(Decision.PASS, "gemini")),
        )
        self.assertEqual(result.decision, Decision.PASS)
        consensus = result.metadata["consensus"]
        self.assertTrue(consensus["degraded"])
        self.assertEqual(consensus["unavailable_providers"], ["glm"])

    def test_one_unavailable_can_still_reject(self):
        result = self.review(
            ScriptedReviewer(ProviderUnavailableError("glm timeout")),
            ScriptedReviewer(outcome(Decision.FAIL, "gemini")),
        )
        self.assertEqual(result.decision, Decision.FAIL)

    def test_one_unavailable_preserves_manual_review(self):
        result = self.review(
            ScriptedReviewer(ProviderUnavailableError("glm timeout")),
            ScriptedReviewer(outcome(Decision.MANUAL_REVIEW, "gemini")),
        )
        self.assertEqual(result.decision, Decision.MANUAL_REVIEW)

    def test_both_unavailable_pause_review(self):
        with self.assertRaisesRegex(ReviewSystemError, "两个审核通道均不可用"):
            self.review(
                ScriptedReviewer(ProviderUnavailableError("glm timeout")),
                ScriptedReviewer(ProviderUnavailableError("gemini timeout")),
            )

    def test_configuration_error_never_falls_back(self):
        with self.assertRaises(ProviderConfigurationError):
            self.review(
                ScriptedReviewer(ProviderConfigurationError("bad glm key")),
                ScriptedReviewer(outcome(Decision.PASS, "gemini")),
            )


if __name__ == "__main__":
    unittest.main()
