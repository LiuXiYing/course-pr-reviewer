"""Opt-in, assignment-scoped Markdown size gates (phase one only)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .exceptions import ConfigurationError, ContentLimitExceeded, ReviewSystemError
from .models import Issue, ReasonCode
from .path_utils import canonical_filename
from .snapshot import PullRequestSnapshot

DEFAULT_CHARACTERS = ("#", "-", "*", "`", "|", ">")


def measure(content: str, characters: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Count LF-delimited source lines, not rendered Markdown or Unicode separators."""
    content = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n") if content else []
    if lines and lines[-1] == "":
        lines.pop()  # A terminal newline terminates the previous line.
    counts = Counter(content)
    return {
        "total_lines": len(lines),
        "nonempty_lines": sum(bool(line.strip()) for line in lines),
        "characters": {character: counts[character] for character in characters},
    }


def check_template_metrics(
    assignment: dict[str, Any],
    snapshot: PullRequestSnapshot,
    expected_prefix: str,
    github,
) -> tuple[list[Issue], dict[str, Any]]:
    settings = assignment.get("template_metrics", {})
    if not settings.get("enabled", False):
        return [], {}

    template_path = assignment["report_template"]
    report_path = expected_prefix + settings["report_file"]
    max_bytes = settings.get("max_file_bytes", 200_000)
    characters = settings.get("characters", DEFAULT_CHARACTERS)
    if github is None or snapshot.base_sha is None:
        raise ConfigurationError("模板数量检查缺少 GitHub 客户端或受信任的基础分支 SHA")
    try:
        template = github.text_file(
            snapshot.repository, template_path, snapshot.base_sha, max_bytes=max_bytes,
        )
    except (ContentLimitExceeded, ReviewSystemError) as exc:
        raise ConfigurationError(f"无法读取数量检查基准模板 {template_path}：{exc}") from exc
    if not isinstance(template, str) or not template.removeprefix("\ufeff").strip() or "\0" in template:
        raise ConfigurationError("数量检查基准模板必须是非空 UTF-8 文本")
    if len(template.encode("utf-8")) > max_bytes:
        raise ConfigurationError("数量检查基准模板超过 max_file_bytes，请教师调整配置")

    matches = [
        changed for changed in snapshot.files
        if changed.status != "removed"
        and canonical_filename(changed.filename) == canonical_filename(report_path)
    ]
    if len(matches) != 1:
        return [Issue(
            code=ReasonCode.INVALID_FILE,
            message="模板数量检查必须对应唯一的已提交报告文件。",
            file=report_path,
        )], {}
    changed = matches[0]
    content = changed.content
    if content is None:
        if changed.blob_sha is None:
            raise ReviewSystemError(f"数量检查无法读取报告：{changed.filename} 缺少 blob SHA")
        try:
            content = github.text_blob(snapshot.repository, changed.blob_sha, max_bytes=max_bytes)
        except ContentLimitExceeded:
            return [Issue(
                code=ReasonCode.INVALID_FILE,
                message=f"报告超过模板数量检查上限 {max_bytes} 字节。",
                file=changed.filename,
            )], {}
    if content is None or "\0" in content:
        return [Issue(code=ReasonCode.INVALID_FILE, message="报告必须是 UTF-8 文本。", file=changed.filename)], {}
    if len(content.encode("utf-8")) > max_bytes:
        return [Issue(
            code=ReasonCode.INVALID_FILE,
            message=f"报告超过模板数量检查上限 {max_bytes} 字节。",
            file=changed.filename,
        )], {}

    baseline, submitted = measure(template, characters), measure(content, characters)
    metadata = {
        "template": template_path, "base_sha": snapshot.base_sha,
        "report_file": changed.filename, "baseline": baseline, "submitted": submitted,
        "line_check": "PASS", "character_check": "NOT_RUN",
    }
    issues = []
    for key, label, code in (
        ("total_lines", "总行数", ReasonCode.TEMPLATE_LINES_SHORT),
        ("nonempty_lines", "非空行数", ReasonCode.TEMPLATE_NONEMPTY_LINES_SHORT),
    ):
        if submitted[key] < baseline[key]:
            issues.append(Issue(
                code=code,
                message=f"{label}不足：模板 {baseline[key]} 行，提交 {submitted[key]} 行；请保留完整模板。",
                file=changed.filename,
                rule=f"{label}不得少于教师发布模板",
            ))
    if issues:
        metadata["line_check"] = "FAIL"
        return issues, metadata

    for character in characters:
        actual, minimum = submitted["characters"][character], baseline["characters"][character]
        if actual < minimum:
            issues.append(Issue(
                code=ReasonCode.TEMPLATE_CHARACTERS_SHORT,
                message=f"源码字符 {character!r} 数量不足：模板 {minimum}，提交 {actual}；请恢复模板排版。",
                file=changed.filename,
                rule="每一种指定字符数量均不得少于教师发布模板",
            ))
    metadata["character_check"] = "FAIL" if issues else "PASS"
    return issues, metadata
