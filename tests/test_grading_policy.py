from __future__ import annotations

import datetime as dt
import unittest

from grading_fixtures import BANDS, DEADLINE

from course_pr_reviewer.grading.policy import (
    GradingPolicyError,
    compute_grade,
    human_duration,
    normalise_bands,
)

TZ = dt.timezone(dt.timedelta(hours=8))
POLICY = {
    "policy_version": "2026-fall-v1",
    "timezone": "Asia/Shanghai",
    "bands": BANDS,
    "legacy": {"record_completion": True, "fixed_points": None},
    "after_final_accept": "reject_merge",
}


def grade(at: str, *, mode: str = "tiered", deadline: str = DEADLINE, **kwargs):
    return compute_grade(
        mode=mode,
        max_points=kwargs.pop("max_points", "10.00"),
        deadline=dt.datetime.fromisoformat(deadline),
        effective_submission_at=dt.datetime.fromisoformat(at),
        policy={**POLICY, **kwargs.pop("policy", {})},
        **kwargs,
    )


class BandBoundaryTests(unittest.TestCase):
    """计划书 14.1 的边界表，逐秒核对，不允许 48～72 小时出现空隙。"""

    CASES = (
        ("2026-11-20T23:59:59+08:00", "ON_TIME", 100, "10.00"),
        ("2026-11-21T00:00:00+08:00", "ON_TIME", 100, "10.00"),
        ("2026-11-21T00:00:01+08:00", "LATE", 90, "9.00"),
        ("2026-11-23T00:00:00+08:00", "LATE", 90, "9.00"),
        ("2026-11-23T00:00:01+08:00", "LATE", 75, "7.50"),
        ("2026-11-28T00:00:00+08:00", "LATE", 75, "7.50"),
        ("2026-11-28T00:00:01+08:00", "LATE", 60, "6.00"),
        ("2026-12-05T00:00:00+08:00", "LATE", 60, "6.00"),
        ("2026-12-05T00:00:01+08:00", "LATE", 40, "4.00"),
        ("2026-12-19T00:00:00+08:00", "LATE", 40, "4.00"),
        ("2026-12-19T00:00:01+08:00", "LATE", 20, "2.00"),
    )

    def test_deadline_table(self):
        for moment, status, cap, points in self.CASES:
            with self.subTest(moment=moment):
                decision = grade(moment)
                self.assertEqual(decision.time_status, status)
                self.assertEqual(decision.cap_percent, cap)
                self.assertEqual(decision.awarded_points, points)

    def test_late_seconds_are_exact(self):
        self.assertEqual(grade("2026-11-21T00:00:01+08:00").late_seconds, 1)
        self.assertEqual(grade("2026-11-23T00:00:00+08:00").late_seconds, 172800)

    def test_no_gap_between_48_and_72_hours(self):
        # 口语描述容易写成“48 小时内、3～7 天”，会在 48～72 小时之间留空。
        for hours in (48, 49, 60, 71, 72):
            with self.subTest(hours=hours):
                moment = dt.datetime(2026, 11, 21, tzinfo=TZ) + dt.timedelta(hours=hours)
                decision = grade(moment.isoformat())
                self.assertEqual(decision.cap_percent, 90 if hours <= 48 else 75)
                self.assertIsNotNone(decision.band_label)


class DecimalTests(unittest.TestCase):
    def test_non_ten_point_assignments_are_exact(self):
        cases = [
            ("6.00", "2026-11-24T00:00:00+08:00", "4.50"),
            ("15.00", "2026-12-10T00:00:00+08:00", "6.00"),
            ("1.00", "2026-11-21T12:00:00+08:00", "0.90"),
            ("7.00", "2026-11-29T00:00:00+08:00", "4.20"),
        ]
        for max_points, moment, expected in cases:
            with self.subTest(max_points=max_points, moment=moment):
                decision = grade(moment, max_points=max_points)
                self.assertEqual(decision.awarded_points, expected)

    def test_no_binary_float_artifacts(self):
        decision = grade("2026-11-23T00:00:01+08:00", max_points="10.00")
        self.assertEqual(decision.awarded_points, "7.50")
        self.assertNotIn("999", decision.awarded_points)
        self.assertNotIn("000000", decision.awarded_points)

    def test_half_up_rounding(self):
        # 3.33 * 75% = 2.4975 -> 2.50
        decision = grade("2026-11-24T00:00:00+08:00", max_points="3.33")
        self.assertEqual(decision.awarded_points, "2.50")

    def test_invalid_max_points_is_rejected(self):
        for value in ("0", "-1", "abc", "nan"):
            with self.subTest(value=value), self.assertRaises(GradingPolicyError):
                grade("2026-11-24T00:00:00+08:00", max_points=value)


class LegacyTests(unittest.TestCase):
    def test_legacy_late_never_deducts_and_never_invents_a_score(self):
        decision = grade("2026-12-30T00:00:00+08:00", mode="legacy")
        self.assertEqual(decision.mode, "legacy")
        self.assertEqual(decision.time_status, "LATE")
        self.assertIsNone(decision.cap_percent)
        self.assertIsNone(decision.awarded_points)
        self.assertIsNone(decision.max_points)
        self.assertGreater(decision.late_seconds, 20 * 86400)
        self.assertIn("不实行分段迟交计分", decision.explanation)

    def test_legacy_fixed_points_are_opt_in(self):
        decision = compute_grade(
            mode="legacy",
            max_points=None,
            deadline=dt.datetime.fromisoformat(DEADLINE),
            effective_submission_at=dt.datetime.fromisoformat(
                "2026-12-30T00:00:00+08:00"
            ),
            policy={
                **POLICY,
                "legacy": {"record_completion": True, "fixed_points": "8.00"},
            },
        )
        self.assertEqual(decision.awarded_points, "8.00")
        self.assertEqual(decision.cap_percent, 100)


class GraceTests(unittest.TestCase):
    EVENT_AT = "2026-11-20T21:00:00+08:00"

    def claim(self, eligibility: str = "ELIGIBLE") -> dict[str, str]:
        return {"eligibility": eligibility, "event_at": self.EVENT_AT}

    def test_grace_restores_full_marks_within_48_hours(self):
        decision = grade(
            "2026-11-22T06:00:00+08:00", grace_claim=self.claim(), grace_hours=48
        )
        self.assertEqual(decision.time_status, "ON_TIME_CORRECTED")
        self.assertTrue(decision.grace_applied)
        self.assertEqual(decision.cap_percent, 100)
        self.assertEqual(decision.awarded_points, "10.00")

    def test_grace_expires_at_exactly_48_hours(self):
        inside = grade(
            "2026-11-23T00:00:00+08:00", grace_claim=self.claim(), grace_hours=48
        )
        outside = grade(
            "2026-11-23T00:00:01+08:00", grace_claim=self.claim(), grace_hours=48
        )
        self.assertEqual(inside.cap_percent, 100)
        self.assertEqual(outside.cap_percent, 75)
        self.assertFalse(outside.grace_applied)

    def test_claim_after_deadline_is_not_a_claim(self):
        late_claim = {"eligibility": "ELIGIBLE", "event_at": "2026-11-21T00:00:01+08:00"}
        decision = grade(
            "2026-11-22T00:00:00+08:00", grace_claim=late_claim, grace_hours=48
        )
        self.assertEqual(decision.cap_percent, 90)
        self.assertFalse(decision.grace_applied)

    def test_ineligible_claim_is_ignored(self):
        for eligibility in ("NOT_ELIGIBLE", "UNKNOWN"):
            with self.subTest(eligibility=eligibility):
                decision = grade(
                    "2026-11-22T00:00:00+08:00",
                    grace_claim=self.claim(eligibility),
                    grace_hours=48,
                )
                self.assertEqual(decision.cap_percent, 90)

    def test_grace_disabled_means_normal_bands(self):
        decision = grade(
            "2026-11-22T00:00:00+08:00", grace_claim=self.claim(), grace_hours=0
        )
        self.assertEqual(decision.cap_percent, 90)


class FinalAcceptTests(unittest.TestCase):
    FINAL = dt.datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ)

    def test_after_final_accept_blocks_merge_by_default(self):
        decision = grade("2027-01-02T00:00:00+08:00", final_accept_at=self.FINAL)
        self.assertEqual(decision.time_status, "AFTER_FINAL_ACCEPT")
        self.assertEqual(decision.cap_percent, 0)
        self.assertEqual(decision.awarded_points, "0.00")
        self.assertTrue(decision.merge_blocked)

    def test_after_final_accept_can_accept_without_score(self):
        decision = grade(
            "2027-01-02T00:00:00+08:00",
            final_accept_at=self.FINAL,
            policy={"after_final_accept": "accept_unscored"},
        )
        self.assertFalse(decision.merge_blocked)
        self.assertIsNone(decision.awarded_points)

    def test_final_accept_must_be_after_deadline(self):
        with self.assertRaises(GradingPolicyError):
            grade(
                "2026-11-20T00:00:00+08:00",
                final_accept_at=dt.datetime(2026, 11, 1, tzinfo=TZ),
            )


class TimeZoneTests(unittest.TestCase):
    def test_naive_deadline_is_rejected(self):
        with self.assertRaises(GradingPolicyError):
            compute_grade(
                mode="tiered",
                max_points="10.00",
                deadline=dt.datetime(2026, 11, 21),  # noqa: DTZ001 - 故意无时区
                effective_submission_at=dt.datetime.fromisoformat(
                    "2026-11-22T00:00:00+08:00"
                ),
                policy=POLICY,
            )

    def test_comparison_happens_in_utc_across_new_year(self):
        decision = compute_grade(
            mode="tiered",
            max_points="10.00",
            deadline=dt.datetime.fromisoformat("2026-12-31T00:00:00+08:00"),
            effective_submission_at=dt.datetime.fromisoformat(
                "2027-01-01T00:30:00+08:00"
            ),
            policy=POLICY,
        )
        self.assertEqual(decision.cap_percent, 90)
        self.assertEqual(decision.late_seconds, 24 * 3600 + 1800)

    def test_other_timezone_is_displayed_in_course_zone(self):
        decision = compute_grade(
            mode="tiered",
            max_points="10.00",
            deadline=dt.datetime.fromisoformat("2026-11-21T00:00:00+00:00"),
            effective_submission_at=dt.datetime.fromisoformat(
                "2026-11-21T02:00:00+00:00"
            ),
            policy={**POLICY, "timezone": "Europe/Berlin"},
        )
        self.assertEqual(decision.cap_percent, 90)
        self.assertIn("+01:00", decision.effective_submission_at)


class BandValidationTests(unittest.TestCase):
    def test_missing_tail_band_is_rejected(self):
        with self.assertRaises(GradingPolicyError):
            normalise_bands(BANDS[:-1])

    def test_non_increasing_bands_are_rejected(self):
        broken = [
            {"max_late_hours": 0, "cap_percent": 100},
            {"max_late_hours": 48, "cap_percent": 90},
            {"max_late_hours": 48, "cap_percent": 75},
        ]
        with self.assertRaises(GradingPolicyError):
            normalise_bands(broken)

    def test_cap_must_not_grow_with_lateness(self):
        broken = [
            {"max_late_hours": 0, "cap_percent": 100},
            {"max_late_hours": 48, "cap_percent": 50},
            {"max_late_hours": None, "cap_percent": 80},
        ]
        with self.assertRaises(GradingPolicyError):
            normalise_bands(broken)

    def test_null_only_allowed_last(self):
        broken = [
            {"max_late_hours": 0, "cap_percent": 100},
            {"max_late_hours": None, "cap_percent": 90},
            {"max_late_hours": 72, "cap_percent": 75},
        ]
        with self.assertRaises(GradingPolicyError):
            normalise_bands(broken)


class ExplanationTests(unittest.TestCase):
    def test_human_duration(self):
        self.assertEqual(human_duration(0), "0 分钟")
        self.assertEqual(human_duration(90), "1 分")
        self.assertEqual(human_duration(203400), "2 天 8 小时 30 分")
        self.assertEqual(human_duration(45), "45 秒")

    def test_band_labels_read_naturally(self):
        self.assertEqual(grade("2026-11-22T00:00:00+08:00").band_label, "48 小时以内")
        self.assertEqual(
            grade("2026-11-24T00:00:00+08:00").band_label, "48 小时以上，7 天以内"
        )
        self.assertEqual(
            grade("2027-01-20T00:00:00+08:00").band_label, "超过 28 天"
        )

    def test_decision_json_is_stable(self):
        decision = grade("2026-11-24T00:00:00+08:00")
        payload = decision.to_dict()
        self.assertEqual(payload["late_human"], "3 天")
        self.assertEqual(payload["details"]["deadline_local"], "2026-11-21 00:00")
        self.assertIsInstance(payload["cap_percent"], int)


if __name__ == "__main__":
    unittest.main()
