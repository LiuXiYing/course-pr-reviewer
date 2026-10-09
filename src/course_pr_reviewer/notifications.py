"""Compatibility interfaces for permanently disabled email notifications."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

MailTransport = Callable[[str, int, str, str, EmailMessage], None]


def notification_required(result: dict[str, Any]) -> bool:
    """No review decision requires email, including errors and manual reviews."""
    return False


@dataclass(frozen=True)
class TeacherEmailNotifier:
    """Legacy constructor retained for callers; credentials are never used."""

    recipient: str
    username: str
    password: str
    host: str = "smtp.gmail.com"
    port: int = 465
    transport: MailTransport | None = None

    def send(
        self,
        *,
        course_name: str,
        result: dict[str, Any],
        repository: str,
        run_url: str,
    ) -> None:
        """Email is disabled unconditionally, including custom transports."""
        return None
