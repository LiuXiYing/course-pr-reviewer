import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openpyxl import Workbook

from course_pr_reviewer.cli import main

ROOT = Path(__file__).parents[1]


class CliTests(unittest.TestCase):
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
