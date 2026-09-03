"""Command-line entry point used locally and by the composite action."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .ai import AIClient, GeminiClient, GlmAIReviewer, GlmClient
from .config import load_course_config, load_student_roster
from .consensus import TextConsensusReviewer, VisionConsensusReviewer
from .exceptions import ConfigurationError, ReviewerError
from .models import Decision, Issue, ReasonCode, ReviewResult
from .notifications import TeacherEmailNotifier, notification_required
from .publisher import GitHubResultPublisher, load_result
from .reviewer import review_pull_request
from .roster_import import import_students_excel
from .snapshot import GitHubClient, load_snapshot
from .vision import GlmVisionReviewer, PaddleOCREngine


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="course-pr-reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate course and roster YAML")
    validate.add_argument("--config", required=True)
    validate.add_argument("--students", required=True)

    review = subparsers.add_parser(
        "review", help="run a fail-closed deterministic PR review"
    )
    review.add_argument("--config", required=True)
    review.add_argument("--students", required=True)
    review.add_argument("--metadata-dir", required=True)
    review.add_argument("--result-file", default="review-result.json")

    publish = subparsers.add_parser(
        "publish", help="comment, merge, or close a reviewed PR safely"
    )
    publish.add_argument("--config", required=True)
    publish.add_argument("--result-file", required=True)

    notify = subparsers.add_parser(
        "notify", help="email the teacher when a review requires human intervention"
    )
    notify.add_argument("--config", required=True)
    notify.add_argument("--result-file", required=True)

    roster_import = subparsers.add_parser(
        "import-students", help="convert a three-column .xlsx roster to students.yml"
    )
    roster_import.add_argument("--excel", required=True)
    roster_import.add_argument("--output", required=True)
    roster_import.add_argument("--force", action="store_true")

    requirements = subparsers.add_parser(
        "runtime-requirements",
        help="print optional runtime groups required by a course config",
    )
    requirements.add_argument("--config", required=True)
    return parser


def _write_github_output(result: ReviewResult) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        compact = result.to_json(indent=None)
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"decision={result.decision.value}\n")
            output.write(f"result-json={compact}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## Course PR Reviewer\n\n")
            summary.write(f"- Decision: `{result.decision.value}`\n")
            summary.write(f"- Summary: {result.summary}\n")


def _validate(config_path: str, students_path: str) -> int:
    course = load_course_config(config_path)
    roster = load_student_roster(students_path)
    print(
        json.dumps(
            {
                "valid": True,
                "course": course.name,
                "assignments": sorted(course.assignments),
                "students": len(roster.by_student_id),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _review(
    config_path: str, students_path: str, metadata_dir: str, result_file: str
) -> int:
    course = load_course_config(config_path)
    roster = load_student_roster(students_path)
    snapshot = load_snapshot(
        metadata_dir,
        github_token=os.environ.get("GH_TOKEN"),
        repository=os.environ.get("GITHUB_REPOSITORY"),
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    needs_ai = any(
        course.assignment_feature_enabled(assignment_id, "ai_review")
        for assignment_id in course.assignments
    )
    needs_vision = any(
        course.assignment_feature_enabled(assignment_id, "vision_review")
        for assignment_id in course.assignments
    )
    needs_ocr = any(
        course.assignment_feature_enabled(assignment_id, "ocr_review")
        for assignment_id in course.assignments
    )
    github_token = os.environ.get("GH_TOKEN", "")
    github = (
        GitHubClient(
            github_token,
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        if github_token
        else None
    )
    def configured_client(settings: dict) -> AIClient | None:
        provider = settings["provider"]
        if provider == "gemini":
            key = os.environ.get("GEMINI_API_KEY", "")
            return GeminiClient(key) if key else None
        key = os.environ.get("GLM_API_KEY", "")
        return GlmClient(key) if key else None

    ai_reviewers = {}
    if needs_ai:
        for settings in course.ai_providers:
            client = configured_client(settings)
            if client is None:
                ai_reviewers = {}
                break
            ai_reviewers[settings["provider"]] = GlmAIReviewer(
                client, github, settings=settings
            )
    ai_reviewer = None
    if len(ai_reviewers) == 1:
        ai_reviewer = next(iter(ai_reviewers.values()))
    elif len(ai_reviewers) == 2:
        ai_reviewer = TextConsensusReviewer(
            ai_reviewers, max_rounds=course.ai_consensus_rounds
        )

    vision_reviewer = None
    vision_reviewers = {}
    if github and needs_vision:
        for settings in course.vision_providers:
            client = configured_client(settings)
            if client is None:
                vision_reviewers = {}
                break
            ocr_engine = PaddleOCREngine(course.ocr) if needs_ocr else None
            vision_reviewers[settings["provider"]] = GlmVisionReviewer(
                client,
                github,
                ocr_engine=ocr_engine,
                settings=settings,
            )
    if len(vision_reviewers) == 1:
        vision_reviewer = next(iter(vision_reviewers.values()))
    elif len(vision_reviewers) == 2:
        vision_reviewer = VisionConsensusReviewer(
            vision_reviewers, max_rounds=course.vision_consensus_rounds
        )
    result = review_pull_request(
        course,
        roster,
        snapshot,
        ai_reviewer=ai_reviewer,
        vision_reviewer=vision_reviewer,
    )
    Path(result_file).write_text(result.to_json() + "\n", encoding="utf-8")
    _write_github_output(result)
    print(result.to_json())
    return 0 if result.decision is Decision.PASS else 1


def _write_error_result(message: str, result_file: str, code: ReasonCode) -> None:
    result = ReviewResult(
        decision=Decision.ERROR,
        summary="审核器无法可靠地完成本次审核。",
        issues=(Issue(code=code, message=message),),
        metadata={"reviewer_version": __version__},
    )
    Path(result_file).write_text(result.to_json() + "\n", encoding="utf-8")
    _write_github_output(result)
    print(result.to_json())


def _notify(config_path: str, result_file: str) -> int:
    course = load_course_config(config_path)
    result = load_result(result_file)
    if not notification_required(result):
        print(json.dumps({"email": "skipped"}, ensure_ascii=False))
        return 0

    metadata = result.setdefault("metadata", {})
    required_environment = {
        "TEACHER_EMAIL": os.environ.get("TEACHER_EMAIL", ""),
        "SMTP_USERNAME": os.environ.get("SMTP_USERNAME", ""),
        "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    }
    missing = [name for name, value in required_environment.items() if not value]
    if missing:
        metadata["teacher_email_notification"] = "failed"
        metadata["teacher_email_notification_error"] = (
            "缺少邮件配置：" + "、".join(missing)
        )
        Path(result_file).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(metadata["teacher_email_notification_error"], file=sys.stderr)
        return 2

    try:
        port = int(os.environ.get("SMTP_PORT", "465"))
        notifier = TeacherEmailNotifier(
            recipient=required_environment["TEACHER_EMAIL"],
            username=required_environment["SMTP_USERNAME"],
            password=required_environment["SMTP_PASSWORD"],
            host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            port=port,
        )
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        run_url = (
            f"{server_url}/{repository}/actions/runs/{run_id}"
            if repository and run_id
            else ""
        )
        notifier.send(
            course_name=course.name,
            result=result,
            repository=repository,
            run_url=run_url,
        )
    except (ValueError, ReviewSystemError) as exc:
        metadata["teacher_email_notification"] = "failed"
        metadata["teacher_email_notification_error"] = str(exc)
        Path(result_file).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(str(exc), file=sys.stderr)
        return 2

    metadata["teacher_email_notification"] = "sent"
    metadata.pop("teacher_email_notification_error", None)
    Path(result_file).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"email": "sent"}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate(args.config, args.students)
        if args.command == "import-students":
            count = import_students_excel(args.excel, args.output, force=args.force)
            print(
                json.dumps(
                    {"imported": count, "output": args.output}, ensure_ascii=False
                )
            )
            return 0
        if args.command == "runtime-requirements":
            course = load_course_config(args.config)
            groups = []
            if any(
                course.assignment_feature_enabled(assignment_id, "ocr_review")
                for assignment_id in course.assignments
            ):
                groups.append("ocr")
            print(" ".join(groups))
            return 0
        if args.command == "notify":
            return _notify(args.config, args.result_file)
        if args.command == "publish":
            course = load_course_config(args.config)
            token = os.environ.get("GH_TOKEN", "")
            expected_repository = os.environ.get("GITHUB_REPOSITORY", "")
            github = GitHubClient(
                token,
                api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            )
            merge_token = os.environ.get("MERGE_TOKEN", "")
            merge_github = (
                GitHubClient(
                    merge_token,
                    api_url=os.environ.get(
                        "GITHUB_API_URL", "https://api.github.com"
                    ),
                )
                if merge_token
                else None
            )
            result = load_result(args.result_file)
            outcome = GitHubResultPublisher(github, merge_github=merge_github).publish(
                course, result, expected_repository=expected_repository
            )
            print(json.dumps(outcome.__dict__, ensure_ascii=False, sort_keys=True))
            return 0
        return _review(args.config, args.students, args.metadata_dir, args.result_file)
    except ReviewerError as exc:
        if args.command == "review":
            code = (
                ReasonCode.CONFIG_ERROR
                if isinstance(exc, ConfigurationError)
                else ReasonCode.SERVICE_ERROR
            )
            _write_error_result(str(exc), args.result_file, code)
        else:
            print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
