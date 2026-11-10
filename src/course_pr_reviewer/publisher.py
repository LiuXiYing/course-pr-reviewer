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
from .exceptions import GradeStoreError, ReviewSystemError
from .feedback import validate_feedback
from .grading import records
from .models import Decision, ReasonCode
from .snapshot import REPOSITORY_RE, SHA_RE, GitHubClient

COMMENT_MARKER = "<!-- course-pr-reviewer -->"
MAX_RESULT_BYTES = 1_000_000
MAX_COMMENT_CHARS = 60_000

# 只有这些原因代码可以驱动自动关闭 PR：超期关闭、作业已合并的重复提交关闭。
CLOSE_REASON_CODES = frozenset(
    {
        ReasonCode.LATE_PR_CLOSE_REQUIRED.value,
        ReasonCode.ALREADY_MERGED_CLOSE_REQUIRED.value,
    }
)
MERGED_DUPLICATE_HEADING = "## 该作业已合并，本 PR 是重复提交，已自动关闭 🔁"
HEADINGS = {
    Decision.PASS: "PR 自动审核通过 ✅",
    Decision.FAIL: "PR 自动审核未通过 ❌",
    Decision.MANUAL_REVIEW: "PR 无法自动确认 ⚠️",
    Decision.ERROR: "PR 审核系统错误 🚨",
}


def _is_merged_duplicate(result: dict[str, Any]) -> bool:
    return ReasonCode.ALREADY_MERGED_CLOSE_REQUIRED.value in result.get(
        "reason_codes", []
    )


def _heading(result: dict[str, Any], decision: Decision) -> str:
    """重复提交的关闭要一眼看出来，不混在普通 FAIL 里。"""
    if _is_merged_duplicate(result):
        return MERGED_DUPLICATE_HEADING
    return f"## {HEADINGS[decision]}"


@dataclass(frozen=True)
class PublicationOutcome:
    commented: bool = False
    merged: bool = False
    closed: bool = False
    skipped: str | None = None
    receipt_status: str | None = None
    receipt_error: str | None = None


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


def _feedback_lines(result: dict[str, Any]) -> list[str]:
    feedback = result.get("metadata", {}).get("ai_feedback")
    if feedback is None or result["decision"] == Decision.PASS.value:
        return []
    rule_based = isinstance(feedback, dict) and feedback.get("source") == "rules"
    heading = "修改建议（规则生成）" if rule_based else "AI 错误说明（辅助参考）"
    lines = ["", f"### {heading}", ""]
    content = feedback.get("content") if isinstance(feedback, dict) else None
    try:
        if not isinstance(feedback, dict) or feedback.get("status") != "generated":
            raise ReviewSystemError("AI 错误说明不可用")
        validate_feedback(content, len(result.get("issues", [])))
    except ReviewSystemError:
        if rule_based:
            return lines + ["请根据下方完整的原始审核结果处理。"]
        return lines + ["本次未能生成可靠的 AI 说明，请根据下方完整的原始审核结果处理。"]

    notice = (
        "> 以下建议根据课程配置和原始审核结果生成。"
        if rule_based
        else "> 以下说明由 AI 生成，请按问题编号与下方原始审核结果对照；审核结论以原始结果为准。"
    )
    lines.extend(
        [
            notice,
            "",
            _safe_markdown_with_code(content["summary"]),
        ]
    )
    for group in content["groups"]:
        numbers = "、".join(str(number) for number in group["issue_numbers"])
        lines.extend(
            [
                "",
                f"**{_safe_markdown(group['title'])}**（对应原始问题 {numbers}）",
                "",
                _safe_markdown_with_code(group["explanation"]),
                "",
                *[
                    f"- {_safe_markdown_with_code(suggestion)}"
                    for suggestion in group["suggestions"]
                ],
            ]
        )
    return lines


def _grading_lines(result: dict[str, Any]) -> list[str]:
    """Deterministic grading block; never generated by a model."""
    grading = result.get("metadata", {}).get("grading")
    if not isinstance(grading, dict):
        return []
    decision = result.get("decision")
    details = grading.get("details") if isinstance(grading.get("details"), dict) else {}
    lines = ["", "### 成绩记录", ""]
    if decision != Decision.PASS.value:
        cap = grading.get("cap_percent")
        if decision == Decision.FAIL.value and cap is not None:
            lines.extend(
                [
                    "- 本次未产生成绩记录；上面列出的内容问题解决后重新提交，",
                    f"  将按 {_safe_markdown(cap)}% 的成绩上限计分（仅提示，不等于最终成绩）。",
                ]
            )
        else:
            lines.append("- 本次未产生成绩记录，内容审核通过并合并后才会写入成绩。")
        return lines
    if grading.get("mode") == "legacy":
        lines.append(
            "- 本实验属于制度实施前已发布的作业，不实行分段迟交计分；"
            "是否计入课程成绩以教师原有规则为准。"
        )
        if details.get("submitted_local"):
            lines.append(
                f"- 有效提交时间：{_safe_markdown(details['submitted_local'])}（课程时区）"
            )
        status = grading.get("receipt_status")
        lines.append(f"- 记录状态：{_safe_markdown(_receipt_label(status))}")
        return lines
    if details.get("submitted_local"):
        lines.append(
            f"- 有效提交时间：{_safe_markdown(details['submitted_local'])}（课程时区）"
        )
    if details.get("deadline_local"):
        lines.append(f"- 截止时间：{_safe_markdown(details['deadline_local'])}（课程时区）")
    if grading.get("grace_applied"):
        lines.append("- 迟交状态：准时（截止前有效提交，48 小时内修正通过）")
    elif grading.get("time_status") == "AFTER_FINAL_ACCEPT":
        lines.append("- 迟交状态：晚于课程最终收件截止时间")
    elif grading.get("late_seconds"):
        lines.append(f"- 迟交：{_safe_markdown(grading.get('late_human') or '')}")
    else:
        lines.append("- 迟交状态：准时")
    if grading.get("band_label"):
        lines.append(f"- 分档：{_safe_markdown(grading['band_label'])}")
    if grading.get("cap_percent") is not None:
        lines.append(f"- 成绩上限：{_safe_markdown(grading['cap_percent'])}%")
    awarded = grading.get("awarded_points")
    max_points = grading.get("max_points")
    if awarded is not None:
        lines.append(f"- 本次得分：{_safe_markdown(awarded)} / {_safe_markdown(max_points)}")
    if grading.get("claim_status") == "UNKNOWN":
        lines.append(
            "- 说明：截止前的提交内容未能完整核验，本次按实际迟交档位计分；"
            "如怀疑是系统故障，请联系教师复核。"
        )
    lines.append(f"- 记录状态：{_safe_markdown(_receipt_label(grading.get('receipt_status')))}")
    lines.append("- 说明：无需向教师私聊申请补交，以后按时提交可得更高分数。")
    return lines


def _receipt_label(status: Any) -> str:
    return {
        "PENDING_ACCEPTANCE": "已预留记录，合并确认后写入 grades 分支",
        "FINALIZED": "已写入 grades 分支",
        "FAILED": "写入失败，系统稍后自动重试",
    }.get(str(status), "尚未写入 grades 分支")


def render_comment(result: dict[str, Any]) -> str:
    decision = Decision(result["decision"])
    lines = [COMMENT_MARKER, _heading(result, decision)]
    feedback_lines = _feedback_lines(result)
    if feedback_lines:
        lines.extend(feedback_lines)
        lines.extend(["", "### 原始审核结果（判定依据）"])
    lines.extend(["", _safe_markdown_with_code(result["summary"])])
    lines.extend(_grading_lines(result))
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
            f"（已审核 {_safe_markdown(rounds_used)} 轮，最多 {_safe_markdown(max_rounds)} 轮）"
        )
        if consensus.get("disagreement_pass") is True:
            row += "；达到复核上限仍有分歧，至少一方 PASS，按规则通过"
        elif consensus.get("disagreement_blocked") is True:
            row += "；达到复核上限仍无 PASS，不自动合并"
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
        for number, item in enumerate(issues, start=1):
            code = _inline_code(item["code"])
            message = _safe_markdown_with_code(item["message"])
            file = item.get("file")
            label = code
            if file:
                label += f" · {_inline_code(file)}"
            lines.append(f"- **[{number}]** {label}：{message}")
            if item.get("location"):
                lines.append(
                    f"  - 位置：{_safe_markdown_with_code(item['location'])}"
                )
            if item.get("rule"):
                lines.append(
                    f"  - 审核点：{_safe_markdown_with_code(item['rule'])}"
                )
            if item.get("evidence"):
                lines.append(
                    f"  - 证据：{_safe_markdown_with_code(item['evidence'])}"
                )
    head_sha = result.get("metadata", {}).get("head_sha", "")
    tail = (
        "该作业已经合并，本次没有执行内容审核；如需重新提交，请先联系教师。"
        if _is_merged_duplicate(result)
        else "新提交会重新触发审核。"
    )
    lines.extend(
        [
            "",
            "---",
            f"审核提交：{_inline_code(str(head_sha)[:12])}。{tail}",
        ]
    )
    body = "\n".join(lines)
    if len(body) > MAX_COMMENT_CHARS and feedback_lines:
        # The optional explanation must never crowd out the original findings.
        return render_comment(
            {
                **result,
                "metadata": {
                    key: value for key, value in metadata.items() if key != "ai_feedback"
                },
            }
        )
    if len(body) > MAX_COMMENT_CHARS:
        raise ReviewSystemError("生成的 PR 评论超过 60000 字符安全上限")
    return body


class GitHubResultPublisher:
    def __init__(
        self,
        github: GitHubClient,
        *,
        merge_github: GitHubClient | None = None,
        grade_store=None,
        roster=None,
    ) -> None:
        self.github = github
        # GITHUB_TOKEN 无法合并 PR（403 Resource not accessible by integration），
        # 合并需要单独的 PAT；未提供时回退到主 client。
        self.merge_github = merge_github if merge_github is not None else github
        self.grade_store = grade_store
        self.roster = roster

    # ------------------------------------------------------------------
    # 成绩收据：先写 PENDING_ACCEPTANCE，合并确认后才 FINALIZED
    # ------------------------------------------------------------------
    @staticmethod
    def _grading(result: dict[str, Any]) -> dict[str, Any] | None:
        grading = result.get("metadata", {}).get("grading")
        return grading if isinstance(grading, dict) else None

    def _receipt(
        self,
        course: CourseConfiguration,
        result: dict[str, Any],
        grading: dict[str, Any],
        *,
        record_status: str,
        merged: bool = False,
        merge_commit_sha: str | None = None,
        accepted_at: str | None = None,
    ) -> dict[str, Any]:
        metadata = result["metadata"]
        course_id = str(
            course.data.get("course", {}).get("id") or course.name.strip().replace(" ", "-")
        )
        return records.build_receipt(
            course_id=course_id,
            assignment_id=str(metadata.get("assignment_id", "")),
            student_id=str(metadata.get("student_id", "")),
            github_login=str(metadata.get("github_login", "")),
            pr_number=int(metadata["pr_number"]),
            reviewed_head_sha=str(metadata["head_sha"]),
            grading=grading,
            record_status=record_status,
            review_decision=str(result["decision"]),
            merge_commit_sha=merge_commit_sha,
            accepted_at=accepted_at,
            grading_config_sha256=grading.get("config_sha256"),
            source_workflow_run_id=grading.get("workflow_run_id"),
            merged=merged,
        )

    def _write_receipt(
        self,
        course: CourseConfiguration,
        result: dict[str, Any],
        grading: dict[str, Any],
        *,
        record_status: str,
        merged: bool = False,
        merge_commit_sha: str | None = None,
        accepted_at: str | None = None,
    ) -> None:
        if self.grade_store is None:
            raise GradeStoreError("缺少 grades 存储，无法写入成绩收据")
        record = self._receipt(
            course,
            result,
            grading,
            record_status=record_status,
            merged=merged,
            merge_commit_sha=merge_commit_sha,
            accepted_at=accepted_at,
        )
        path = records.receipt_path(record["assignment_id"], record["student_id"])
        self.grade_store.write_json(
            path,
            record,
            f"feat: {record['student_id']} {record['assignment_id']} "
            f"{'成绩确认' if merged else '审核通过待合并'}",
        )
        grading["receipt_status"] = record_status

    def _finalize_receipt(
        self,
        course: CourseConfiguration,
        result: dict[str, Any],
        grading: dict[str, Any],
        *,
        merge_commit_sha: str | None,
        accepted_at: str | None,
    ) -> None:
        try:
            self._write_receipt(
                course,
                result,
                grading,
                record_status="FINALIZED",
                merged=True,
                merge_commit_sha=merge_commit_sha,
                accepted_at=accepted_at,
            )
        except ReviewSystemError as exc:
            # 已合并但收据未写完：不能回滚合并，交给 reconcile 补齐。
            grading["receipt_status"] = "FAILED"
            grading["receipt_error"] = str(exc)
            return
        if course.grading and course.grading.get("storage", {}).get(
            "export_on_finalize", False
        ):
            if self.roster is None:
                grading["report_status"] = "skipped_no_roster"
                return
            try:
                from .grading.export import rebuild_reports

                rebuild_reports(self.grade_store, course, self.roster)
                grading["report_status"] = "rebuilt"
            except Exception as exc:  # noqa: BLE001 - reports are rebuildable
                grading["report_status"] = "failed"
                grading["report_error"] = str(exc)

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

    @staticmethod
    def _close_allowed(
        course: CourseConfiguration, reason_codes: set[str]
    ) -> bool:
        """重复提交（作业已合并）的关闭是课程既定规则，不受 close_late_pr 影响。"""
        if ReasonCode.ALREADY_MERGED_CLOSE_REQUIRED.value in reason_codes:
            return True
        return course.feature_enabled("close_late_pr")

    def _create_comment(self, repository: str, number: int, body: str) -> None:
        repo = urllib.parse.quote(repository, safe="/")
        self.github.post_json(
            f"/repos/{repo}/issues/{number}/comments", {"body": body}
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
            or not reason_codes.intersection(CLOSE_REASON_CODES)
        ):
            raise ReviewSystemError("自动关闭请求缺少关闭原因代码")
        if state != "open":
            return PublicationOutcome(
                skipped="already_closed" if close_requested else "pr_not_open"
            )

        commented = False
        grading = self._grading(result) if course.grading_enabled else None
        receipt_status: str | None = None
        if (
            grading is not None
            and result["decision"] == Decision.PASS.value
            and not close_requested
        ):
            try:
                self._write_receipt(
                    course, result, grading, record_status="PENDING_ACCEPTANCE"
                )
                receipt_status = "PENDING_ACCEPTANCE"
            except ReviewSystemError as exc:
                # Tiered 必须先留下成绩事实再合并，否则合并成功后写失败就得
                # 让学生重来；legacy 允许先合并，由 reconcile 补记录。
                if grading.get("mode") == "tiered":
                    raise
                grading["receipt_status"] = "FAILED"
                grading["receipt_error"] = str(exc)
                receipt_status = "FAILED"

        if course.feature_enabled("comment_review"):
            self._create_comment(repository, number, render_comment(result))
            commented = True

        repo = urllib.parse.quote(repository, safe="/")
        if close_requested and self._close_allowed(course, reason_codes):
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
            merge_commit = response.get("sha")
            if grading is not None:
                self._finalize_receipt(
                    course,
                    result,
                    grading,
                    merge_commit_sha=merge_commit
                    if isinstance(merge_commit, str)
                    else None,
                    accepted_at=response.get("merged_at")
                    if isinstance(response.get("merged_at"), str)
                    else None,
                )
                receipt_status = grading.get("receipt_status")
            return PublicationOutcome(
                commented=commented,
                merged=True,
                receipt_status=receipt_status,
                receipt_error=grading.get("receipt_error") if grading else None,
            )

        return PublicationOutcome(
            commented=commented,
            receipt_status=receipt_status,
            receipt_error=grading.get("receipt_error") if grading else None,
        )
