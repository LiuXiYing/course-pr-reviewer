"""Deterministic pull-request review rules."""

from __future__ import annotations

import datetime as dt
import re
import string
from pathlib import PurePosixPath

from . import __version__
from .ai import GlmAIReviewer
from .config import CourseConfiguration, Student, StudentRoster
from .exceptions import InvalidStudentImage, ReviewSystemError
from .models import Decision, Issue, ReasonCode, ReviewResult
from .snapshot import PullRequestSnapshot
from .vision import GlmVisionReviewer

TITLE_PATTERNS = {
    "student_id": r"(?P<student_id>[0-9]{10})",
    "student_name": r"(?P<student_name>.+?)",
    "student_identity": r"(?P<student_identity>.+?)",
    "assignment_id": r"(?P<assignment_id>[A-Za-z][A-Za-z0-9_-]{0,49})",
}


def _title_regex(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    for literal, field_name, format_spec, conversion in string.Formatter().parse(
        template
    ):
        parts.append(re.escape(literal))
        if field_name:
            parts.append(TITLE_PATTERNS[field_name])
    return re.compile("^" + "".join(parts) + "$")


def _issue(code: ReasonCode, message: str, *, file: str | None = None) -> Issue:
    return Issue(code=code, message=message, file=file)


def _configured_assignment_for_path(
    course: CourseConfiguration, student: Student, filename: str
) -> str | None:
    for assignment_id in course.assignments:
        prefix = course.expected_submission_dir(student, assignment_id) + "/"
        if filename.startswith(prefix):
            return assignment_id
    return None


def _required_file_issues(assignment: dict, submitted: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    allowed: set[str] = set()
    for requirement in assignment["required_files"]:
        alternatives = (
            [requirement] if isinstance(requirement, str) else requirement["one_of"]
        )
        allowed.update(alternatives)
        if not submitted.intersection(alternatives):
            label = " 或 ".join(f"`{name}`" for name in alternatives)
            issues.append(
                _issue(ReasonCode.REQUIRED_FILE_MISSING, f"缺少必交文件：{label}")
            )

    if not assignment.get("allow_extra_files", False):
        for extra in sorted(submitted - allowed):
            issues.append(
                _issue(
                    ReasonCode.EXTRA_FILE,
                    f"文件不在本次作业的允许列表中：`{extra}`",
                    file=extra,
                )
            )
    return issues


def _with_defaults(course: CourseConfiguration, assignment: dict) -> dict:
    merged = dict(course.data.get("defaults", {}))
    merged.update(assignment)
    return merged


def review_pull_request(
    course: CourseConfiguration,
    roster: StudentRoster,
    snapshot: PullRequestSnapshot,
    ai_reviewer: GlmAIReviewer | None = None,
    vision_reviewer: GlmVisionReviewer | None = None,
) -> ReviewResult:
    metadata = {
        "course": course.name,
        "repository": snapshot.repository,
        "pr_number": snapshot.number,
        "head_sha": snapshot.current_head_sha,
        "reviewer_version": __version__,
    }

    if snapshot.captured_head_sha != snapshot.current_head_sha:
        return ReviewResult(
            decision=Decision.ERROR,
            summary="PR 在信息收集后又发生了更新，本次审核作废。",
            issues=(
                _issue(
                    ReasonCode.STALE_HEAD_SHA,
                    f"采集 SHA {snapshot.captured_head_sha} 与当前 SHA {snapshot.current_head_sha} 不一致",
                ),
            ),
            metadata=metadata,
        )

    student = roster.find_by_github(snapshot.author_login)
    if student is None:
        return ReviewResult(
            decision=Decision.MANUAL_REVIEW,
            summary="PR 作者尚未登记在学生名单中。",
            issues=(
                _issue(
                    ReasonCode.UNKNOWN_GITHUB_USER,
                    f"未登记 GitHub 账号：{snapshot.author_login}",
                ),
            ),
            metadata=metadata,
        )
    if not student.active:
        return ReviewResult(
            decision=Decision.MANUAL_REVIEW,
            summary="PR 作者的学生记录已停用。",
            issues=(
                _issue(
                    ReasonCode.INACTIVE_STUDENT,
                    f"学生 {student.identity} 的账号映射已停用",
                ),
            ),
            metadata=metadata,
        )

    title_template = course.data["course"].get(
        "title_template", "[{student_id}{student_name}]{assignment_id}作业提交"
    )
    match = _title_regex(title_template).fullmatch(snapshot.title)
    if not match:
        return ReviewResult(
            decision=Decision.FAIL,
            summary="PR 标题不符合课程规范。",
            issues=(
                _issue(
                    ReasonCode.TITLE_MISMATCH,
                    f"当前标题：`{snapshot.title}`；要求格式：`{title_template}`",
                ),
            ),
            metadata=metadata,
        )

    fields = match.groupdict()
    identity_matches = (
        fields.get("student_identity") == student.identity
        if fields.get("student_identity") is not None
        else fields.get("student_id") == student.student_id
        and fields.get("student_name") == student.name
    )
    if not identity_matches:
        return ReviewResult(
            decision=Decision.FAIL,
            summary="PR 标题中的学生身份与 GitHub 账号映射不一致。",
            issues=(
                _issue(
                    ReasonCode.IDENTITY_MISMATCH,
                    f"账号 {snapshot.author_login} 应使用身份 {student.identity}",
                ),
            ),
            metadata=metadata,
        )

    assignment_id = fields["assignment_id"]
    assignment = course.assignment(assignment_id)
    if assignment is None or not assignment.get("enabled", True):
        return ReviewResult(
            decision=Decision.FAIL,
            summary="PR 标题中的作业未配置或未启用。",
            issues=(
                _issue(
                    ReasonCode.ASSIGNMENT_NOT_CONFIGURED,
                    f"作业 {assignment_id} 未配置或未启用",
                ),
            ),
            metadata=metadata,
        )
    metadata["assignment_id"] = assignment_id
    expected_title = course.expected_title(student, assignment_id)
    if snapshot.title != expected_title:
        return ReviewResult(
            decision=Decision.FAIL,
            summary="PR 标题不是根据学生名单生成的精确标题。",
            issues=(
                _issue(ReasonCode.TITLE_MISMATCH, f"正确标题应为：`{expected_title}`"),
            ),
            metadata=metadata,
        )

    if not snapshot.files:
        return ReviewResult(
            decision=Decision.FAIL,
            summary="PR 不包含任何文件变更。",
            issues=(_issue(ReasonCode.NO_FILES_CHANGED, "未检测到变更文件"),),
            metadata=metadata,
        )

    assignment = _with_defaults(course, assignment)
    expected_dir = course.expected_submission_dir(student, assignment_id)
    expected_prefix = expected_dir + "/"
    assignment_order = list(course.assignments)
    current_index = assignment_order.index(assignment_id)
    submitted: set[str] = set()
    issues: list[Issue] = []

    for changed in snapshot.files:
        if changed.status == "removed":
            issues.append(
                _issue(ReasonCode.FILE_DELETED, "不允许删除文件", file=changed.filename)
            )
        if changed.status == "renamed" or changed.previous_filename:
            issues.append(
                _issue(
                    ReasonCode.FILE_RENAMED, "不允许重命名文件", file=changed.filename
                )
            )

        if not changed.filename.startswith(expected_prefix):
            path_assignment = _configured_assignment_for_path(
                course, student, changed.filename
            )
            if (
                path_assignment
                and assignment_order.index(path_assignment) < current_index
            ):
                issues.append(
                    _issue(
                        ReasonCode.OLD_ASSIGNMENT_MODIFIED,
                        "不允许在新作业 PR 中修改旧作业",
                        file=changed.filename,
                    )
                )
            else:
                issues.append(
                    _issue(
                        ReasonCode.PATH_OUT_OF_SCOPE,
                        f"只允许修改 `{expected_prefix}` 下的文件",
                        file=changed.filename,
                    )
                )
            continue

        if changed.status != "removed":
            relative = str(PurePosixPath(changed.filename).relative_to(expected_dir))
            submitted.add(relative)

    issues.extend(_required_file_issues(assignment, submitted))

    deadline = dt.datetime.fromisoformat(assignment["deadline"])
    if snapshot.event_at > deadline:
        late_by = snapshot.event_at - deadline
        hours = int(late_by.total_seconds() // 3600)
        metadata["late_seconds"] = int(late_by.total_seconds())
        close_after_days = assignment.get("late_close_after_days", 7)
        close_required = course.feature_enabled(
            "close_late_pr"
        ) and late_by >= dt.timedelta(days=close_after_days)
        if close_required:
            metadata["close_pr"] = True
        issues.append(
            _issue(
                (
                    ReasonCode.LATE_PR_CLOSE_REQUIRED
                    if close_required
                    else ReasonCode.DEADLINE_EXCEEDED
                ),
                (
                    f"本次 PR 创建时间晚于截止时间 {hours} 小时；"
                    f"超过 {close_after_days} 天关闭阈值"
                    if close_required
                    else f"本次 PR 创建时间晚于截止时间 {hours} 小时"
                ),
            )
        )

    if issues:
        return ReviewResult(
            decision=Decision.FAIL,
            summary=f"确定性审核发现 {len(issues)} 个问题。",
            issues=tuple(issues),
            metadata=metadata,
        )

    if course.assignment_feature_enabled(assignment_id, "ai_review"):
        if ai_reviewer is None:
            return ReviewResult(
                decision=Decision.ERROR,
                summary="已启用 AI 审核，但所选 AI 审核器未正确配置。",
                issues=(
                    _issue(
                        ReasonCode.SERVICE_ERROR,
                        f"缺少 {course.ai['provider']} 所需 API Key 或审核器初始化失败",
                    ),
                ),
                metadata=metadata,
            )
        try:
            ai_outcome = ai_reviewer.review(course, assignment_id, snapshot)
        except ReviewSystemError as exc:
            return ReviewResult(
                decision=Decision.ERROR,
                summary="AI 内容审核无法可靠完成。",
                issues=(_issue(ReasonCode.SERVICE_ERROR, str(exc)),),
                metadata=metadata,
            )
        metadata["ai_provider"] = course.ai["provider"]
        metadata["ai_model"] = course.ai["model"]
        metadata.update(
            {f"ai_{key}": value for key, value in ai_outcome.metadata.items()}
        )
        if ai_outcome.confidence is not None:
            metadata["ai_confidence"] = ai_outcome.confidence
        if ai_outcome.decision is not Decision.PASS:
            return ReviewResult(
                decision=ai_outcome.decision,
                summary=ai_outcome.summary,
                issues=ai_outcome.issues,
                confidence=ai_outcome.confidence,
                metadata=metadata,
            )

    if course.assignment_feature_enabled(assignment_id, "vision_review"):
        if vision_reviewer is None:
            return ReviewResult(
                decision=Decision.ERROR,
                summary="已启用图片审核，但所选图片审核器未正确配置。",
                issues=(
                    _issue(
                        ReasonCode.SERVICE_ERROR,
                        f"缺少 {course.vision['provider']} 所需 API Key、GitHub Token "
                        "或图片审核器初始化失败",
                    ),
                ),
                metadata=metadata,
            )
        try:
            vision_outcome = vision_reviewer.review(
                course, assignment_id, snapshot, expected_dir
            )
        except InvalidStudentImage as exc:
            return ReviewResult(
                decision=Decision.FAIL,
                summary="提交的图片文件无效或超过安全限制。",
                issues=(_issue(ReasonCode.INVALID_FILE, str(exc)),),
                metadata=metadata,
            )
        except ReviewSystemError as exc:
            return ReviewResult(
                decision=Decision.ERROR,
                summary="OCR 或 AI 图片审核无法可靠完成。",
                issues=(_issue(ReasonCode.SERVICE_ERROR, str(exc)),),
                metadata=metadata,
            )
        metadata["vision_provider"] = course.vision["provider"]
        metadata["vision_model"] = course.vision["model"]
        metadata.update(
            {f"vision_{key}": value for key, value in vision_outcome.metadata.items()}
        )
        if vision_outcome.confidence is not None:
            metadata["vision_confidence"] = vision_outcome.confidence
        if vision_outcome.decision is not Decision.PASS:
            return ReviewResult(
                decision=vision_outcome.decision,
                summary=vision_outcome.summary,
                issues=vision_outcome.issues,
                confidence=vision_outcome.confidence,
                metadata=metadata,
            )

    return ReviewResult(
        decision=Decision.PASS,
        summary=(
            "账号、标题、文件范围、必交文件、截止时间、head SHA 和已启用的内容审核均已通过。"
        ),
        metadata=metadata,
    )
