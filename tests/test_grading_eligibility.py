from __future__ import annotations

import io
import unittest

from PIL import Image

from course_pr_reviewer.grading.eligibility import (
    ELIGIBLE,
    NOT_ELIGIBLE,
    UNKNOWN,
    FileFact,
    is_template_copy,
    judge_claim,
)

PREFIX = "2023010102刘西莹/Lab1"
ASSIGNMENT = {
    "required_files": ["Lab1.md", "imgs/shot.png"],
    "min_nonempty_lines": 3,
    "allow_extra_files": False,
}
TEMPLATE = "# Lab1\n\n## 1. 实验目的\n（填写）\n\n## 2. 实验步骤\n（粘贴截图）\n"


def png_bytes(*, blank: bool, size=(320, 240)) -> bytes:
    image = Image.new("RGB", size, (255, 255, 255))
    if not blank:
        pixels = image.load()
        for x in range(0, size[0], 7):
            for y in range(0, size[1], 5):
                pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def files(**extra):
    base = {
        "report": FileFact(f"{PREFIX}/Lab1.md", "added", "1" * 40),
        "image": FileFact(f"{PREFIX}/imgs/shot.png", "added", "2" * 40),
    }
    base.update(extra)
    return [base["report"], base["image"]]


def blobs(report: str | None = None, image: bytes | None = None):
    mapping = {
        "1" * 40: (report if report is not None else "第一行\n第二行\n第三行\n第四行\n").encode(),
        "2" * 40: image if image is not None else png_bytes(blank=False),
    }
    return lambda sha: mapping[sha]


class EligibilityTests(unittest.TestCase):
    def judge(self, *, file_list=None, loader=None, template=None, assignment=None,
              min_image_bytes=10240):
        return judge_claim(
            files=file_list if file_list is not None else files(),
            assignment=assignment or ASSIGNMENT,
            expected_prefix=PREFIX,
            template_texts=template,
            load_blob=loader if loader is not None else blobs(),
            min_image_bytes=min_image_bytes,
        )

    def test_substantive_submission_is_eligible(self):
        verdict = self.judge()
        self.assertEqual(verdict.status, ELIGIBLE)
        self.assertEqual(len(verdict.files), 2)
        self.assertTrue(all(item.get("blob_sha") for item in verdict.files))

    def test_empty_pull_request_is_not_eligible(self):
        verdict = judge_claim(
            files=[], assignment=ASSIGNMENT, expected_prefix=PREFIX, load_blob=blobs()
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("没有任何文件变更", " ".join(verdict.reasons))

    def test_missing_required_file_is_not_eligible(self):
        verdict = self.judge(file_list=[files()[0]])
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("缺少必交文件", " ".join(verdict.reasons))

    def test_out_of_scope_path_is_not_eligible(self):
        extra = FileFact("2023010103张三/Lab1/Lab1.md", "added", "3" * 40)
        verdict = self.judge(file_list=files() + [extra])
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("不在本次作业目录", " ".join(verdict.reasons))

    def test_removed_or_renamed_file_is_not_eligible(self):
        for status in ("removed", "renamed"):
            with self.subTest(status=status):
                verdict = self.judge(
                    file_list=[
                        FileFact(f"{PREFIX}/Lab1.md", status, "1" * 40),
                        FileFact(f"{PREFIX}/imgs/shot.png", "added", "2" * 40),
                    ]
                )
                self.assertEqual(verdict.status, NOT_ELIGIBLE)

    def test_empty_text_file_is_not_eligible(self):
        verdict = self.judge(loader=blobs(report="   \n\n"))
        self.assertEqual(verdict.status, NOT_ELIGIBLE)

    def test_short_report_is_not_eligible(self):
        verdict = self.judge(loader=blobs(report="只有一行\n"))
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("少于要求的 3 行", " ".join(verdict.reasons))

    def test_template_copy_is_not_eligible(self):
        verdict = self.judge(template={f"{PREFIX}/Lab1.md": TEMPLATE})
        # 报告内容与模板长度差距大，这里换成"只改姓名"的真实场景更准确。
        self.assertEqual(verdict.status, ELIGIBLE)
        copy = TEMPLATE.replace("（填写）", "刘西莹")
        verdict = self.judge(
            loader=blobs(report=copy), template={f"{PREFIX}/Lab1.md": TEMPLATE}
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("与教师模板基本相同", " ".join(verdict.reasons))

    def test_template_comparison_helper(self):
        self.assertTrue(is_template_copy(TEMPLATE, TEMPLATE))
        self.assertTrue(is_template_copy(TEMPLATE.replace("（填写）", "张三"), TEMPLATE))
        self.assertFalse(
            is_template_copy(TEMPLATE + "\n" + "真实实验记录\n" * 20, TEMPLATE)
        )

    def test_blank_image_is_not_eligible(self):
        verdict = self.judge(
            loader=blobs(image=png_bytes(blank=True)), min_image_bytes=1
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("单一颜色", " ".join(verdict.reasons))

    def test_tiny_image_is_not_eligible(self):
        verdict = self.judge(loader=blobs(image=png_bytes(blank=False, size=(16, 16))))
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("图片", " ".join(verdict.reasons))

    def test_small_but_non_blank_image_below_side_limit_is_rejected(self):
        # 尺寸小但文件足够大（噪声多），触发的是像素边长检查。
        image = Image.new("RGB", (40, 40))
        pixels = image.load()
        for x in range(40):
            for y in range(40):
                pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, (x * y) % 256)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        verdict = self.judge(loader=blobs(image=buffer.getvalue()), min_image_bytes=1)
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("小于", " ".join(verdict.reasons))

    def test_image_below_byte_threshold_is_rejected(self):
        verdict = self.judge(
            loader=blobs(image=png_bytes(blank=False, size=(16, 16)))
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("字节", " ".join(verdict.reasons))

    def test_corrupt_image_is_not_eligible(self):
        verdict = self.judge(
            loader=blobs(image=b"not an image at all" * 100), min_image_bytes=1
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("无法解析", " ".join(verdict.reasons))

    def test_unreadable_blob_is_unknown_not_eligible(self):
        def failing(sha):
            raise RuntimeError("boom")

        verdict = self.judge(loader=failing)
        self.assertEqual(verdict.status, UNKNOWN)
        self.assertIn("无法读取", " ".join(verdict.reasons))

    def test_missing_loader_is_unknown(self):
        verdict = judge_claim(
            files=files(), assignment=ASSIGNMENT, expected_prefix=PREFIX, load_blob=None
        )
        self.assertEqual(verdict.status, UNKNOWN)

    def test_non_substantive_mode_accepts_presence_only(self):
        verdict = judge_claim(
            files=files(),
            assignment=ASSIGNMENT,
            expected_prefix=PREFIX,
            load_blob=None,
            require_substantive=False,
        )
        self.assertEqual(verdict.status, ELIGIBLE)

    def test_one_of_alternatives_satisfy_requirement(self):
        assignment = {
            "required_files": [{"one_of": ["Lab1.md", "report.md"]}, "imgs/shot.png"],
            "min_nonempty_lines": 1,
        }
        verdict = judge_claim(
            files=[
                FileFact(f"{PREFIX}/report.md", "added", "1" * 40),
                FileFact(f"{PREFIX}/imgs/shot.png", "added", "2" * 40),
            ],
            assignment=assignment,
            expected_prefix=PREFIX,
            load_blob=blobs(),
        )
        self.assertEqual(verdict.status, ELIGIBLE)

    def test_deleted_empty_file_is_rejected_even_without_size(self):
        verdict = judge_claim(
            files=[
                FileFact(f"{PREFIX}/Lab1.md", "added", "1" * 40, size=0),
                FileFact(f"{PREFIX}/imgs/shot.png", "added", "2" * 40),
            ],
            assignment=ASSIGNMENT,
            expected_prefix=PREFIX,
            load_blob=blobs(),
        )
        self.assertEqual(verdict.status, NOT_ELIGIBLE)
        self.assertIn("空文件", " ".join(verdict.reasons))


if __name__ == "__main__":
    unittest.main()
