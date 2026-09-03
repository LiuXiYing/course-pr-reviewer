from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from course_pr_reviewer.exceptions import ContentLimitExceeded, ReviewSystemError
from course_pr_reviewer.snapshot import GitHubClient, load_snapshot, snapshot_from_dict


def valid_snapshot():
    return {
        "repository": "teacher/course",
        "number": 1,
        "title": "[2023010102刘西莹]Lab1作业提交",
        "author_login": "example-user",
        "captured_head_sha": "a" * 40,
        "current_head_sha": "a" * 40,
        "event_at": "2026-09-01T12:00:00+08:00",
        "files": [{"filename": "2023010102刘西莹/Lab1/Lab1.md", "status": "added"}],
    }


class SnapshotTests(unittest.TestCase):
    def test_snapshot_json_loads_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(
                json.dumps(valid_snapshot(), ensure_ascii=False), encoding="utf-8"
            )
            snapshot = load_snapshot(directory)
        self.assertEqual(snapshot.number, 1)
        self.assertEqual(snapshot.event_at.utcoffset(), dt.timedelta(hours=8))

    def test_unsafe_changed_path_is_rejected(self):
        data = valid_snapshot()
        data["files"][0]["filename"] = "../secret"
        with self.assertRaisesRegex(ReviewSystemError, "不安全"):
            snapshot_from_dict(data)

    def test_invalid_sha_is_rejected(self):
        data = valid_snapshot()
        data["current_head_sha"] = "main"
        with self.assertRaisesRegex(ReviewSystemError, "SHA"):
            snapshot_from_dict(data)

    def test_github_blob_is_decoded_as_utf8_text(self):
        client = GitHubClient("token")
        raw = "作业内容\n".encode()
        client.get_bytes = lambda *args, **kwargs: raw
        self.assertEqual(
            client.text_blob("teacher/course", "a" * 40, max_bytes=1000),
            "作业内容\n",
        )

    def test_github_blob_bytes_returns_exact_binary(self):
        client = GitHubClient("token")
        raw = b"\x89PNG\r\n\x1a\n"
        calls = []

        def get_bytes(path, *, max_bytes, accept):
            calls.append((path, max_bytes, accept))
            return raw

        client.get_bytes = get_bytes
        self.assertEqual(
            client.blob_bytes("teacher/course", "a" * 40, max_bytes=1000), raw
        )
        self.assertEqual(
            calls,
            [
                (
                    "/repos/teacher/course/git/blobs/" + "a" * 40,
                    1000,
                    "application/vnd.github.raw+json",
                )
            ],
        )

    @patch("course_pr_reviewer.snapshot.urllib.request.urlopen")
    def test_raw_response_larger_than_limit_is_rejected(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b"x" * 1001

        client = GitHubClient("token")
        with self.assertRaisesRegex(ContentLimitExceeded, "1000"):
            client.get_bytes("/blob", max_bytes=1000)

        response.read.assert_called_once_with(1001)

    def test_binary_blob_is_skipped(self):
        client = GitHubClient("token")
        raw = b"image\0data"
        client.get_bytes = lambda *args, **kwargs: raw
        self.assertIsNone(client.text_blob("teacher/course", "a" * 40, max_bytes=1000))

    @patch("course_pr_reviewer.snapshot.GitHubClient.changed_files")
    @patch("course_pr_reviewer.snapshot.GitHubClient.pull_request")
    def test_event_metadata_is_refreshed_from_github(self, pull_request, changed_files):
        pull_request.return_value = {
            "title": "[2023010102刘西莹]Lab1作业提交",
            "user": {"login": "example-user"},
            "head": {"sha": "b" * 40},
        }
        changed_files.return_value = [
            {"filename": "2023010102刘西莹/Lab1/Lab1.md", "status": "added"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            event = {
                "repository": "teacher/course",
                "number": 7,
                "title": "[2023010102刘西莹]Lab1作业提交",
                "head_sha": "a" * 40,
                "event_at": "2026-09-01T12:00:00+08:00",
            }
            (Path(directory) / "event.json").write_text(
                json.dumps(event), encoding="utf-8"
            )
            snapshot = load_snapshot(
                directory, github_token="token", repository="teacher/course"
            )
        self.assertEqual(snapshot.captured_head_sha, "a" * 40)
        self.assertEqual(snapshot.current_head_sha, "b" * 40)
        pull_request.assert_called_once_with("teacher/course", 7)

    @patch("course_pr_reviewer.snapshot.GitHubClient.changed_files", return_value=[])
    @patch("course_pr_reviewer.snapshot.GitHubClient.pull_request")
    def test_title_change_after_collection_is_rejected(
        self, pull_request, changed_files
    ):
        pull_request.return_value = {
            "title": "新标题",
            "user": {"login": "example-user"},
            "head": {"sha": "a" * 40},
        }
        with tempfile.TemporaryDirectory() as directory:
            event = {
                "repository": "teacher/course",
                "number": 7,
                "title": "旧标题",
                "head_sha": "a" * 40,
                "event_at": "2026-09-01T12:00:00+08:00",
            }
            (Path(directory) / "event.json").write_text(
                json.dumps(event), encoding="utf-8"
            )
            with self.assertRaisesRegex(ReviewSystemError, "标题"):
                load_snapshot(
                    directory, github_token="token", repository="teacher/course"
                )


if __name__ == "__main__":
    unittest.main()
