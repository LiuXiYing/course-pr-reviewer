from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from course_pr_reviewer.ai import GlmClient
from course_pr_reviewer.config import CourseConfiguration, load_course_config
from course_pr_reviewer.exceptions import InvalidStudentImage
from course_pr_reviewer.models import Decision, ReasonCode
from course_pr_reviewer.snapshot import ChangedFile, PullRequestSnapshot
from course_pr_reviewer.vision import (
    GlmVisionReviewer,
    OCRResult,
    PaddleOCREngine,
    prepare_image,
)

ROOT = Path(__file__).parents[1]
SHA = "a" * 40


def png_bytes(width=320, height=200):
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def vision_result(decision="PASS", confidence=0.96, issues=None):
    return {
        "decision": decision,
        "summary": "图片审核结果",
        "confidence": confidence,
        "issues": [] if issues is None else issues,
    }


class FakeTransport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(json.loads(body))
        return {
            "id": "vision-response",
            "choices": [
                {"message": {"content": json.dumps(self.result, ensure_ascii=False)}}
            ],
            "usage": {"total_tokens": 88},
        }


class FakeGitHub:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def blob_bytes(self, repository, sha, *, max_bytes):
        self.calls.append((repository, sha, max_bytes))
        return self.raw


class FakeOCR:
    def __init__(self, text="运行结果：42", confidence=0.97):
        self.result = OCRResult(text, confidence, 1, 0)
        self.calls = []

    def extract(self, image):
        self.calls.append(image.path)
        return self.result


class ImagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_course_config(ROOT / "examples/course-review.yml").vision

    def test_valid_image_is_reencoded_as_bounded_png(self):
        result = prepare_image("result.png", png_bytes(5000, 100), self.settings)
        self.assertLessEqual(max(result.width, result.height), 4096)
        self.assertTrue(result.data.startswith(b"\x89PNG"))

    def test_invalid_image_is_rejected(self):
        with self.assertRaises(InvalidStudentImage):
            prepare_image("result.png", b"not an image", self.settings)

    def test_tiny_image_is_rejected(self):
        with self.assertRaisesRegex(InvalidStudentImage, "32"):
            prepare_image("result.png", png_bytes(16, 16), self.settings)


class PaddleAdapterTests(unittest.TestCase):
    def test_result_lines_are_filtered_by_confidence(self):
        class Pipeline:
            def predict(self, path):
                return [
                    {
                        "rec_texts": ["有效文字", "低置信文字", "  "],
                        "rec_scores": [0.95, 0.2, 0.99],
                    }
                ]

        captured = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return Pipeline()

        course = load_course_config(ROOT / "examples/course-review.yml")
        engine = PaddleOCREngine(course.ocr, pipeline_factory=factory)
        prepared = prepare_image("result.png", png_bytes(), course.vision)
        result = engine.extract(prepared)
        self.assertEqual(result.text, "有效文字")
        self.assertEqual(result.accepted_lines, 1)
        self.assertEqual(result.discarded_lines, 2)
        self.assertEqual(captured["device"], "cpu")
        self.assertFalse(captured["use_doc_unwarping"])


class GlmVisionReviewerTests(unittest.TestCase):
    def setUp(self):
        loaded = load_course_config(ROOT / "examples/course-review.yml")
        data = copy.deepcopy(loaded.data)
        data["features"].update(ai_review=False, ocr_review=True, vision_review=True)
        self.course = CourseConfiguration(data)
        self.full_path = "2023010102刘西莹/Lab1/result.png"
        self.snapshot = PullRequestSnapshot(
            repository="teacher/course",
            number=1,
            title="[2023010102刘西莹]Lab1作业提交",
            author_login="example-user",
            captured_head_sha=SHA,
            current_head_sha=SHA,
            event_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            files=(ChangedFile(self.full_path, "added", blob_sha=SHA),),
        )

    def reviewer(self, result, *, ocr=None):
        transport = FakeTransport(result)
        client = GlmClient("test-key", transport=transport, sleeper=lambda _: None)
        github = FakeGitHub(png_bytes())
        reviewer = GlmVisionReviewer(
            client, github, ocr_engine=ocr if ocr is not None else FakeOCR()
        )
        return reviewer, transport, github

    def test_pass_sends_sanitized_image_and_relative_path(self):
        reviewer, transport, github = self.reviewer(vision_result())
        outcome = reviewer.review(
            self.course, "Lab1", self.snapshot, "2023010102刘西莹/Lab1"
        )
        self.assertEqual(outcome.decision, Decision.PASS)
        self.assertEqual(outcome.metadata["image_count"], 1)
        request = transport.calls[0]
        self.assertEqual(request["model"], "glm-4.6v-flash")
        self.assertNotIn("response_format", request)
        user_content = request["messages"][1]["content"]
        payload = json.loads(user_content[0]["text"].split("\n", 1)[1])
        self.assertEqual(payload["images"][0]["file"], "result.png")
        self.assertNotIn("2023010102", json.dumps(user_content, ensure_ascii=False))
        decoded = base64.b64decode(user_content[2]["image_url"]["url"])
        self.assertTrue(decoded.startswith(b"\x89PNG"))
        self.assertEqual(github.calls[0][1], SHA)

    def test_high_confidence_visual_failure_is_returned(self):
        issue = {
            "category": "VISUAL_VIOLATION",
            "message": "截图中没有要求的运行结果",
            "file": "result.png",
            "evidence": "截图只显示空白窗口",
            "rule": "检查截图中的运行结果",
        }
        reviewer, _, _ = self.reviewer(vision_result("FAIL", issues=[issue]))
        outcome = reviewer.review(
            self.course, "Lab1", self.snapshot, "2023010102刘西莹/Lab1"
        )
        self.assertEqual(outcome.decision, Decision.FAIL)
        self.assertEqual(outcome.issues[0].code, ReasonCode.VISION_REJECTED)

    def test_ocr_failure_requires_exact_ocr_evidence(self):
        issue = {
            "category": "OCR_TEXT_VIOLATION",
            "message": "运行结果不符合要求",
            "file": "result.png",
            "evidence": "不存在的文字",
            "rule": "检查运行输出",
        }
        reviewer, _, _ = self.reviewer(vision_result("FAIL", issues=[issue]))
        outcome = reviewer.review(
            self.course, "Lab1", self.snapshot, "2023010102刘西莹/Lab1"
        )
        self.assertEqual(outcome.decision, Decision.MANUAL_REVIEW)
        self.assertEqual(outcome.issues[0].code, ReasonCode.VISION_UNCERTAIN)

    def test_low_fail_confidence_is_manual(self):
        issue = {
            "category": "VISUAL_VIOLATION",
            "message": "无法确认结果",
            "file": "result.png",
            "evidence": "窗口内容模糊",
            "rule": "检查运行输出",
        }
        reviewer, _, _ = self.reviewer(
            vision_result("FAIL", confidence=0.86, issues=[issue])
        )
        outcome = reviewer.review(
            self.course, "Lab1", self.snapshot, "2023010102刘西莹/Lab1"
        )
        self.assertEqual(outcome.decision, Decision.MANUAL_REVIEW)

    def test_configured_image_pattern_without_match_is_manual(self):
        snapshot = copy.copy(self.snapshot)
        object.__setattr__(
            snapshot,
            "files",
            (ChangedFile("2023010102刘西莹/Lab1/Lab1.md", "added"),),
        )
        reviewer, transport, _ = self.reviewer(vision_result())
        outcome = reviewer.review(
            self.course, "Lab1", snapshot, "2023010102刘西莹/Lab1"
        )
        self.assertEqual(outcome.decision, Decision.MANUAL_REVIEW)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
