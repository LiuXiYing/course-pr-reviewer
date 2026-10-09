from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from course_pr_reviewer.notifications import TeacherEmailNotifier, notification_required


class NotificationTests(unittest.TestCase):
    def test_no_result_requires_email(self):
        for decision in ("PASS", "FAIL", "MANUAL_REVIEW", "ERROR", "UNKNOWN", None):
            with self.subTest(decision=decision):
                self.assertFalse(notification_required({"decision": decision}))
        self.assertFalse(notification_required({}))

    @patch("smtplib.SMTP_SSL")
    def test_legacy_notifier_never_contacts_smtp_or_custom_transport(self, smtp):
        transport = Mock()
        for custom_transport in (None, transport):
            notifier = TeacherEmailNotifier(
                recipient="teacher@example.com", username="sender@example.com",
                password="secret", transport=custom_transport,
            )
            for decision in ("PASS", "FAIL", "MANUAL_REVIEW", "ERROR"):
                notifier.send(
                    course_name="课程", result={"decision": decision},
                    repository="teacher/course", run_url="",
                )
        smtp.assert_not_called()
        transport.assert_not_called()

    def test_disabled_notifier_does_not_require_valid_credentials(self):
        notifier = TeacherEmailNotifier(recipient="", username="", password="", host="", port=0)
        notifier.send(course_name="课程", result={}, repository="teacher/course", run_url="")


if __name__ == "__main__":
    unittest.main()
