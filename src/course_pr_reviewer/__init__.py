"""Reusable course PR reviewer."""

from importlib.metadata import PackageNotFoundError, version

from .models import Decision, Issue, ReasonCode, ReviewResult

__all__ = ["Decision", "Issue", "ReasonCode", "ReviewResult"]

try:
    # 单一版本来源是 pyproject.toml，避免手改常量时漏改导致上报版本失真。
    __version__ = version("course-pr-reviewer")
except PackageNotFoundError:  # pragma: no cover - 仅在未安装的源码树中出现
    __version__ = "0+unknown"
