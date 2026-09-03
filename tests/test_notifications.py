from __future__ import annotations

import smtplib
import unittest

from course_pr_reviewer.exceptions import ReviewSystemError
from course_pr_reviewer.notifications import (
    TeacherEmailNotifier,
    notification_required,
)


def result(decision="MANUAL_REVIEW"):
    return {
        "decision": decision,
        "summary": "无法自动确认",
        "issues": [{"code": "AI_UNCERTAIN", "message": "两个模型无法达成一致"}],
        "metadata": {"pr_number": 7},
    }


class NotificationTests(unittest.TestCase):
    def test_only_manual_or_error_requires_email(self):
        self.assertTrue(notification_required(result("MANUAL_REVIEW")))
        self.assertTrue(notification_required(result("ERROR")))
        self.assertFalse(notification_required(result("PASS")))
        self.assertFalse(notification_required(result("FAIL")))

    def test_notifier_sends_plain_text_summary(self):
        calls = []

        def transport(host, port, username, password, message):
            calls.append((host, port, username, password, message))

        notifier = TeacherEmailNotifier(
            recipient="teacher@example.com",
            username="sender@example.com",
            password="secret",
            transport=transport,
        )
        notifier.send(
            course_name="日志收集与分析",
            result=result(),
            repository="teacher/course",
            run_url="https://github.com/teacher/course/actions/runs/1",
        )

        self.assertEqual(len(calls), 1)
        message = calls[0][-1]
        self.assertIn("PR #7 需要人工处理", message["Subject"])
        self.assertIn("https://github.com/teacher/course/pull/7", message.get_content())
        self.assertIn("两个模型无法达成一致", message.get_content())

    def test_smtp_error_is_safe_and_does_not_expose_password(self):
        def transport(*args):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

        notifier = TeacherEmailNotifier(
            recipient="teacher@example.com",
            username="sender@example.com",
            password="top-secret",
            transport=transport,
        )
        with self.assertRaisesRegex(ReviewSystemError, "SMTPAuthenticationError") as raised:
            notifier.send(
                course_name="课程",
                result=result("ERROR"),
                repository="teacher/course",
                run_url="",
            )
        self.assertNotIn("top-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
