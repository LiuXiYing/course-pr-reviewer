from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from grading_fixtures import GradingFixture, course_data
from test_grading_export import receipt

from course_pr_reviewer.cli import main
from course_pr_reviewer.grading import records


class GradeCommandTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GradingFixture()
        self.root = Path(tempfile.mkdtemp(prefix="grade-cli-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_cli(self, *args):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(list(args))
        return code, buffer.getvalue()

    def test_validate_reports_modes(self):
        code, output = self.run_cli(
            "grade",
            "validate",
            "--config",
            str(self.fixture.config_path),
            "--students",
            str(self.fixture.students_path),
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["modes"], {"Lab1": "tiered"})
        self.assertEqual(payload["branch"], "grades")
        self.assertEqual(len(payload["bands"]), 6)

    def test_validate_on_course_without_grading(self):
        data = course_data()
        data["features"]["grading"] = False
        data.pop("grading")
        data["assignments"]["Lab1"].pop("grading")
        fixture = GradingFixture(data)
        code, output = self.run_cli(
            "grade",
            "validate",
            "--config",
            str(fixture.config_path),
            "--students",
            str(fixture.students_path),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["grading"], "disabled")

    def test_init_creates_the_branch_layout(self):
        target = self.root / "grades"
        code, output = self.run_cli(
            "grade",
            "init",
            "--config",
            str(self.fixture.config_path),
            "--students",
            str(self.fixture.students_path),
            "--dir",
            str(target),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["branch"], "grades")
        self.assertTrue((target / "metadata.json").is_file())
        self.assertIn("成绩记录分支", (target / "README.md").read_text())

    def test_export_writes_reports(self):
        target = self.root / "grades"
        target.mkdir()
        path = target / records.receipt_path("Lab1", "2023010102")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(receipt()), encoding="utf-8")
        code, output = self.run_cli(
            "grade",
            "export",
            "--config",
            str(self.fixture.config_path),
            "--students",
            str(self.fixture.students_path),
            "--dir",
            str(target),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["receipts"], 1)
        self.assertTrue((target / records.REPORTS["grades"]).is_file())
        self.assertTrue((target / records.REPORTS["submissions"]).is_file())

    def test_dry_run_export_writes_nothing(self):
        target = self.root / "grades"
        target.mkdir()
        code, _ = self.run_cli(
            "grade",
            "export",
            "--config",
            str(self.fixture.config_path),
            "--students",
            str(self.fixture.students_path),
            "--dir",
            str(target),
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertFalse((target / records.REPORTS["grades"]).exists())

    def test_reconcile_requires_github_credentials(self):
        code, _ = self.run_cli(
            "grade",
            "reconcile",
            "--config",
            str(self.fixture.config_path),
            "--students",
            str(self.fixture.students_path),
            "--dir",
            str(self.root / "grades"),
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
