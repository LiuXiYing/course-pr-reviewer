"""Bounded regeneration of malformed model output without relaxing validation."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .exceptions import StructuredOutputError

if TYPE_CHECKING:
    from .ai import AIClient

LOGGER = logging.getLogger(__name__)
MAX_OUTPUT_CORRECTIONS = 2
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def normalize_structured_output(value: Any, schema: dict[str, Any]) -> Any:
    """Drop unused fields, leaving missing fields and invalid values to validation."""
    if isinstance(value, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return value
        allowed = properties if schema.get("additionalProperties") is False else value
        return {
            key: normalize_structured_output(item, properties.get(key, {}))
            for key, item in value.items()
            if key in allowed
        }
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [normalize_structured_output(item, item_schema) for item in value]
    return value


def _response_metadata(response: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if not isinstance(response, dict):
        return metadata
    usage = response.get("usage")
    if isinstance(usage, dict):
        for key in USAGE_FIELDS:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[key] = value
    response_id = response.get("id")
    if isinstance(response_id, str) and response_id:
        metadata["response_id"] = response_id
    return metadata


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite constants are not valid JSON")


def parse_api_response(
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise StructuredOutputError(
            "AI API 响应缺少 choices[0].message.content"
        ) from exc
    if not isinstance(content, str):
        raise StructuredOutputError("AI API 的 message.content 不是文本")
    try:
        parsed = json.loads(content, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise StructuredOutputError("AI message.content 不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise StructuredOutputError("AI message.content 顶层必须是 JSON 对象")
    return parsed, _response_metadata(response)


def complete_structured_output(
    client: AIClient,
    *,
    settings: dict[str, Any],
    schema: dict[str, Any],
    system_prompt: str,
    user_content: str | list[dict[str, Any]],
    label: str,
    json_mode: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Regenerate at most twice; provider failures keep their existing retry policy.

    Only validator paths and trusted schema constraints enter correction prompts.
    A rejected response is never promoted to a system instruction or accepted as
    evidence. Each attempt receives the same original submission and review rules.
    """
    validator = Draft202012Validator(schema)
    failures: list[str] = []
    totals: dict[str, int] = {}
    for correction in range(MAX_OUTPUT_CORRECTIONS + 1):
        prompt = system_prompt
        if failures:
            prompt += (
                f"\n第 {correction}/{MAX_OUTPUT_CORRECTIONS} 次输出格式纠正。"
                f"上次输出未通过验证：{failures[-1]}。"
                "请依据原始提交重新独立审核，返回完整且符合 Schema 的 JSON。"
                "纠正格式不代表修改审核标准，不得为了结束重试而改判 PASS。"
                "issues 中的 evidence 必须是非空的可核验证据；"
                "文本证据应引用连续原文，不能编造、用空白填充或直接删除未解决的问题。"
                "缺少填写内容时可引用相应填写位置的原文；无法可靠判断时保留不确定结论。"
            )
        response = client.complete(
            model=settings["model"],
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
            timeout_seconds=settings["timeout_seconds"],
            max_attempts=settings["max_attempts"],
            max_output_tokens=settings["max_output_tokens"],
            json_mode=json_mode,
        )
        metadata = _response_metadata(response)
        for key in USAGE_FIELDS:
            if key in metadata:
                totals[key] = totals.get(key, 0) + metadata[key]
        try:
            parsed, _ = parse_api_response(response)
            parsed = normalize_structured_output(parsed, schema)
            errors = sorted(
                validator.iter_errors(parsed),
                key=lambda error: tuple(str(part) for part in error.absolute_path),
            )
            if errors:
                details = []
                for error in errors[:3]:
                    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
                    constraint = json.dumps(error.validator_value, ensure_ascii=False)
                    details.append(f"{location}: {error.validator}={constraint}")
                raise StructuredOutputError(
                    "未通过 Schema 验证：" + "；".join(details)
                )
        except StructuredOutputError as exc:
            failures.append(str(exc))
            LOGGER.warning(
                "%s output validation failed (attempt %d/%d): %s",
                label, correction + 1, MAX_OUTPUT_CORRECTIONS + 1, exc,
            )
            continue
        metadata.update(totals)
        metadata["structured_output"] = {
            "attempts": correction + 1,
            "corrections": correction,
            "validation_errors": failures,
        }
        return parsed, metadata
    raise StructuredOutputError(
        f"{label}在 {MAX_OUTPUT_CORRECTIONS + 1} 次生成"
        f"（含 {MAX_OUTPUT_CORRECTIONS} 次自动纠正）后仍无效：{failures[-1]}"
    )
