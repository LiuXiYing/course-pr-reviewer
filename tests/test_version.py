from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

import course_pr_reviewer

ROOT = Path(__file__).parents[1]


class VersionTests(unittest.TestCase):
    def test_reported_version_matches_packaging_metadata(self):
        declared = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        self.assertEqual(course_pr_reviewer.__version__, declared)


if __name__ == "__main__":
    unittest.main()
