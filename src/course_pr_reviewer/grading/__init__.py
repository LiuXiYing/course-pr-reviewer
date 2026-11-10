"""Grading policy, trusted eligibility and the ``grades`` branch store.

The grading layer is deliberately separate from content review: content rules
decide ``review_decision``, this package decides ``grade_decision``. It must stay
free of AI calls and must never execute student code.
"""

from __future__ import annotations

from .policy import (
    DEFAULT_BANDS,
    GradeDecision,
    compute_grade,
    default_grading_settings,
)

__all__ = [
    "DEFAULT_BANDS",
    "GradeDecision",
    "compute_grade",
    "default_grading_settings",
]
