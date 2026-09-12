from __future__ import annotations

import unittest

from course_pr_reviewer.report_context import report_segments
from course_pr_reviewer.exceptions import ContentLimitExceeded


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

    def test_fenced_examples_are_marked_but_required_blank_answers_are_not(self):
        template = (
            "把下面的 student 和 IP 换成自己的信息：\n\n"
            "```bash\nssh student@192.168.80.128\n```\n"
            "> 记录：IP 为 ______。\n"
        )
        parts = report_segments(template, template)
        example = next(p for p in parts if "ssh student@" in p["content"])
        unanswered = next(p for p in parts if "> 记录" in p["content"])
        self.assertTrue(example["is_example"])
        self.assertFalse(unanswered["is_example"])

    def test_repeated_template_lines_are_not_mislabeled_as_student_changes(self):
        template = "# Lab2\n" + "示例：ssh student@192.168.80.128\n" * 250
        content = "# 已填写 Lab2\n" + template.split("\n", 1)[1]
        parts = report_segments(template, content)
        changed = "".join(p["content"] for p in parts if p["kind"] == "submission")
        self.assertEqual(changed, "# 已填写 Lab2\n")
        self.assertTrue(all(p["is_example"] for p in parts[1:]))

    def test_excessive_comparison_work_is_bounded(self):
        template = "重复行\n" * 2001
        with self.assertRaises(ContentLimitExceeded):
            report_segments(template, template)


if __name__ == "__main__":
    unittest.main()
