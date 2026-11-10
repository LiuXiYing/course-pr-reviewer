"""Pure, deterministic grading policy.

No GitHub API, no database, no AI. Given the trusted effective submission time
and the course policy this module returns the full grade decision, so the same
reviewed head always produces the same score no matter how often it is rerun.

Time rules (all comparisons happen on UTC instants, seconds precision):

* ``effective_submission_at <= deadline`` -> on time, 100% cap.
* a valid pre-deadline claim plus a final head pushed within ``grace.hours``
  -> still 100% (``ON_TIME_CORRECTED``).
* otherwise the late bands from the course config apply, matched on exact
  seconds so there is no gap between "48 小时" and "3 天".
* ``legacy`` assignments never deduct points and never invent a score.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

POINTS_QUANT = Decimal("0.01")

DEFAULT_BANDS: tuple[dict[str, Any], ...] = (
    {"max_late_hours": 0, "cap_percent": 100},
    {"max_late_hours": 48, "cap_percent": 90},
    {"max_late_hours": 168, "cap_percent": 75},
    {"max_late_hours": 336, "cap_percent": 60},
    {"max_late_hours": 672, "cap_percent": 40},
    {"max_late_hours": None, "cap_percent": 20},
)

DEFAULT_POLICY_VERSION = "2026-fall-v1"
DEFAULT_MAX_POINTS = "10.00"
DEFAULT_GRACE_HOURS = 48

MODE_LEGACY = "legacy"
MODE_TIERED = "tiered"
GRADING_MODES = (MODE_LEGACY, MODE_TIERED)

TIME_ON_TIME = "ON_TIME"
TIME_ON_TIME_CORRECTED = "ON_TIME_CORRECTED"
TIME_LATE = "LATE"
TIME_AFTER_FINAL_ACCEPT = "AFTER_FINAL_ACCEPT"


class GradingPolicyError(ValueError):
    """Raised when the policy itself is unusable; callers must fail closed."""


def default_grading_settings() -> dict[str, Any]:
    """The grading block a course gets when it opts in without overrides."""
    return {
        "storage": {
            "branch": "grades",
            "schema_version": 1,
            "write_receipts": True,
            "export_on_finalize": False,
        },
        "policy_version": DEFAULT_POLICY_VERSION,
        "defaults": {
            "max_points": DEFAULT_MAX_POINTS,
            "legacy": {"record_completion": True, "fixed_points": None},
            "tiered": {
                "grace": {
                    "enabled": True,
                    "hours": DEFAULT_GRACE_HOURS,
                    "require_substantive_snapshot": True,
                },
                "bands": [dict(band) for band in DEFAULT_BANDS],
                "after_final_accept": "reject_merge",
            },
        },
    }


def _decimal_points(value: Any, field_name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise GradingPolicyError(f"{field_name} 不能是布尔值")
    elif isinstance(value, (int, float, str)):
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise GradingPolicyError(f"{field_name} 不是有效数字：{value!r}") from exc
    else:
        raise GradingPolicyError(f"{field_name} 不是有效数字：{value!r}")
    if parsed.is_nan() or parsed.is_infinite():
        raise GradingPolicyError(f"{field_name} 不是有限数字")
    if parsed < 0:
        raise GradingPolicyError(f"{field_name} 不能为负数")
    return parsed


def format_points(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(POINTS_QUANT, rounding=ROUND_HALF_UP))


def _aware(value: dt.datetime, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise GradingPolicyError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise GradingPolicyError(f"{field_name} 必须包含时区")
    return value


def human_duration(seconds: int) -> str:
    """Render a positive duration in the wording used in PR comments."""
    if seconds <= 0:
        return "0 分钟"
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, remainder = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分")
    if not parts:
        parts.append(f"{remainder} 秒")
    return " ".join(parts)


def _format_hours(hours: int) -> str:
    # 72 小时以内用小时表达，和教师平时的说法一致（48 小时而不是 2 天）。
    if hours < 72:
        return f"{hours} 小时"
    if hours % 24 == 0:
        return f"{hours // 24} 天"
    return f"{hours // 24} 天 {hours % 24} 小时"


def _band_label(lower_hours: int, upper_hours: int | None) -> str:
    if upper_hours is None:
        return f"超过 {_format_hours(lower_hours)}"
    if lower_hours == 0:
        return f"{_format_hours(upper_hours)}以内"
    return f"{_format_hours(lower_hours)}以上，{_format_hours(upper_hours)}以内"


def normalise_bands(bands: Any) -> list[dict[str, Any]]:
    """Validate and normalise the late bands; raise on anything ambiguous."""
    if not isinstance(bands, (list, tuple)) or not bands:
        raise GradingPolicyError("grading.defaults.tiered.bands 必须是非空数组")
    normalised: list[dict[str, Any]] = []
    previous: int | None = None
    for index, band in enumerate(bands):
        if not isinstance(band, dict):
            raise GradingPolicyError(f"bands[{index}] 必须是对象")
        if set(band) != {"max_late_hours", "cap_percent"}:
            raise GradingPolicyError(
                f"bands[{index}] 只能包含 max_late_hours 与 cap_percent"
            )
        cap = band["cap_percent"]
        if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= 100:
            raise GradingPolicyError(f"bands[{index}].cap_percent 必须是 0～100 的整数")
        limit = band["max_late_hours"]
        is_last = index == len(bands) - 1
        if limit is None:
            if not is_last:
                raise GradingPolicyError("只有最后一个分档可以设置 max_late_hours: null")
        else:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise GradingPolicyError(
                    f"bands[{index}].max_late_hours 必须是大于等于 0 的整数"
                )
            if previous is not None and limit <= previous:
                raise GradingPolicyError(
                    f"bands[{index}].max_late_hours 必须严格递增，"
                    f"当前 {limit} 不大于上一档 {previous}"
                )
            previous = limit
        if index == 0 and limit != 0:
            raise GradingPolicyError(
                "第一个分档必须是 max_late_hours: 0（准时不计迟交）"
            )
        if normalised and cap > normalised[-1]["cap_percent"]:
            raise GradingPolicyError(
                f"bands[{index}].cap_percent 不能高于上一档，迟交越久上限不能更高"
            )
        normalised.append({"max_late_hours": limit, "cap_percent": cap})
    if normalised[-1]["max_late_hours"] is not None:
        raise GradingPolicyError(
            "最后一个分档必须设置 max_late_hours: null，覆盖无限迟交"
        )
    return normalised


@dataclass(frozen=True)
class GradeDecision:
    mode: str
    time_status: str
    deadline: str
    effective_submission_at: str
    late_seconds: int
    grace_applied: bool
    cap_percent: int | None
    max_points: str | None
    awarded_points: str | None
    policy_version: str
    explanation: str
    band_label: str | None = None
    merge_blocked: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def scored(self) -> bool:
        return self.awarded_points is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "time_status": self.time_status,
            "deadline": self.deadline,
            "effective_submission_at": self.effective_submission_at,
            "late_seconds": self.late_seconds,
            "late_human": human_duration(self.late_seconds),
            "grace_applied": self.grace_applied,
            "cap_percent": self.cap_percent,
            "max_points": self.max_points,
            "awarded_points": self.awarded_points,
            "policy_version": self.policy_version,
            "explanation": self.explanation,
            "band_label": self.band_label,
            "merge_blocked": self.merge_blocked,
            **({"details": self.details} if self.details else {}),
        }


def _local(moment: dt.datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _display(moment: dt.datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).isoformat()


def _grace_is_valid(grace_claim: dict[str, Any] | None, deadline: dt.datetime) -> bool:
    if not isinstance(grace_claim, dict):
        return False
    if grace_claim.get("eligibility") != "ELIGIBLE":
        return False
    raw = grace_claim.get("event_at")
    if not isinstance(raw, str):
        return False
    try:
        claimed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return False
    if claimed.tzinfo is None:
        return False
    return claimed <= deadline


def compute_grade(
    *,
    mode: str,
    max_points: Decimal | str | None,
    deadline: dt.datetime,
    effective_submission_at: dt.datetime,
    grace_claim: dict[str, Any] | None = None,
    grace_hours: int = 0,
    final_accept_at: dt.datetime | None = None,
    policy: dict[str, Any] | None = None,
) -> GradeDecision:
    """Return the full grade decision for one (student, assignment) pair.

    ``effective_submission_at`` must be the trusted server-side time of the
    reviewed head, never a commit author date and never the review run time.
    """
    policy = dict(policy or {})
    timezone_name = str(policy.get("timezone", "Asia/Shanghai"))
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:  # pragma: no cover - config validation catches this
        raise GradingPolicyError(f"未知时区：{timezone_name}") from exc
    policy_version = str(policy.get("policy_version", DEFAULT_POLICY_VERSION))
    if mode not in GRADING_MODES:
        raise GradingPolicyError(f"不支持的 grading.mode：{mode}")

    submitted = _aware(effective_submission_at, "effective_submission_at")
    due = _aware(deadline, "deadline")
    if final_accept_at is not None:
        final = _aware(final_accept_at, "final_accept_at")
        if final <= due:
            raise GradingPolicyError("final_accept_at 必须晚于作业 deadline")
    else:
        final = None

    late_seconds = int((submitted - due).total_seconds())
    late_seconds = max(late_seconds, 0)
    on_time = submitted <= due

    deadline_text = _display(due, zone)
    submitted_text = _display(submitted, zone)
    base_details = {
        "deadline_local": _local(due, zone),
        "submitted_local": _local(submitted, zone),
        "timezone": timezone_name,
    }

    legacy_settings = policy.get("legacy", {}) if isinstance(policy.get("legacy"), dict) else {}
    legacy_fixed = legacy_settings.get("fixed_points")
    if mode == MODE_LEGACY:
        if final is not None and submitted > final:
            time_status = TIME_AFTER_FINAL_ACCEPT
        else:
            time_status = TIME_ON_TIME if on_time else TIME_LATE
        awarded: Decimal | None = None
        cap: int | None = None
        if legacy_fixed is not None:
            cap = 100
            awarded = _decimal_points(legacy_fixed, "legacy.fixed_points").quantize(
                POINTS_QUANT, rounding=ROUND_HALF_UP
            )
        explanation = (
            "本实验属于制度实施前已发布的作业，不实行分段迟交计分；"
            "内容审核通过并自动合并，是否计入课程成绩以教师原有规则为准。"
            if legacy_fixed is None
            else f"本实验属于制度实施前已发布的作业，按课程设定固定记 {awarded} 分。"
        )
        if not on_time and legacy_fixed is None:
            explanation += f"本次有效提交晚于截止时间 {human_duration(late_seconds)}，不倒扣。"
        return GradeDecision(
            mode=mode,
            time_status=time_status,
            deadline=deadline_text,
            effective_submission_at=submitted_text,
            late_seconds=late_seconds,
            grace_applied=False,
            cap_percent=cap,
            max_points=None,
            awarded_points=format_points(awarded),
            policy_version=policy_version,
            explanation=explanation,
            band_label=None,
            merge_blocked=False,
            details=base_details,
        )

    points = _decimal_points(
        DEFAULT_MAX_POINTS if max_points is None else max_points, "max_points"
    )
    points = points.quantize(POINTS_QUANT, rounding=ROUND_HALF_UP)
    if points <= 0:
        raise GradingPolicyError("max_points 必须大于 0")

    after_final = final is not None and submitted > final
    grace_valid = _grace_is_valid(grace_claim, due)
    grace_applied = False
    if after_final:
        time_status = TIME_AFTER_FINAL_ACCEPT
        cap_percent = 0
        band_label = None
    elif on_time:
        time_status = TIME_ON_TIME
        cap_percent = 100
        band_label = None
    elif (
        grace_valid
        and grace_hours > 0
        and submitted <= due + dt.timedelta(hours=grace_hours)
    ):
        time_status = TIME_ON_TIME_CORRECTED
        cap_percent = 100
        band_label = None
        grace_applied = True
    else:
        bands = normalise_bands(policy.get("bands") or list(DEFAULT_BANDS))
        time_status = TIME_LATE
        cap_percent = None
        band_label = None
        lower = 0
        for band in bands:
            limit = band["max_late_hours"]
            if limit is not None and late_seconds <= limit * 3_600:
                cap_percent = band["cap_percent"]
                band_label = _band_label(lower, limit)
                break
            if limit is None:
                cap_percent = band["cap_percent"]
                band_label = _band_label(lower, None)
                break
            lower = limit
        if cap_percent is None:  # pragma: no cover - normalise_bands guarantees a tail
            raise GradingPolicyError("分档表没有覆盖本次迟交时长")

    if time_status == TIME_AFTER_FINAL_ACCEPT:
        policy_after = str(policy.get("after_final_accept", "reject_merge"))
        if policy_after == "accept_unscored":
            awarded = None
            explanation = (
                "本次提交晚于课程最终收件截止时间，只做内容验收并合并，不产生可计分成绩。"
            )
            merge_blocked = False
        else:
            awarded = Decimal("0.00")
            explanation = (
                "本次提交晚于课程最终收件截止时间，不再计分，也不会自动合并。"
                "如确有特殊情况，请联系教师。"
            )
            merge_blocked = True
    else:
        merge_blocked = False
        awarded = (points * Decimal(cap_percent) / Decimal(100)).quantize(
            POINTS_QUANT, rounding=ROUND_HALF_UP
        )
        if time_status == TIME_ON_TIME:
            explanation = "本次提交在截止时间之前，按 100% 计分。"
        elif time_status == TIME_ON_TIME_CORRECTED:
            explanation = (
                f"截止前存在经系统确认的有效提交，且在 {grace_hours} 小时内修正通过，"
                "因此按准时（100%）计分。"
            )
        else:
            explanation = (
                f"本次提交晚于截止时间 {human_duration(late_seconds)}，"
                f"按 {cap_percent}% 上限计分；无需向教师私聊申请补交。"
            )
    return GradeDecision(
        mode=mode,
        time_status=time_status,
        deadline=deadline_text,
        effective_submission_at=submitted_text,
        late_seconds=late_seconds,
        grace_applied=grace_applied,
        cap_percent=cap_percent,
        max_points=format_points(points),
        awarded_points=format_points(awarded),
        policy_version=policy_version,
        explanation=explanation,
        band_label=band_label,
        merge_blocked=merge_blocked,
        details=base_details,
    )
