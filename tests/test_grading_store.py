from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from course_pr_reviewer.exceptions import ContentConflict, GradeStoreError
from course_pr_reviewer.grading.store import (
    DryRunStore,
    GitHubContentsStore,
    LocalDirStore,
)


class LocalStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="grades-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = LocalDirStore(self.root)

    def test_init_creates_metadata_and_readme(self):
        metadata = self.store.ensure_branch(
            course_id="test-course", course_name="测试课程", timezone="Asia/Shanghai"
        )
        self.assertEqual(metadata["course_id"], "test-course")
        self.assertTrue((self.root / "metadata.json").is_file())
        self.assertIn("成绩记录分支", (self.root / "README.md").read_text())

    def test_init_is_idempotent(self):
        first = self.store.ensure_branch(
            course_id="test-course", course_name="测试课程", timezone="Asia/Shanghai"
        )
        second = self.store.ensure_branch(
            course_id="test-course", course_name="测试课程", timezone="Asia/Shanghai"
        )
        self.assertEqual(first["created_at"], second["created_at"])

    def test_write_and_read_json(self):
        self.store.write_json("receipts/Lab1/2023010102.json", {"a": 1}, "msg")
        self.assertEqual(
            self.store.read_json("receipts/Lab1/2023010102.json"), {"a": 1}
        )
        self.assertIsNone(self.store.read_json("receipts/Lab1/missing.json"))

    def test_path_traversal_is_rejected(self):
        for path in ("../escape.json", "/etc/passwd", "a/../../b.json", "a\\b.json"):
            with self.subTest(path=path), self.assertRaises(GradeStoreError):
                self.store.write_text(path, "{}", "msg")

    def test_list_paths(self):
        self.store.write_json("receipts/Lab1/2023010102.json", {}, "m")
        self.store.write_json("receipts/Lab1/2023010103.json", {}, "m")
        self.assertEqual(
            self.store.list_paths("receipts/Lab1"),
            ["receipts/Lab1/2023010102.json", "receipts/Lab1/2023010103.json"],
        )
        self.assertEqual(self.store.list_paths("receipts/Lab9"), [])

    def test_invalid_json_is_reported(self):
        path = self.root / "receipts/Lab1/bad.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(GradeStoreError):
            self.store.read_json("receipts/Lab1/bad.json")

    def test_dry_run_store_records_without_writing(self):
        dry = DryRunStore(self.store)
        dry.write_json("receipts/Lab1/2023010102.json", {"a": 1}, "msg")
        self.assertFalse((self.root / "receipts/Lab1/2023010102.json").exists())
        self.assertEqual(dry.writes, [("receipts/Lab1/2023010102.json", "msg")])


class FakeContentsClient:
    """Contents API stand-in: keeps file text and honours optimistic SHA."""

    PREFIX = "/repos/teacher/course/contents/"

    def __init__(self):
        self.files: dict[str, str] = {}
        self.conflicts = 0
        self.put_calls = 0
        self.created_refs: list[str] = []
        self.branch_exists = False

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _key(self, path: str) -> str:
        rest = path.removeprefix(self.PREFIX)
        return urllib.parse.unquote(rest.split("?")[0])

    def get_json(self, path, allow_missing=False):
        if "/branches/" in path:
            return {"name": "grades"} if self.branch_exists else None
        key = self._key(path)
        if key in self.files:
            text = self.files[key]
            return {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "sha": self._sha(text),
                "size": len(text.encode("utf-8")),
            }
        prefix = key.rstrip("/") + "/"
        listing = [
            {"path": name, "type": "file"}
            for name in sorted(self.files)
            if name.startswith(prefix) and "/" not in name[len(prefix):]
        ]
        if listing:
            return listing
        if allow_missing:
            return None
        raise AssertionError(f"unexpected 404 for {path}")

    def put_json(self, path, body):
        self.put_calls += 1
        if self.conflicts > 0:
            self.conflicts -= 1
            raise ContentConflict("HTTP 409 conflict")
        key = self._key(path)
        existing = self.files.get(key)
        if body.get("sha") and existing is not None and body["sha"] != self._sha(existing):
            raise ContentConflict("HTTP 409 stale sha")
        self.files[key] = base64.b64decode(body["content"]).decode("utf-8")
        return {"content": {"path": key}}

    def post_json(self, path, body):
        if path.endswith("/git/blobs"):
            return {"sha": "b" * 40}
        if path.endswith("/git/trees"):
            return {"sha": "c" * 40}
        if path.endswith("/git/commits"):
            return {"sha": "d" * 40}
        if path.endswith("/git/refs"):
            self.created_refs.append(body["ref"])
            self.branch_exists = True
            return {"ref": body["ref"]}
        raise AssertionError(path)

    def request_json(self, method, path, *, body=None, expected=(200,), allow_missing=False):
        if method == "POST" and path.endswith("/git/refs"):
            return self.post_json(path, body)
        raise AssertionError(f"{method} {path}")


class GitHubStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeContentsClient()
        self.store = GitHubContentsStore(
            self.client, "teacher/course", branch="grades", sleep=lambda _: None
        )

    def test_write_then_read(self):
        self.store.write_json("receipts/Lab1/2023010102.json", {"x": 2}, "msg")
        self.assertEqual(
            self.store.read_json("receipts/Lab1/2023010102.json"), {"x": 2}
        )
        self.assertIn("receipts/Lab1/2023010102.json", self.client.files)

    def test_conflict_is_retried(self):
        self.client.conflicts = 2
        self.store.write_text("receipts/Lab1/a.json", "{}", "msg")
        self.assertEqual(self.client.put_calls, 3)

    def test_persistent_conflict_raises(self):
        self.client.conflicts = 99
        with self.assertRaises(GradeStoreError) as caught:
            self.store.write_text("receipts/Lab1/a.json", "{}", "msg")
        self.assertIn("冲突", str(caught.exception))

    def test_missing_file_reads_as_none(self):
        self.assertIsNone(self.store.read_text("receipts/Lab1/none.json"))

    def test_list_paths_returns_children(self):
        self.store.write_json("receipts/Lab1/a.json", {}, "m")
        self.store.write_json("receipts/Lab1/b.json", {}, "m")
        self.assertEqual(
            self.store.list_paths("receipts/Lab1"),
            ["receipts/Lab1/a.json", "receipts/Lab1/b.json"],
        )

    def test_init_creates_orphan_branch(self):
        metadata = self.store.ensure_branch(
            course_id="test-course", course_name="测试课程", timezone="Asia/Shanghai"
        )
        self.assertEqual(metadata["course_id"], "test-course")
        self.assertEqual(self.client.created_refs, ["refs/heads/grades"])

    def test_init_is_a_noop_when_branch_exists(self):
        self.client.branch_exists = True
        metadata = self.store.ensure_branch(
            course_id="test-course", course_name="测试课程", timezone="Asia/Shanghai"
        )
        self.assertEqual(metadata["course_id"], "test-course")
        self.assertEqual(self.client.created_refs, [])

    def test_json_round_trip_is_stable(self):
        payload = {"b": [1, 2], "a": "中文"}
        self.store.write_json("receipts/Lab1/a.json", payload, "m")
        raw = self.client.files["receipts/Lab1/a.json"]
        self.assertEqual(json.loads(raw), payload)
        self.assertTrue(raw.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
