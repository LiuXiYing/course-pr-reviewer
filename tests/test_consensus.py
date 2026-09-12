from __future__ import annotations

import unittest

from course_pr_reviewer.ai import AIOutcome
from course_pr_reviewer.consensus import TextConsensusReviewer, VisionConsensusReviewer
from course_pr_reviewer.exceptions import (
    ProviderConfigurationError,
    ProviderUnavailableError,
    ReviewSystemError,
    TemplateLoadError,
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
        self.arguments = []

    def review(self, *args, **kwargs):
        self.calls.append(kwargs.get("reconsideration"))
        self.arguments.append(args)
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

    def test_disagreement_passes_only_after_three_rounds_and_retains_reasons(self):
        glm = ScriptedReviewer(outcome(Decision.PASS, "glm"))
        gemini = ScriptedReviewer(outcome(Decision.FAIL, "gemini"))
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertEqual(result.issues, ())
        consensus = result.metadata["consensus"]
        self.assertEqual(consensus["rounds_used"], 3)
        self.assertTrue(consensus["disagreement_pass"])
        self.assertEqual(
            consensus["provider_decisions"], {"glm": "PASS", "gemini": "FAIL"}
        )
        self.assertEqual(len(glm.calls), 3)
        self.assertEqual(len(gemini.calls), 3)
        self.assertIsNone(glm.calls[0])
        for index, entry in enumerate(consensus["history"], 1):
            self.assertEqual(entry["round"], index)
            finding = entry["findings"]["gemini"]
            self.assertEqual(finding["issues"][0]["evidence"], "gemini-evidence")
            self.assertEqual(finding["summary"], "gemini: FAIL")
        for index in (1, 2):
            self.assertEqual(glm.calls[index]["previous_round"], index)
            self.assertEqual(glm.calls[index], gemini.calls[index])
            self.assertEqual(glm.calls[index]["findings"][1]["decision"], "FAIL")

    def test_second_round_agreement_stops_before_the_limit(self):
        glm = ScriptedReviewer(
            outcome(Decision.FAIL, "glm"), outcome(Decision.PASS, "glm")
        )
        gemini = ScriptedReviewer(outcome(Decision.PASS, "gemini"))
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 2)
        self.assertFalse(result.metadata["consensus"]["disagreement_pass"])
        self.assertEqual(len(glm.calls), 2)

    def test_third_round_agreement_on_failure_does_not_pass(self):
        glm = ScriptedReviewer(outcome(Decision.FAIL, "glm"))
        gemini = ScriptedReviewer(
            outcome(Decision.PASS, "gemini"),
            outcome(Decision.MANUAL_REVIEW, "gemini"),
            outcome(Decision.FAIL, "gemini"),
        )
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertEqual(len(result.issues), 2)
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 3)
        self.assertFalse(result.metadata["consensus"]["disagreement_pass"])

    def test_explicit_round_limit_is_respected(self):
        glm = ScriptedReviewer(outcome(Decision.FAIL, "glm"))
        gemini = ScriptedReviewer(outcome(Decision.PASS, "gemini"))
        result = self.review(glm, gemini, rounds=2)
        self.assertEqual(result.decision, Decision.PASS)
        self.assertEqual(len(glm.calls), 2)
        self.assertEqual(result.metadata["consensus"]["max_rounds"], 2)

    def test_every_mixed_decision_pair_is_downweighted_to_pass(self):
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
                self.assertEqual(result.decision, Decision.PASS)
                self.assertEqual(result.issues, ())
                self.assertEqual(
                    result.metadata["consensus"]["rounds_used"], 3
                )
                self.assertEqual(len(glm.calls), 3)
                self.assertEqual(len(gemini.calls), 3)

    def test_vision_reconsideration_keeps_submission_directory_and_three_rounds(self):
        glm = ScriptedReviewer(outcome(Decision.FAIL, "glm"))
        gemini = ScriptedReviewer(outcome(Decision.PASS, "gemini"))
        reviewer = VisionConsensusReviewer({"glm": glm, "gemini": gemini}, max_rounds=3)
        snapshot = object()
        result = reviewer.review(object(), "Lab1", snapshot, "student/Lab1")
        self.assertEqual(result.decision, Decision.PASS)
        self.assertEqual(result.metadata["consensus"]["stage"], "图片审核")
        self.assertEqual(len(glm.calls), 3)
        self.assertEqual(glm.calls[2]["previous_round"], 2)
        for args in glm.arguments:
            self.assertIs(args[2], snapshot)
            self.assertEqual(args[3], "student/Lab1")

    def test_provider_failure_during_reconsideration_uses_remaining_verdict(self):
        glm = ScriptedReviewer(
            outcome(Decision.PASS, "glm"), ProviderUnavailableError("glm timeout")
        )
        gemini = ScriptedReviewer(outcome(Decision.FAIL, "gemini"))
        result = self.review(glm, gemini)
        self.assertEqual(result.decision, Decision.FAIL)
        self.assertTrue(result.metadata["consensus"]["degraded"])
        self.assertEqual(result.metadata["consensus"]["rounds_used"], 2)
        self.assertEqual(len(result.metadata["consensus"]["history"]), 2)

    def test_both_providers_failing_during_reconsideration_never_passes(self):
        glm = ScriptedReviewer(
            outcome(Decision.PASS, "glm"), ProviderUnavailableError("glm timeout")
        )
        gemini = ScriptedReviewer(
            outcome(Decision.FAIL, "gemini"), ProviderUnavailableError("gemini timeout")
        )
        with self.assertRaises(ReviewSystemError):
            self.review(glm, gemini)

    def test_one_temporarily_unavailable_uses_working_result(self):
        result = self.review(
            ScriptedReviewer(ProviderUnavailableError("glm timeout")),
            ScriptedReviewer(outcome(Decision.PASS, "gemini")),
        )
        self.assertEqual(result.decision, Decision.PASS)
        consensus = result.metadata["consensus"]
        self.assertTrue(consensus["degraded"])
        self.assertEqual(consensus["unavailable_providers"], ["glm"])
        self.assertEqual(consensus["provider_errors"], {"glm": "glm timeout"})
        self.assertEqual(
            consensus["history"][0]["provider_errors"], {"glm": "glm timeout"}
        )

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

    def test_template_loading_error_never_falls_back_to_a_passing_provider(self):
        with self.assertRaises(TemplateLoadError):
            self.review(
                ScriptedReviewer(TemplateLoadError("template unavailable")),
                ScriptedReviewer(outcome(Decision.PASS, "gemini")),
            )


if __name__ == "__main__":
    unittest.main()
