"""Publish review results to GitHub with head-SHA-bound mutations."""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .config import CourseConfiguration
from .exceptions import ReviewSystemError
from .models import Decision, ReasonCode
from .snapshot import REPOSITORY_RE, SHA_RE, GitHubClient

COMMENT_MARKER = "<!-- course-pr-reviewer -->"
MAX_RESULT_BYTES = 1_000_000
MAX_COMMENT_CHARS = 60_000


@dataclass(frozen=True)
class PublicationOutcome:
    commented: bool = False
    merged: bool = False
    closed: bool = False
    skipped: str | None = None


def _result_schema() -> dict[str, Any]:
    path = files("course_pr_reviewer").joinpath("schemas", "review-result.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_result(path: str | Path) -> dict[str, Any]:
    result_path = Path(path)
    try:
        raw = result_path.read_bytes()
    except OSError as exc:
        raise ReviewSystemError(f"无法读取审核结果：{result_path}") from exc
    if len(raw) > MAX_RESULT_BYTES:
        raise ReviewSystemError("审核结果超过 1 MB 安全上限")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewSystemError("审核结果不是有效 JSON") from exc
    if not isinstance(result, dict):
        raise ReviewSystemError("审核结果顶层必须是 JSON 对象")
    errors = sorted(
        Draft202012Validator(_result_schema()).iter_errors(result),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise ReviewSystemError(
            f"审核结果未通过 Schema 验证：{location}: {errors[0].message}"
        )
    return result


def _safe_markdown(value: Any) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = text.replace("@", "@\u200b")
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", text)


def _inline_code(value: Any) -> str:
    """Render untrusted text as Markdown inline code without visible escapes."""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    text = text.replace("@", "@\u200b")
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)),
        default=0,
    )
    fence = "`" * (longest_run + 1)
    padding = " " if text.startswith(("`", " ")) or text.endswith(("`", " ")) else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _safe_markdown_with_code(value: Any) -> str:
    """Escape prose while preserving simple backtick-delimited code spans."""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    parts: list[str] = []
    cursor = 0
    for match in re.finditer(r"`([^`]+)`", text):
        parts.append(_safe_markdown(text[cursor : match.start()]))
        parts.append(_inline_code(match.group(1)))
        cursor = match.end()
    parts.append(_safe_markdown(text[cursor:]))
    return "".join(parts)


def render_comment(result: dict[str, Any]) -> str:
    decision = Decision(result["decision"])
    headings = {
        Decision.PASS: "PR 自动审核通过 ✅",
        Decision.FAIL: "PR 自动审核未通过 ❌",
        Decision.MANUAL_REVIEW: "PR 无法自动确认 ⚠️",
        Decision.ERROR: "PR 审核系统错误 🚨",
    }
    lines = [
        COMMENT_MARKER,
        f"## {headings[decision]}",
        "",
        _safe_markdown_with_code(result["summary"]),
    ]
    metadata = result.get("metadata", {})
    consensus_rows = []
    for key, label in (
        ("ai_consensus", "文本审核"),
        ("vision_consensus", "图片审核"),
    ):
        consensus = metadata.get(key)
        if not isinstance(consensus, dict):
            continue
        decisions = consensus.get("provider_decisions", {})
        if not isinstance(decisions, dict):
            decisions = {}
        decision_text = "；".join(
            f"{str(provider).upper()}={decision}"
            for provider, decision in decisions.items()
        )
        rounds_used = consensus.get("rounds_used", "?")
        max_rounds = consensus.get("max_rounds", "?")
        row = (
            f"- {_safe_markdown(label)}：{_safe_markdown(decision_text)}"
            f"（第 {_safe_markdown(rounds_used)}/{_safe_markdown(max_rounds)} 轮）"
        )
        unavailable = consensus.get("unavailable_providers", [])
        if consensus.get("degraded") is True and isinstance(unavailable, list):
            names = "、".join(str(item).upper() for item in unavailable)
            row += f"；降级运行，{_safe_markdown(names)} 暂时不可用"
        consensus_rows.append(row)
    if consensus_rows:
        lines.extend(["", "### 双模型审核状态", "", *consensus_rows])
    issues = result.get("issues", [])
    if issues:
        lines.extend(["", "### 具体问题", ""])
        for item in issues:
            code = _inline_code(item["code"])
            message = _safe_markdown_with_code(item["message"])
            file = item.get("file")
            label = code
            if file:
                label += f" · {_inline_code(file)}"
            lines.append(f"- {label}：{message}")
            if item.get("rule"):
                lines.append(
                    f"  - 审核点：{_safe_markdown_with_code(item['rule'])}"
                )
            if item.get("evidence"):
                lines.append(
                    f"  - 证据：{_safe_markdown_with_code(item['evidence'])}"
                )
    head_sha = result.get("metadata", {}).get("head_sha", "")
    lines.extend(
        [
            "",
            "---",
            f"审核提交：{_inline_code(str(head_sha)[:12])}。新提交会重新触发审核。",
        ]
    )
    body = "\n".join(lines)
    if len(body) > MAX_COMMENT_CHARS:
        raise ReviewSystemError("生成的 PR 评论超过 60000 字符安全上限")
    return body


class GitHubResultPublisher:
    def __init__(
        self, github: GitHubClient, *, merge_github: GitHubClient | None = None
    ) -> None:
        self.github = github
        # GITHUB_TOKEN 无法合并 PR（403 Resource not accessible by integration），
        # 合并需要单独的 PAT；未提供时回退到主 client。
        self.merge_github = merge_github if merge_github is not None else github

    @staticmethod
    def _coordinates(
        result: dict[str, Any], expected_repository: str
    ) -> tuple[str, int, str]:
        metadata = result["metadata"]
        repository = metadata.get("repository")
        number = metadata.get("pr_number")
        head_sha = metadata.get("head_sha")
        if (
            not isinstance(repository, str)
            or not REPOSITORY_RE.fullmatch(repository)
            or repository != expected_repository
        ):
            raise ReviewSystemError("审核结果中的仓库与当前运行仓库不一致")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ReviewSystemError("审核结果中的 PR 编号无效")
        if not isinstance(head_sha, str) or not SHA_RE.fullmatch(head_sha):
            raise ReviewSystemError("审核结果中的 head SHA 无效")
        return repository, number, head_sha.lower()

    def _current_pr(self, repository: str, number: int) -> dict[str, Any]:
        return self.github.pull_request(repository, number)

    @staticmethod
    def _is_same_head(pr: dict[str, Any], head_sha: str) -> bool:
        return str(pr.get("head", {}).get("sha", "")).lower() == head_sha

    def _upsert_comment(self, repository: str, number: int, body: str) -> None:
        repo = urllib.parse.quote(repository, safe="/")
        existing: dict[str, Any] | None = None
        for page in range(1, 11):
            comments = self.github.get_json(
                f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}"
            )
            if not isinstance(comments, list):
                raise ReviewSystemError("GitHub API 返回了无效的 PR 评论列表")
            for comment in reversed(comments):
                if (
                    isinstance(comment, dict)
                    and COMMENT_MARKER in str(comment.get("body", ""))
                    and comment.get("user", {}).get("login") == "github-actions[bot]"
                ):
                    existing = comment
                    break
            if existing is not None or len(comments) < 100:
                break
        if existing is None:
            self.github.post_json(
                f"/repos/{repo}/issues/{number}/comments", {"body": body}
            )
            return
        comment_id = existing.get("id")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool):
            raise ReviewSystemError("已有审核评论缺少有效 ID")
        self.github.patch_json(
            f"/repos/{repo}/issues/comments/{comment_id}", {"body": body}
        )

    def publish(
        self,
        course: CourseConfiguration,
        result: dict[str, Any],
        *,
        expected_repository: str,
    ) -> PublicationOutcome:
        repository, number, head_sha = self._coordinates(result, expected_repository)
        reason_codes = set(result.get("reason_codes", []))
        if ReasonCode.STALE_HEAD_SHA.value in reason_codes:
            return PublicationOutcome(skipped="stale_review")

        pr = self._current_pr(repository, number)
        if not self._is_same_head(pr, head_sha):
            return PublicationOutcome(skipped="head_changed")
        if pr.get("merged") is True:
            return PublicationOutcome(skipped="already_merged")
        state = pr.get("state")
        close_requested = bool(result["metadata"].get("close_pr"))
        if close_requested and (
            result["decision"] != Decision.FAIL.value
            or ReasonCode.LATE_PR_CLOSE_REQUIRED.value not in reason_codes
        ):
            raise ReviewSystemError("自动关闭请求缺少超期关闭原因代码")
        if state != "open":
            return PublicationOutcome(
                skipped="already_closed" if close_requested else "pr_not_open"
            )

        commented = False
        if course.feature_enabled("comment_review"):
            self._upsert_comment(repository, number, render_comment(result))
            commented = True

        repo = urllib.parse.quote(repository, safe="/")
        if close_requested and course.feature_enabled("close_late_pr"):
            latest = self._current_pr(repository, number)
            if not self._is_same_head(latest, head_sha):
                return PublicationOutcome(commented=commented, skipped="head_changed")
            if latest.get("state") == "open":
                self.github.patch_json(
                    f"/repos/{repo}/pulls/{number}", {"state": "closed"}
                )
                return PublicationOutcome(commented=commented, closed=True)
            return PublicationOutcome(commented=commented, skipped="already_closed")

        if result["decision"] == Decision.PASS.value and course.feature_enabled(
            "auto_merge"
        ):
            latest = self._current_pr(repository, number)
            if not self._is_same_head(latest, head_sha):
                return PublicationOutcome(commented=commented, skipped="head_changed")
            if latest.get("state") != "open":
                return PublicationOutcome(commented=commented, skipped="pr_not_open")
            response = self.merge_github.put_json(
                f"/repos/{repo}/pulls/{number}/merge",
                {
                    "sha": head_sha,
                    "merge_method": course.data["course"].get("merge_method", "merge"),
                },
            )
            if not isinstance(response, dict) or response.get("merged") is not True:
                message = (
                    response.get("message", "GitHub 未确认合并成功")
                    if isinstance(response, dict)
                    else "GitHub 返回了无效的合并响应"
                )
                raise ReviewSystemError(f"自动合并失败：{message}")
            return PublicationOutcome(commented=commented, merged=True)

        return PublicationOutcome(commented=commented)
