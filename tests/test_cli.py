import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from course_pr_reviewer.cli import main
from course_pr_reviewer.models import Decision, Issue, ReasonCode, ReviewResult

ROOT = Path(__file__).parents[1]


class CliTests(unittest.TestCase):
    @staticmethod
    def _review_result(path: Path, decision: Decision) -> None:
        issues = (
            ()
            if decision is Decision.PASS
            else (Issue(code=ReasonCode.AI_UNCERTAIN, message="需要人工确认"),)
        )
        result = ReviewResult(
            decision=decision,
            summary="审核结果摘要",
            issues=issues,
            metadata={
                "repository": "teacher/course",
                "pr_number": 7,
                "head_sha": "a" * 40,
            },
        )
        path.write_text(result.to_json() + "\n", encoding="utf-8")

    def test_runtime_requirements_reports_ocr(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "runtime-requirements",
                    "--config",
                    str(ROOT / "examples/course-review.yml"),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue().strip(), "ocr")

    def test_validate_command(self):
        exit_code = main(
            [
                "validate",
                "--config",
                str(ROOT / "examples/course-review.yml"),
                "--students",
                str(ROOT / "examples/students.yml"),
            ]
        )
        self.assertEqual(exit_code, 0)

    def test_notify_skips_pass_without_smtp_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            self._review_result(result_path, Decision.PASS)
            with patch.dict(os.environ, {}, clear=True):
                exit_code = main(
                    [
                        "notify",
                        "--config",
                        str(ROOT / "examples/course-review.yml"),
                        "--result-file",
                        str(result_path),
                    ]
                )
        self.assertEqual(exit_code, 0)

    @patch("course_pr_reviewer.cli.TeacherEmailNotifier")
    def test_notify_marks_manual_review_after_email(self, notifier):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            self._review_result(result_path, Decision.MANUAL_REVIEW)
            environment = {
                "TEACHER_EMAIL": "teacher@example.com",
                "SMTP_USERNAME": "sender@example.com",
                "SMTP_PASSWORD": "secret",
                "GITHUB_REPOSITORY": "teacher/course",
                "GITHUB_RUN_ID": "123",
            }
            with patch.dict(os.environ, environment, clear=True):
                exit_code = main(
                    [
                        "notify",
                        "--config",
                        str(ROOT / "examples/course-review.yml"),
                        "--result-file",
                        str(result_path),
                    ]
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["metadata"]["teacher_email_notification"], "sent")
        notifier.return_value.send.assert_called_once()

    def test_notify_records_missing_smtp_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            self._review_result(result_path, Decision.ERROR)
            with patch.dict(os.environ, {}, clear=True):
                exit_code = main(
                    [
                        "notify",
                        "--config",
                        str(ROOT / "examples/course-review.yml"),
                        "--result-file",
                        str(result_path),
                    ]
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 2)
        self.assertEqual(result["metadata"]["teacher_email_notification"], "failed")

    def test_review_fails_closed_when_ai_key_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata_dir = Path(directory) / "pr-info"
            metadata_dir.mkdir()
            (metadata_dir / "snapshot.json").write_text(
                json.dumps(
                    {
                        "repository": "teacher/course",
                        "number": 1,
                        "title": "[2023010102刘西莹]Lab1作业提交",
                        "author_login": "example-user",
                        "captured_head_sha": "a" * 40,
                        "current_head_sha": "a" * 40,
                        "event_at": "2026-09-01T12:00:00+08:00",
                        "files": [
                            {
                                "filename": "2023010102刘西莹/Lab1/Lab1.md",
                                "status": "added",
                            },
                            {
                                "filename": "2023010102刘西莹/Lab1/result.png",
                                "status": "added",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result_path = Path(directory) / "result.json"
            exit_code = main(
                [
                    "review",
                    "--config",
                    str(ROOT / "examples/course-review.yml"),
                    "--students",
                    str(ROOT / "examples/students.yml"),
                    "--metadata-dir",
                    str(metadata_dir),
                    "--result-file",
                    str(result_path),
                ]
            )
            self.assertEqual(exit_code, 1)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["decision"], "ERROR")
            self.assertIn("SERVICE_ERROR", result["reason_codes"])

    def test_import_students_command(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.append(["学号", "姓名", "github账号名"])
            worksheet.append([2023010102, "刘西莹", "example-user"])
            workbook.save(excel)
            workbook.close()
            exit_code = main(
                [
                    "import-students",
                    "--excel",
                    str(excel),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("2023010102", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
