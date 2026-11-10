"""Semantic validation for the ``grading`` configuration block.

JSON Schema already rejects unknown keys and wrong types; this module adds the
rules that decide whether the numbers and dates actually make sense. Everything
here fails closed: an unusable grading block must stop automatic scoring and
merging instead of silently falling back to 100%.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ..exceptions import ConfigurationError
from .policy import (
    DEFAULT_BANDS,
    DEFAULT_GRACE_HOURS,
    DEFAULT_MAX_POINTS,
    GRADING_MODES,
    GradingPolicyError,
    _decimal_points,
    compute_grade,
    default_grading_settings,
    normalise_bands,
)

AFTER_FINAL_ACCEPT_VALUES = ("reject_merge", "accept_unscored")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} 必须是对象")
    return value


def _deadline(assignment_id: str, value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ConfigurationError(f"{assignment_id}.deadline 不是有效时间")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{assignment_id}.deadline 不是有效时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{assignment_id}.deadline 必须包含时区")
    return parsed


def _optional_datetime(value: Any, label: str) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} 不是有效时间")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationError(f"{label} 不是有效时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{label} 必须包含时区，例如 2027-01-10T23:59:59+08:00")
    return parsed


def grading_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Return the effective grading settings, filling documented defaults."""
    settings = default_grading_settings()
    configured = data.get("grading")
    if not isinstance(configured, dict):
        return settings
    for key in ("storage", "defaults"):
        section = configured.get(key)
        if isinstance(section, dict):
            settings[key].update(section)
    defaults = configured.get("defaults")
    if isinstance(defaults, dict):
        for key in ("legacy", "tiered"):
            section = defaults.get(key)
            if isinstance(section, dict):
                settings["defaults"][key].update(
                    {k: v for k, v in section.items() if k not in {"bands", "grace"}}
                )
        tiered = defaults.get("tiered")
        if isinstance(tiered, dict):
            grace = tiered.get("grace")
            if isinstance(grace, dict):
                settings["defaults"]["tiered"]["grace"].update(grace)
            if "bands" in tiered:
                settings["defaults"]["tiered"]["bands"] = tiered["bands"]
    if "policy_version" in configured:
        settings["policy_version"] = configured["policy_version"]
    return settings


def validate_grading(data: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the grading block and return the effective settings.

    Returns ``None`` when the course has not opted into grading at all, so old
    configurations keep the previous semantics.
    """
    enabled = bool(data.get("features", {}).get("grading", False))
    configured = data.get("grading")
    if configured is None:
        if enabled:
            raise ConfigurationError(
                "features.grading 已开启，但缺少 grading 配置块"
            )
        return None
    if not isinstance(configured, dict):
        raise ConfigurationError("grading 必须是对象")

    settings = grading_settings(data)

    branch = settings["storage"].get("branch")
    if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch):
        raise ConfigurationError("grading.storage.branch 必须是安全的 Git 分支名")
    if branch in {"main", "master"}:
        raise ConfigurationError("grading.storage.branch 不能指向默认分支")
    if settings["storage"].get("schema_version") not in (1, None):
        raise ConfigurationError("grading.storage.schema_version 目前只支持 1")

    policy_version = settings.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ConfigurationError("grading.policy_version 不能为空")

    try:
        max_points = _decimal_points(
            settings["defaults"].get("max_points", DEFAULT_MAX_POINTS),
            "grading.defaults.max_points",
        )
    except GradingPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc
    if max_points <= 0:
        raise ConfigurationError("grading.defaults.max_points 必须大于 0")

    legacy = settings["defaults"]["legacy"]
    if not isinstance(legacy.get("record_completion"), bool):
        raise ConfigurationError("grading.defaults.legacy.record_completion 必须是布尔值")
    if legacy.get("fixed_points") is not None:
        try:
            fixed = _decimal_points(legacy["fixed_points"], "legacy.fixed_points")
        except GradingPolicyError as exc:
            raise ConfigurationError(str(exc)) from exc
        if fixed > max_points:
            raise ConfigurationError("legacy.fixed_points 不能高于默认满分")

    tiered = settings["defaults"]["tiered"]
    grace = tiered["grace"]
    hours = grace.get("hours", DEFAULT_GRACE_HOURS)
    if not isinstance(hours, int) or isinstance(hours, bool) or not 0 <= hours <= 720:
        raise ConfigurationError("grading.defaults.tiered.grace.hours 必须是 0～720 的整数")
    if not isinstance(grace.get("enabled"), bool):
        raise ConfigurationError("grading.defaults.tiered.grace.enabled 必须是布尔值")
    if not isinstance(grace.get("require_substantive_snapshot"), bool):
        raise ConfigurationError(
            "grading.defaults.tiered.grace.require_substantive_snapshot 必须是布尔值"
        )
    if tiered.get("after_final_accept") not in AFTER_FINAL_ACCEPT_VALUES:
        raise ConfigurationError(
            "grading.defaults.tiered.after_final_accept 只能是 "
            + " 或 ".join(AFTER_FINAL_ACCEPT_VALUES)
        )

    try:
        bands = normalise_bands(tiered.get("bands") or list(DEFAULT_BANDS))
    except GradingPolicyError as exc:
        raise ConfigurationError(str(exc)) from exc
    settings["defaults"]["tiered"]["bands"] = bands

    final_accept_at = _optional_datetime(
        data.get("course", {}).get("final_accept_at"), "course.final_accept_at"
    )

    assignments = data.get("assignments", {})
    tiered_deadlines: list[tuple[str, dt.datetime]] = []
    saw_tiered = False
    for assignment_id, assignment in assignments.items():
        block = assignment.get("grading")
        if block is None:
            if enabled:
                raise ConfigurationError(
                    f"{assignment_id} 缺少 grading.mode："
                    "制度启用后每个作业都必须显式标注 legacy 或 tiered"
                )
            continue
        if not isinstance(block, dict):
            raise ConfigurationError(f"{assignment_id}.grading 必须是对象")
        mode = block.get("mode")
        if mode not in GRADING_MODES:
            raise ConfigurationError(
                f"{assignment_id}.grading.mode 只能是 legacy 或 tiered"
            )
        deadline = _deadline(assignment_id, assignment.get("deadline"))
        if mode == "tiered":
            saw_tiered = True
            tiered_deadlines.append((assignment_id, deadline))
            try:
                points = _decimal_points(
                    block.get("max_points", max_points),
                    f"{assignment_id}.grading.max_points",
                )
            except GradingPolicyError as exc:
                raise ConfigurationError(str(exc)) from exc
            if points <= 0:
                raise ConfigurationError(f"{assignment_id}.grading.max_points 必须大于 0")
        elif block.get("max_points") is not None:
            raise ConfigurationError(
                f"{assignment_id} 是 legacy 作业，不应配置 grading.max_points"
            )
        # A dry computation proves the assignment can actually be scored.
        try:
            compute_grade(
                mode=mode,
                max_points=block.get("max_points", max_points),
                deadline=deadline,
                effective_submission_at=deadline,
                grace_hours=int(grace.get("hours", 0)),
                final_accept_at=final_accept_at,
                policy={
                    "policy_version": policy_version,
                    "timezone": data["course"]["timezone"],
                    "bands": bands,
                    "legacy": legacy,
                    "after_final_accept": tiered.get("after_final_accept"),
                },
            )
        except GradingPolicyError as exc:
            raise ConfigurationError(f"{assignment_id}.grading 无法计分：{exc}") from exc

    if enabled and not saw_tiered:
        # Recording legacy completions only is a valid intermediate state: a
        # course can switch grading on before publishing its first tiered lab.
        pass
    if final_accept_at is not None:
        for assignment_id, deadline in tiered_deadlines:
            if final_accept_at <= deadline:
                raise ConfigurationError(
                    f"course.final_accept_at 必须晚于 tiered 作业 {assignment_id} 的 deadline"
                )
    return settings


__all__ = [
    "AFTER_FINAL_ACCEPT_VALUES",
    "grading_settings",
    "validate_grading",
]
