import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import Workbook

from course_pr_reviewer.cli import main
from course_pr_reviewer.models import Decision, Issue, ReasonCode, ReviewResult
from course_pr_reviewer.publisher import load_result
from course_pr_reviewer.snapshot import snapshot_from_dict

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

    @patch("smtplib.SMTP_SSL")
    def test_notify_is_disabled_for_every_decision_with_or_without_credentials(self, smtp):
        environments = [
            {},
            {
                "TEACHER_EMAIL": "teacher@example.com",
                "SMTP_USERNAME": "sender@example.com",
                "SMTP_PASSWORD": "secret",
                "SMTP_PORT": "465",
            },
            {"SMTP_PORT": "invalid-port"},
        ]
        for decision in Decision:
            for environment in environments:
                with self.subTest(decision=decision, configured=bool(environment)):
                    with tempfile.TemporaryDirectory() as directory:
                        result_path = Path(directory) / "result.json"
                        self._review_result(result_path, decision)
                        original = result_path.read_bytes()
                        output = io.StringIO()
                        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
                            exit_code = main([
                                "notify", "--config", str(ROOT / "examples/course-review.yml"),
                                "--result-file", str(result_path),
                            ])
                        self.assertEqual(exit_code, 0)
                        self.assertEqual(json.loads(output.getvalue()), {"email": "disabled"})
                        self.assertEqual(result_path.read_bytes(), original)
        smtp.assert_not_called()

    def test_notify_ignores_missing_config_and_result_after_review_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "missing.yml"
            result_path = Path(directory) / "missing.json"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "notify", "--config", str(config_path), "--result-file", str(result_path),
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"email": "disabled"})
            self.assertFalse(result_path.exists())

    def test_notify_ignores_invalid_config_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "invalid.yml"
            result_path = Path(directory) / "invalid.json"
            config_path.write_text("[invalid config")
            result_path.write_text("not JSON")
            with redirect_stdout(io.StringIO()):
                exit_code = main([
                    "notify", "--config", str(config_path), "--result-file", str(result_path),
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(result_path.read_text(), "not JSON")

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

    def test_review_persists_original_before_feedback_and_keeps_failure_exit_code(self):
        snapshot = snapshot_from_dict({
            "repository": "teacher/course", "number": 24,
            "title": "[2023010102刘西莹]Lab1作业提交", "author_login": "example-user",
            "captured_head_sha": "a" * 40, "current_head_sha": "a" * 40,
            "event_at": "2026-09-01T12:00:00+08:00", "files": [],
        })
        for fail in (False, True):
            with self.subTest(provider_fails=fail), tempfile.TemporaryDirectory() as directory:
                result_path = Path(directory) / "result.json"
                output_path = Path(directory) / "github-output"
                client = Mock()
                saved_before_feedback = []

                def complete(**kwargs):
                    saved = load_result(result_path)
                    self.assertEqual(saved["decision"], "FAIL")
                    self.assertEqual(saved["reason_codes"], ["NO_FILES_CHANGED"])
                    self.assertNotIn("ai_feedback", saved["metadata"])
                    saved_before_feedback.append(saved)
                    if fail:
                        raise TimeoutError("feedback provider timed out")
                    return {"choices": [{"message": {"content": json.dumps({
                        "summary": "请提交本次作业的文件。",
                        "groups": [{
                            "title": "缺少文件变更", "issue_numbers": [1],
                            "explanation": "PR 不包含任何文件变更。",
                            "suggestions": ["按课程要求提交文件并更新当前 PR。"],
                        }],
                    }, ensure_ascii=False)}}]}

                client.complete.side_effect = complete
                with (
                    patch.dict(os.environ, {
                        "GLM_API_KEY": "test-key", "GITHUB_OUTPUT": str(output_path),
                    }, clear=True),
                    patch("course_pr_reviewer.cli.load_snapshot", return_value=snapshot),
                    patch("course_pr_reviewer.cli.GlmClient", return_value=client),
                    patch("course_pr_reviewer.feedback.LOGGER"),
                    redirect_stdout(io.StringIO()),
                ):
                    exit_code = main([
                        "review", "--config", str(ROOT / "examples/course-review.yml"),
                        "--students", str(ROOT / "examples/students.yml"),
                        "--metadata-dir", directory, "--result-file", str(result_path),
                    ])
                result = load_result(result_path)
                self.assertEqual(exit_code, 1)
                client.complete.assert_called_once()
                self.assertEqual(len(saved_before_feedback), 1)
                original = saved_before_feedback[0]
                self.assertEqual(result["summary"], original["summary"])
                self.assertEqual(result["issues"], original["issues"])
                self.assertEqual(result["reason_codes"], original["reason_codes"])
                self.assertEqual(result["decision"], original["decision"])
                self.assertEqual(
                    result["metadata"]["ai_feedback"]["status"],
                    "unavailable" if fail else "generated",
                )
                self.assertIn("decision=FAIL", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
