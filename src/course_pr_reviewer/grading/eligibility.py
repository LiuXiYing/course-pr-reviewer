"""Trusted pre-deadline eligibility for the 48-hour correction window.

A correction claim may only be created when the head that was pushed before the
deadline already contained real work. Everything here is deterministic and
operates on the exact snapshot of that head:

* the changed paths, taken from the trusted PR metadata;
* each file's blob SHA, from the GitHub API;
* the bytes of those blobs, read through an injected loader so this module never
  calls the network by itself and never executes student code.

Blank files, template copies, renamed/deleted files, out-of-scope paths and blank
images all fail. Anything that cannot be verified returns ``UNKNOWN`` so the
caller can retry instead of granting an unearned 100%.
"""

from __future__ import annotations

import difflib
import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..ai import BINARY_SUFFIXES
from ..path_utils import canonical_filename

ELIGIBLE = "ELIGIBLE"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
UNKNOWN = "UNKNOWN"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
DEFAULT_MIN_IMAGE_BYTES = 10_240
DEFAULT_MIN_IMAGE_SIDE = 64
# 与教师模板相比，新增的有效字符少于此值（或模板长度的 5%）时，视为
# “只改了姓名/纯复制模板”，不给修正资格。
MIN_NEW_CHARACTERS = 20
MIN_NEW_RATIO = 0.05
# 模板内容至少要有这么多出现在提交里，才谈得上“只改了姓名”。
TEMPLATE_COVERAGE = 0.6

_WHITESPACE_RE = re.compile(r"\s+")

BlobLoader = Callable[[str], bytes]


@dataclass(frozen=True)
class FileFact:
    path: str
    status: str = "added"
    blob_sha: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class EligibilityVerdict:
    status: str
    reasons: tuple[str, ...] = ()
    files: tuple[dict[str, str], ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return self.status == ELIGIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligibility": self.status,
            "reasons": list(self.reasons),
            "files": [dict(item) for item in self.files],
            **({"detail": self.detail} if self.detail else {}),
        }


def _normalise(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def is_template_copy(content: str, template: str) -> bool:
    """提交只是在模板上填了姓名/日期，没有真正写实验内容。

    直接比整体相似率会惩罚短模板；这里同时看两件事：模板内容几乎都在（覆盖率），
    以及新增字符很少（少于 max(20, 模板长度 5%)）。
    """
    left = _normalise(content)
    right = _normalise(template)
    if not left:
        return True
    if left == right:
        return True
    if not right:
        return False
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    added = max(0, len(left) - matched)
    coverage = matched / len(right) if right else 0.0
    limit = max(MIN_NEW_CHARACTERS, int(len(right) * MIN_NEW_RATIO))
    # 两个条件缺一不可：模板本身要几乎全在（coverage），且新增内容很少（added）。
    # 只看新增字符会把“完全另写但很短”的真实报告误判成模板复制。
    return added <= limit and coverage >= TEMPLATE_COVERAGE


def _required_names(assignment: Mapping[str, Any]) -> list[list[str]]:
    groups: list[list[str]] = []
    for requirement in assignment.get("required_files", []):
        if isinstance(requirement, str):
            groups.append([requirement])
        elif isinstance(requirement, dict) and "one_of" in requirement:
            groups.append(list(requirement["one_of"]))
    return groups


def _image_issue(raw: bytes, *, min_bytes: int, min_side: int) -> str | None:
    from PIL import Image, UnidentifiedImageError

    if len(raw) < min_bytes:
        return f"图片只有 {len(raw)} 字节，明显是占位图"
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            width, height = image.size
            if width < min_side or height < min_side:
                return f"图片尺寸 {width}×{height} 小于 {min_side} 像素"
            converted = image.convert("RGB")
            extrema = converted.getextrema()
            if all(low == high for low, high in extrema):
                return "图片是单一颜色的空白图"
    except (UnidentifiedImageError, OSError, ValueError):
        return "图片无法解析为合法图像"
    return None


def judge_claim(
    *,
    files: Sequence[FileFact],
    assignment: Mapping[str, Any],
    expected_prefix: str,
    template_texts: Mapping[str, str] | None = None,
    load_blob: BlobLoader | None = None,
    min_image_bytes: int = DEFAULT_MIN_IMAGE_BYTES,
    min_image_side: int = DEFAULT_MIN_IMAGE_SIDE,
    require_substantive: bool = True,
) -> EligibilityVerdict:
    """Decide whether this head is a substantive pre-deadline submission."""
    reasons: list[str] = []
    prefix_raw = expected_prefix.strip("/") + "/"
    prefix = canonical_filename(prefix_raw)
    used: list[FileFact] = []

    if not files:
        return EligibilityVerdict(NOT_ELIGIBLE, ("没有任何文件变更",))

    for item in files:
        if item.status in {"removed", "renamed"}:
            reasons.append(f"`{item.path}` 是删除或重命名，不能作为有效提交")
            continue
        if not canonical_filename(item.path).startswith(prefix):
            reasons.append(f"`{item.path}` 不在本次作业目录 `{expected_prefix}` 内")
            continue
        used.append(item)
    if reasons:
        return EligibilityVerdict(NOT_ELIGIBLE, tuple(reasons))

    submitted = {
        canonical_filename(item.path[len(prefix_raw):]): item for item in used
    }
    required_groups = _required_names(assignment)
    for alternatives in required_groups:
        if not any(
            canonical_filename(name) in submitted for name in alternatives
        ):
            label = " 或 ".join(f"`{name}`" for name in alternatives)
            reasons.append(f"缺少必交文件：{label}")
    if reasons:
        return EligibilityVerdict(NOT_ELIGIBLE, tuple(reasons))

    required_files = {
        canonical_filename(name)
        for alternatives in required_groups
        for name in alternatives
    }
    minimum_lines = int(assignment.get("min_nonempty_lines", 0) or 0)
    verified: list[dict[str, str]] = []
    unknown: list[str] = []
    inspected = 0

    for item in used:
        suffix = PurePosixPath(item.path).suffix.casefold()
        relative = canonical_filename(item.path[len(prefix_raw):])
        if item.size == 0:
            reasons.append(f"`{item.path}` 是空文件")
            continue
        is_required = relative in required_files
        if load_blob is None or not item.blob_sha:
            unknown.append(item.path)
            continue
        try:
            raw = load_blob(item.blob_sha)
        except Exception:  # noqa: BLE001 - any read failure means "cannot verify"
            unknown.append(item.path)
            continue
        inspected += 1
        if suffix in IMAGE_SUFFIXES:
            issue = _image_issue(
                raw, min_bytes=min_image_bytes, min_side=min_image_side
            )
            if issue:
                reasons.append(f"`{item.path}` {issue}")
                continue
        elif suffix in BINARY_SUFFIXES:
            if not raw:
                reasons.append(f"`{item.path}` 是空文件")
                continue
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                if not raw:
                    reasons.append(f"`{item.path}` 是空文件")
                    continue
                text = ""
            if text and not text.strip():
                reasons.append(f"`{item.path}` 只有空白字符")
                continue
            if text and is_required and minimum_lines > 0:
                nonempty = sum(1 for line in text.splitlines() if line.strip())
                if nonempty < minimum_lines:
                    reasons.append(
                        f"`{item.path}` 只有 {nonempty} 行非空内容，"
                        f"少于要求的 {minimum_lines} 行"
                    )
                    continue
            template = (template_texts or {}).get(item.path)
            if (
                require_substantive
                and template is not None
                and text
                and is_template_copy(text, template)
            ):
                reasons.append(
                    f"`{item.path}` 与教师模板基本相同（只填写了极少量内容），"
                    "不算实质性提交"
                )
                continue
        verified.append(
            {
                "path": item.path,
                **({"blob_sha": item.blob_sha} if item.blob_sha else {}),
            }
        )

    if reasons:
        return EligibilityVerdict(NOT_ELIGIBLE, tuple(reasons))
    if unknown and require_substantive:
        return EligibilityVerdict(
            UNKNOWN,
            tuple(f"无法读取 `{path}` 的内容，暂不能确认提交是否实质性" for path in unknown),
            tuple(verified),
            {"unverified_files": unknown},
        )
    if require_substantive and inspected == 0:
        return EligibilityVerdict(
            UNKNOWN, ("没有任何文件内容可用于验证实质性",), tuple(verified)
        )
    return EligibilityVerdict(ELIGIBLE, (), tuple(verified))
