"""Command-line entry point used locally and by the composite action."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from . import __version__
from .ai import AIClient, GeminiClient, GlmAIReviewer, GlmClient
from .config import load_course_config, load_student_roster
from .consensus import TextConsensusReviewer, VisionConsensusReviewer
from .exceptions import ConfigurationError, ReviewerError
from .feedback import add_ai_feedback
from .grading.export import rebuild_reports
from .grading.reconcile import reconcile
from .grading.store import GitHubContentsStore, open_store
from .models import Decision, Issue, ReasonCode, ReviewResult
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
    publish.add_argument(
        "--students", default=None, help="roster used to refresh grade reports"
    )

    grade = subparsers.add_parser(
        "grade", help="validate, initialise, export or reconcile the grades branch"
    )
    grade_sub = grade.add_subparsers(dest="grade_command", required=True)
    for name, help_text in (
        ("validate", "check grading configuration without touching any branch"),
        ("init", "create the grades branch with metadata and README"),
        ("export", "regenerate reports/ from receipts and overrides"),
        ("reconcile", "promote PENDING receipts once the PR is really merged"),
    ):
        sub = grade_sub.add_parser(name, help=help_text)
        sub.add_argument("--config", required=True)
        sub.add_argument("--students", required=True)
        sub.add_argument(
            "--repository",
            default=os.environ.get("GITHUB_REPOSITORY", ""),
            help="owner/name; defaults to GITHUB_REPOSITORY",
        )
        sub.add_argument(
            "--dir",
            default=None,
            help="use a local directory instead of the GitHub grades branch",
        )
        sub.add_argument("--dry-run", action="store_true")
        if name == "reconcile":
            sub.add_argument(
                "--since",
                default=None,
                help="only reconcile receipts submitted at or after this ISO time",
            )
            sub.add_argument("--only-pr", type=int, default=None)
        if name == "export":
            sub.add_argument("--rebuild-reports", action="store_true", default=True)

    notify = subparsers.add_parser(
        "notify", help="legacy no-op; email notifications are disabled"
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
        grading = result.metadata.get("grading")
        grading = grading if isinstance(grading, dict) else {}
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"decision={result.decision.value}\n")
            output.write(f"result-json={compact}\n")
            output.write(f"head_sha={result.metadata.get('head_sha', '')}\n")
            output.write(f"grading_mode={grading.get('mode', '')}\n")
            output.write(f"graded_status={grading.get('time_status', '')}\n")
            output.write(f"score={grading.get('awarded_points') or ''}\n")
            output.write(f"receipt_status={grading.get('receipt_status', '')}\n")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## Course PR Reviewer\n\n")
            summary.write(f"- Decision: `{result.decision.value}`\n")
            summary.write(f"- Summary: {result.summary}\n")
            grading = result.metadata.get("grading")
            if isinstance(grading, dict):
                summary.write(
                    f"- Grading: `{grading.get('mode')}` / "
                    f"{grading.get('time_status')} / "
                    f"{grading.get('awarded_points') or '未计分'}\n"
                )


def _workflow_run_id() -> int | None:
    raw = os.environ.get("GITHUB_RUN_ID", "")
    return int(raw) if raw.isdigit() else None


def _grade_store(course, github=None):
    """The ``grades`` branch of the current repository, when grading is on."""
    if not course.grading_enabled:
        return None
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if github is None or not repository:
        return None
    return GitHubContentsStore(github, repository, branch=course.grading_branch)


def _grade(args) -> int:
    """Operator commands for the grades branch (validate/init/export/reconcile)."""
    course = load_course_config(args.config)
    roster = load_student_roster(args.students)
    if not course.grading_enabled:
        print(
            json.dumps({"grading": "disabled", "course": course.name}, ensure_ascii=False)
        )
        return 0 if args.grade_command == "validate" else 2

    if args.grade_command == "validate":
        settings = course.grading or {}
        print(
            json.dumps(
                {
                    "valid": True,
                    "course": course.name,
                    "branch": course.grading_branch,
                    "policy_version": course.grading_policy_version,
                    "final_accept_at": course.data["course"].get("final_accept_at"),
                    "bands": settings.get("defaults", {})
                    .get("tiered", {})
                    .get("bands"),
                    "modes": {
                        key: course.grade_mode(key) for key in course.assignments
                    },
                    "students": len(roster.by_student_id),
                },
                ensure_ascii=False,
            )
        )
        return 0

    repository = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    github = (
        GitHubClient(
            token, api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com")
        )
        if token and not args.dir
        else None
    )
    if github is None and not args.dir:
        print("缺少 GH_TOKEN，无法访问 GitHub；可改用 --dir", file=sys.stderr)
        return 2
    store = open_store(
        root=args.dir,
        github=github,
        repository=repository,
        branch=course.grading_branch,
        dry_run=args.dry_run,
    )

    if args.grade_command == "init":
        metadata = store.ensure_branch(
            course_id=str(
                course.data.get("course", {}).get("id")
                or course.name.strip().replace(" ", "-")
            ),
            course_name=course.name,
            timezone=course.timezone,
        )
        print(
            json.dumps(
                {
                    "branch": course.grading_branch,
                    "dry_run": args.dry_run,
                    "metadata": metadata,
                },
                ensure_ascii=False,
            )
        )
        return 0

    store.ensure_branch(
        course_id=str(
            course.data.get("course", {}).get("id")
            or course.name.strip().replace(" ", "-")
        ),
        course_name=course.name,
        timezone=course.timezone,
    )

    if args.grade_command == "export":
        result = rebuild_reports(store, course, roster)
        print(
            json.dumps(
                {
                    "receipts": result["receipts"],
                    "overrides": result["overrides"],
                    "errors": result["errors"],
                    "dry_run": args.dry_run,
                },
                ensure_ascii=False,
            )
        )
        return 0

    since = dt.datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        print("--since 必须包含时区", file=sys.stderr)
        return 2
    if github is None:
        print("reconcile 需要 GitHub 凭据", file=sys.stderr)
        return 2
    report = reconcile(
        store,
        course,
        roster,
        github,
        repository=repository,
        since=since,
        only_pr=args.only_pr,
    )
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0


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
        github=github,
        grade_store=_grade_store(course, github),
        workflow_run_id=_workflow_run_id(),
    )
    # Persist the authoritative result before any optional explanation request.
    Path(result_file).write_text(result.to_json() + "\n", encoding="utf-8")
    result = add_ai_feedback(course, roster, snapshot, result, configured_client)
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
    # Keep old workflow invocations harmless, even when review/config failed.
    # Do not read credentials or mutate the authoritative review result.
    print(json.dumps({"email": "disabled"}, ensure_ascii=False))
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
            roster = (
                load_student_roster(args.students)
                if getattr(args, "students", None)
                else None
            )
            outcome = GitHubResultPublisher(
                github,
                merge_github=merge_github,
                grade_store=_grade_store(course, github),
                roster=roster,
            ).publish(course, result, expected_repository=expected_repository)
            print(json.dumps(outcome.__dict__, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "grade":
            return _grade(args)
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
