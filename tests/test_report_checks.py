from __future__ import annotations

import unittest

from course_pr_reviewer.models import ReasonCode
from course_pr_reviewer.report_checks import required_address_issues


REQUIREMENTS = [
    {"file": "Lab5.md", "section": "3.6.4",
     "row": "当前 data 地址（图 C 为 v.data）", "columns": [1, 2, 3]},
    {"file": "Lab5.md", "section": "3.6.4",
     "row": "newData 地址", "columns": [2]},
]


def report(addresses=("（填写）",) * 4):
    a, b, c, new = addresses
    return (
        "#### 3.6.4 必填：Memory View 观察记录\n\n"
        "| 记录项目 | 图 A：扩容前 | 图 B：复制完成 | 图 C：追加完成 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        f"| 当前 `data` 地址（图 C 为 `v.data`） | {a} | {b} | {c} |\n"
        f"| `newData` 地址 | 尚未申请，不填写 | {new} | 已离开作用域，不填写 |\n"
        "#### 3.6.5 截图检查标准\n"
    )


class RequiredAddressTests(unittest.TestCase):
    def check(self, content):
        return required_address_issues(content, REQUIREMENTS, "student/Lab5/Lab5.md")

    def test_unchanged_observation_table_reports_all_four_required_addresses(self):
        issues = self.check(report())
        self.assertEqual(len(issues), 4)
        self.assertEqual({i.code for i in issues}, {ReasonCode.REQUIRED_ADDRESS_MISSING})
        self.assertTrue(all(i.evidence in report() for i in issues))
        self.assertTrue(all(i.file == "student/Lab5/Lab5.md" for i in issues))

    def test_each_required_address_rejects_blanks_and_placeholders(self):
        for value in ("", "  ", "填写", "（填写）", "待填写", "___", "0x...", "0x123g"):
            for column in range(4):
                with self.subTest(value=value, column=column):
                    addresses = ["0xfc1820", "0xfc1820", "0xfc1830", "0xfc1830"]
                    addresses[column] = value
                    self.assertEqual(len(self.check(report(addresses))), 1)

    def test_filled_addresses_accept_markdown_case_and_unprefixed_hex(self):
        addresses = ("`0xfc1820`", "**0X0000FC1820**", "__`0xfc1830`__", "FC1830")
        self.assertEqual(self.check(report(addresses)), [])

    def test_exempt_newdata_cells_may_keep_the_template_text(self):
        self.assertEqual(self.check(report(("0x1000", "0x1000", "0x2000", "0x2000"))), [])

    def test_example_addresses_outside_the_section_cannot_replace_answers(self):
        filled = report(("0x1000", "0x1000", "0x2000", "0x2000"))
        example = filled.replace("3.6.4 必填", "3.6.3 示例")
        self.assertEqual(len(self.check(example + report())), 4)
        self.assertEqual(len(self.check(report() + example)), 4)

    def test_hidden_or_fenced_table_is_not_a_completed_observation(self):
        filled = report(("0x1000", "0x1000", "0x2000", "0x2000"))
        for hidden in (f"<!--\n{filled}\n-->", f"```markdown\n{filled}\n```"):
            with self.subTest(hidden=hidden):
                self.assertEqual(len(self.check(hidden)), 2)
                self.assertEqual(len(self.check(hidden + "\n" + report())), 4)

    def test_removed_section_or_address_row_is_rejected(self):
        self.assertEqual(len(self.check("# 完成报告\n")), 2)
        missing_newdata = "\n".join(
            line for line in report(("0x1000",) * 4).splitlines()
            if "`newData` 地址" not in line
        )
        issues = self.check(missing_newdata)
        self.assertEqual(len(issues), 1)
        self.assertIn("newData", issues[0].message)

    def test_truncated_row_does_not_hide_missing_columns(self):
        content = report(("0x1000",) * 4).replace(
            "| 当前 `data` 地址（图 C 为 `v.data`） | 0x1000 | 0x1000 | 0x1000 |",
            "| 当前 `data` 地址（图 C 为 `v.data`） | 0x1000 |",
        )
        self.assertEqual(len(self.check(content)), 2)


if __name__ == "__main__":
    unittest.main()
