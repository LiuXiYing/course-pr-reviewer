"""Email notifications for review results that require human intervention."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from .exceptions import ReviewSystemError
from .models import Decision

MailTransport = Callable[[str, int, str, str, EmailMessage], None]


def notification_required(result: dict[str, Any]) -> bool:
    return result.get("decision") in {
        Decision.MANUAL_REVIEW.value,
        Decision.ERROR.value,
    }


def _smtp_ssl_transport(
    host: str,
    port: int,
    username: str,
    password: str,
    message: EmailMessage,
) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


def _email_address(value: str, field: str) -> str:
    address = value.strip()
    if (
        not address
        or any(character in address for character in ("\r", "\n", " "))
        or address.count("@") != 1
        or not all(address.split("@", 1))
    ):
        raise ReviewSystemError(f"{field} 不是有效的邮件地址")
    return address


@dataclass(frozen=True)
class TeacherEmailNotifier:
    recipient: str
    username: str
    password: str
    host: str = "smtp.gmail.com"
    port: int = 465
    transport: MailTransport = _smtp_ssl_transport

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "recipient", _email_address(self.recipient, "任课教师邮箱")
        )
        object.__setattr__(self, "username", _email_address(self.username, "发件邮箱"))
        if not self.password:
            raise ReviewSystemError("缺少 SMTP 密码")
        if not self.host.strip() or any(
            character in self.host for character in ("\r", "\n", " ")
        ):
            raise ReviewSystemError("SMTP 主机名无效")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ReviewSystemError("SMTP 端口无效")

    def send(
        self,
        *,
        course_name: str,
        result: dict[str, Any],
        repository: str,
        run_url: str,
    ) -> None:
        metadata = result.get("metadata", {})
        number = metadata.get("pr_number")
        pr_url = (
            f"https://github.com/{repository}/pull/{number}"
            if isinstance(number, int) and not isinstance(number, bool) and number > 0
            else f"https://github.com/{repository}/pulls"
        )
        subject_number = f" #{number}" if isinstance(number, int) else ""
        message = EmailMessage()
        message["From"] = self.username
        message["To"] = self.recipient
        message["Subject"] = f"[{course_name}] PR{subject_number} 需要人工处理"

        issue_lines = []
        for item in result.get("issues", [])[:30]:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", "UNKNOWN"))[:100]
            detail = str(item.get("message", ""))[:1000].replace("\r", " ").replace("\n", " ")
            file = item.get("file")
            location = f" ({str(file)[:500]})" if file else ""
            issue_lines.append(f"- {code}{location}: {detail}")
        issues = "\n".join(issue_lines) if issue_lines else "- 未提供具体问题"
        action_line = run_url if run_url else "未提供"
        message.set_content(
            "课程 PR 自动审核需要任课教师人工处理。\n\n"
            f"课程：{course_name}\n"
            f"审核结果：{result.get('decision', 'ERROR')}\n"
            f"摘要：{str(result.get('summary', ''))[:2000]}\n"
            f"PR：{pr_url}\n"
            f"Actions：{action_line}\n\n"
            f"具体问题：\n{issues}\n"
        )
        try:
            self.transport(
                self.host,
                self.port,
                self.username,
                self.password,
                message,
            )
        except (OSError, smtplib.SMTPException) as exc:
            raise ReviewSystemError(
                f"邮件通知任课教师失败：{type(exc).__name__}"
            ) from exc
