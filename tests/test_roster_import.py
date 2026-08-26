from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from openpyxl import Workbook

from course_pr_reviewer.exceptions import ConfigurationError
from course_pr_reviewer.roster_import import import_students_excel


class RosterImportTests(unittest.TestCase):
    def workbook(self, path: Path, rows, headers=("学号", "姓名", "github账号名")):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        workbook.close()

    def test_three_columns_are_converted_to_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            self.workbook(
                excel,
                [
                    (2023010102, "刘西莹", "Example-User"),
                    ("2023010103", "张三", "zhangsan-github"),
                ],
            )
            count = import_students_excel(excel, output)
            data = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertEqual(count, 2)
        self.assertEqual(data["students"]["2023010102"]["name"], "刘西莹")
        self.assertTrue(data["students"]["2023010102"]["active"])

    def test_duplicate_github_account_is_rejected_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            self.workbook(
                excel,
                [
                    (2023010102, "刘西莹", "Same-User"),
                    (2023010103, "张三", "same-user"),
                ],
            )
            with self.assertRaisesRegex(ConfigurationError, "同时对应"):
                import_students_excel(excel, output)
            self.assertFalse(output.exists())

    def test_exact_headers_and_only_three_columns_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            self.workbook(
                excel,
                [(2023010102, "刘西莹", "example-user", "备注")],
                headers=("学号", "姓名", "github账号名", "备注"),
            )
            with self.assertRaisesRegex(ConfigurationError, "只允许三列"):
                import_students_excel(excel, output)

    def test_data_in_unnamed_fourth_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            self.workbook(
                excel,
                [(2023010102, "刘西莹", "example-user", "隐藏备注")],
                headers=("学号", "姓名", "github账号名", None),
            )
            with self.assertRaisesRegex(ConfigurationError, "多余内容"):
                import_students_excel(excel, output)

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            self.workbook(excel, [(2023010102, "刘西莹", "example-user")])
            output.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "--force"):
                import_students_excel(excel, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_corrupt_xlsx_is_reported_as_configuration_error(self):
        with tempfile.TemporaryDirectory() as directory:
            excel = Path(directory) / "students.xlsx"
            output = Path(directory) / "students.yml"
            excel.write_text("not an Excel workbook", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "无法读取 Excel"):
                import_students_excel(excel, output)


if __name__ == "__main__":
    unittest.main()
