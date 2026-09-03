"""Safe comparison helpers for student-supplied filenames."""

from __future__ import annotations

from collections.abc import Collection

_FILENAME_HYPHEN_TRANSLATION = str.maketrans(
    {
        "\N{HYPHEN}": "-",
        "\N{NON-BREAKING HYPHEN}": "-",
        "\N{FULLWIDTH HYPHEN-MINUS}": "-",
    }
)


def canonical_filename(value: str) -> str:
    """Normalize visually equivalent hyphens only for filename comparison."""
    return value.translate(_FILENAME_HYPHEN_TRANSLATION)


def resolve_filename(value: str, candidates: Collection[str]) -> str | None:
    """Resolve an exact or uniquely hyphen-equivalent filename."""
    if value in candidates:
        return value
    canonical = canonical_filename(value)
    matches = [item for item in candidates if canonical_filename(item) == canonical]
    return matches[0] if len(matches) == 1 else None
