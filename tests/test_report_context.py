from __future__ import annotations

import unittest

from course_pr_reviewer.report_context import report_segments


class ReportContextTests(unittest.TestCase):
    def test_example_ip_and_actual_answer_have_distinct_sources(self):
        template = (
            "# Lab2\n把示例替换成自己的信息：\n"
            "```bash\nssh student@192.168.80.128\n```\n"
            "> 记录：Ubuntu IP 为 ______。\n> 记录：用户名为 ______。\n"
        )
        content = template.replace("IP 为 ______", "IP 为 192.168.225.128")
        content = content.replace("用户名为 ______", "用户名为 ___user123___")
        segments = report_segments(template, content)
        self.assertEqual("".join(part["content"] for part in segments), content)
        examples = "".join(p["content"] for p in segments if p["kind"] == "template")
        answers = "".join(p["content"] for p in segments if p["kind"] == "submission")
        self.assertIn("student@192.168.80.128", examples)
        self.assertNotIn("192.168.80.128", answers)
        self.assertIn("192.168.225.128", answers)
        self.assertIn("___user123___", answers)

    def test_unfilled_required_fields_remain_visible(self):
        content = "# Lab2\n> 记录：Ubuntu IP 为 ______。\n"
        segments = report_segments(content, content)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["kind"], "template")
        self.assertEqual(segments[0]["content"], content)

    def test_insertions_deletions_and_line_endings_preserve_exact_submission(self):
        template = "# Lab2\n示例\n> 记录：______\n选读\n"
        content = "# Lab2\r\n> 记录：192.168.1.2\r\n新增说明"
        segments = report_segments(template, content)
        self.assertEqual("".join(part["content"] for part in segments), content)
        self.assertEqual(segments[0]["start_line"], 1)
        self.assertEqual(segments[-1]["end_line"], 3)


if __name__ == "__main__":
    unittest.main()
