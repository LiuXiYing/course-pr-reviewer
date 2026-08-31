from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from course_pr_reviewer.config import CourseConfiguration, load_course_config
from course_pr_reviewer.exceptions import ReviewSystemError
from course_pr_reviewer.models import Decision, Issue, ReasonCode, ReviewResult
from course_pr_reviewer.publisher import (
    COMMENT_MARKER,
    GitHubResultPublisher,
    load_result,
    render_comment,
)

ROOT = Path(__file__).parents[1]
SHA = "a" * 40


def result_dict(
    decision=Decision.PASS,
    *,
    issues=(),
    metadata=None,
):
    return ReviewResult(
        decision=decision,
        summary="审核结果摘要",
        issues=issues,
        metadata={
            "repository": "teacher/course",
            "pr_number": 7,
            "head_sha": SHA,
            **({} if metadata is None else metadata),
        },
    ).to_dict()


class FakeGitHub:
    def __init__(self, *, head_sha=SHA, state="open", merged=False, comments=None):
        self.head_sha = head_sha
        self.state = state
        self.merged = merged
        self.comments = [] if comments is None else comments
        self.get_calls = []
        self.post_calls = []
        self.patch_calls = []
        self.put_calls = []

    def pull_request(self, repository, number):
        return {
            "state": self.state,
            "merged": self.merged,
            "head": {"sha": self.head_sha},
        }

    def get_json(self, path):
        self.get_calls.append(path)
        return self.comments

    def post_json(self, path, body):
        self.post_calls.append((path, body))
        return {"id": 10}

    def patch_json(self, path, body):
        self.patch_calls.append((path, body))
        if path.endswith("/pulls/7"):
            self.state = "closed"
        return {"state": self.state}

    def put_json(self, path, body):
        self.put_calls.append((path, body))
        self.merged = True
        self.state = "closed"
        return {"merged": True}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.course = load_course_config(ROOT / "examples/course-review.yml")

    def test_pass_comments_then_merges_exact_head_sha(self):
        github = FakeGitHub()
        outcome = GitHubResultPublisher(github).publish(
            self.course,
            result_dict(),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.commented)
        self.assertTrue(outcome.merged)
        self.assertEqual(github.put_calls[0][1]["sha"], SHA)
        self.assertEqual(github.put_calls[0][1]["merge_method"], "merge")
        self.assertIn(COMMENT_MARKER, github.post_calls[0][1]["body"])

    def test_merge_uses_dedicated_client_when_provided(self):
        github = FakeGitHub()
        merger = FakeGitHub()
        outcome = GitHubResultPublisher(github, merge_github=merger).publish(
            self.course,
            result_dict(),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.merged)
        self.assertEqual(github.put_calls, [])
        self.assertEqual(merger.put_calls[0][1]["sha"], SHA)
        self.assertIn(COMMENT_MARKER, github.post_calls[0][1]["body"])
        self.assertEqual(merger.post_calls, [])

    def test_fail_updates_existing_bot_comment_without_merging(self):
        issue = Issue(code=ReasonCode.TITLE_MISMATCH, message="标题错误")
        comments = [
            {
                "id": 99,
                "body": COMMENT_MARKER + "\n旧结果",
                "user": {"login": "github-actions[bot]"},
            }
        ]
        github = FakeGitHub(comments=comments)
        outcome = GitHubResultPublisher(github).publish(
            self.course,
            result_dict(Decision.FAIL, issues=(issue,)),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.commented)
        self.assertFalse(outcome.merged)
        self.assertEqual(
            github.patch_calls[0][0], "/repos/teacher/course/issues/comments/99"
        )
        self.assertEqual(github.put_calls, [])

    def test_late_result_comments_then_closes(self):
        issue = Issue(
            code=ReasonCode.LATE_PR_CLOSE_REQUIRED,
            message="超过关闭阈值",
        )
        github = FakeGitHub()
        outcome = GitHubResultPublisher(github).publish(
            self.course,
            result_dict(
                Decision.FAIL,
                issues=(issue,),
                metadata={"close_pr": True},
            ),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.commented)
        self.assertTrue(outcome.closed)
        self.assertEqual(
            github.patch_calls[-1],
            ("/repos/teacher/course/pulls/7", {"state": "closed"}),
        )
        self.assertEqual(github.put_calls, [])

    def test_changed_head_skips_all_mutations(self):
        github = FakeGitHub(head_sha="b" * 40)
        outcome = GitHubResultPublisher(github).publish(
            self.course,
            result_dict(),
            expected_repository="teacher/course",
        )
        self.assertEqual(outcome.skipped, "head_changed")
        self.assertEqual(github.post_calls, [])
        self.assertEqual(github.patch_calls, [])
        self.assertEqual(github.put_calls, [])

    def test_head_change_after_comment_prevents_merge(self):
        class ChangingGitHub(FakeGitHub):
            def __init__(self):
                super().__init__()
                self.pr_reads = 0

            def pull_request(self, repository, number):
                self.pr_reads += 1
                if self.pr_reads == 2:
                    self.head_sha = "b" * 40
                return super().pull_request(repository, number)

        github = ChangingGitHub()
        outcome = GitHubResultPublisher(github).publish(
            self.course,
            result_dict(),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.commented)
        self.assertEqual(outcome.skipped, "head_changed")
        self.assertEqual(github.put_calls, [])

    def test_close_flag_without_late_reason_is_rejected(self):
        issue = Issue(code=ReasonCode.DEADLINE_EXCEEDED, message="仅轻微超期")
        with self.assertRaisesRegex(ReviewSystemError, "超期关闭原因"):
            GitHubResultPublisher(FakeGitHub()).publish(
                self.course,
                result_dict(
                    Decision.FAIL,
                    issues=(issue,),
                    metadata={"close_pr": True},
                ),
                expected_repository="teacher/course",
            )

    def test_stale_review_skips_without_loading_pr(self):
        issue = Issue(code=ReasonCode.STALE_HEAD_SHA, message="head 已变化")

        class NoCalls(FakeGitHub):
            def pull_request(self, repository, number):
                raise AssertionError("stale result must not load PR")

        outcome = GitHubResultPublisher(NoCalls()).publish(
            self.course,
            result_dict(Decision.ERROR, issues=(issue,)),
            expected_repository="teacher/course",
        )
        self.assertEqual(outcome.skipped, "stale_review")

    def test_repository_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ReviewSystemError, "仓库"):
            GitHubResultPublisher(FakeGitHub()).publish(
                self.course,
                result_dict(),
                expected_repository="attacker/course",
            )

    def test_disabled_auto_merge_only_comments(self):
        data = copy.deepcopy(self.course.data)
        data["features"]["auto_merge"] = False
        github = FakeGitHub()
        outcome = GitHubResultPublisher(github).publish(
            CourseConfiguration(data),
            result_dict(),
            expected_repository="teacher/course",
        )
        self.assertTrue(outcome.commented)
        self.assertFalse(outcome.merged)
        self.assertEqual(github.put_calls, [])

    def test_comment_neutralizes_mentions_and_markdown(self):
        issue = Issue(
            code=ReasonCode.AI_REJECTED,
            message="@everyone **不要通过**",
        )
        body = render_comment(result_dict(Decision.FAIL, issues=(issue,)))
        self.assertNotIn("@everyone", body)
        self.assertIn("@\u200beveryone", body)
        self.assertIn("\\*\\*不要通过\\*\\*", body)

    def test_result_file_is_schema_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps({"decision": "PASS"}), encoding="utf-8")
            with self.assertRaisesRegex(ReviewSystemError, "Schema"):
                load_result(path)


if __name__ == "__main__":
    unittest.main()
