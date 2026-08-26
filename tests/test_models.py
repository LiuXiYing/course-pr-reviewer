import json
import unittest

from course_pr_reviewer.models import Decision, Issue, ReasonCode, ReviewResult


class ReviewResultTests(unittest.TestCase):
    def test_pass_has_no_reason_codes(self):
        result = ReviewResult(
            decision=Decision.PASS, summary="全部检查通过", confidence=0.98
        )
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(json.loads(result.to_json())["decision"], "PASS")

    def test_fail_requires_issue(self):
        with self.assertRaises(ValueError):
            ReviewResult(decision=Decision.FAIL, summary="失败")

    def test_pass_rejects_issue(self):
        issue = Issue(code=ReasonCode.TITLE_MISMATCH, message="标题不匹配")
        with self.assertRaises(ValueError):
            ReviewResult(decision=Decision.PASS, summary="通过", issues=(issue,))

    def test_reason_codes_are_stable_and_unique(self):
        result = ReviewResult(
            decision=Decision.FAIL,
            summary="标题不匹配",
            issues=(
                Issue(code=ReasonCode.TITLE_MISMATCH, message="缺少学号"),
                Issue(code=ReasonCode.TITLE_MISMATCH, message="Lab 大小写错误"),
            ),
        )
        self.assertEqual(result.reason_codes, ("TITLE_MISMATCH",))

    def test_confidence_range_is_enforced(self):
        with self.assertRaises(ValueError):
            ReviewResult(decision=Decision.PASS, summary="通过", confidence=1.1)


if __name__ == "__main__":
    unittest.main()
