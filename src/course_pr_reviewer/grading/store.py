"""Read and write the per-course ``grades`` branch.

Two implementations share one small interface:

``GitHubContentsStore``
    Uses the Contents API with the file's blob SHA as the optimistic-concurrency
    token. No clone, no ``git push --force``, no branch checkout, so a failed
    write can never discard another student's record.

``LocalDirStore``
    A plain directory, used by the CLI (``--dir``) and by the tests.

The store only ever writes JSON facts and CSV reports; it never writes to the
default branch and never loads executable content from ``grades``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ..exceptions import ContentConflict, GradeStoreError, ReviewSystemError
from ..snapshot import GitHubClient
from . import records

MAX_RECORD_BYTES = 200_000
WRITE_ATTEMPTS = 4


class GradeStore(Protocol):
    branch: str

    def read_text(self, path: str) -> str | None: ...

    def write_text(self, path: str, content: str, message: str) -> None: ...

    def delete(self, path: str, message: str) -> None: ...

    def list_paths(self, directory: str) -> list[str]: ...


def _safe_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "\\" in path
        or any(char in path for char in ("\n", "\r", "\0"))
    ):
        raise GradeStoreError(f"grades 分支路径不安全：{path!r}")
    return path


def _encode(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class LocalDirStore:
    """A directory that behaves like the ``grades`` branch."""

    def __init__(self, root: str | Path, branch: str = "grades") -> None:
        self.root = Path(root)
        self.branch = branch

    def _path(self, path: str) -> Path:
        return self.root / _safe_path(path)

    def exists(self) -> bool:
        return self.root.is_dir()

    def read_text(self, path: str) -> str | None:
        target = self._path(path)
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise GradeStoreError(f"无法读取 {path}：{exc}") from exc

    def write_text(self, path: str, content: str, message: str) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)

    def delete(self, path: str, message: str) -> None:
        target = self._path(path)
        if target.is_file():
            target.unlink()

    def list_paths(self, directory: str) -> list[str]:
        target = self._path(directory)
        if not target.is_dir():
            return []
        return sorted(
            str(PurePosixPath(directory) / child.name)
            for child in target.iterdir()
            if child.is_file()
        )

    def ensure_branch(
        self, *, course_id: str, course_name: str, timezone: str
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        metadata = self.read_json(records.METADATA_PATH)
        if metadata is None:
            metadata = _initial_metadata(
                course_id=course_id, course_name=course_name, timezone=timezone
            )
            self.write_json(
                records.METADATA_PATH, metadata, "chore: 初始化 grades 分支元数据"
            )
        if self.read_text(records.README_PATH) is None:
            self.write_text(
                records.README_PATH, _readme(course_name), "docs: 说明 grades 分支用途"
            )
        return metadata

    # Convenience wrappers shared by both stores -------------------------
    def read_json(self, path: str) -> dict[str, Any] | None:
        raw = self.read_text(path)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GradeStoreError(f"{path} 不是有效 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise GradeStoreError(f"{path} 顶层必须是对象")
        return parsed

    def write_json(self, path: str, data: dict[str, Any], message: str) -> None:
        self.write_text(path, _encode(data), message)


class DryRunStore:
    """Wraps a store and records writes without performing them."""

    def __init__(self, inner: GradeStore) -> None:
        self.inner = inner
        self.branch = inner.branch
        self.writes: list[tuple[str, str]] = []

    def read_text(self, path: str) -> str | None:
        return self.inner.read_text(path)

    def write_text(self, path: str, content: str, message: str) -> None:
        self.writes.append((path, message))

    def delete(self, path: str, message: str) -> None:
        self.writes.append((path, message))

    def list_paths(self, directory: str) -> list[str]:
        return self.inner.list_paths(directory)

    def read_json(self, path: str) -> dict[str, Any] | None:
        raw = self.read_text(path)
        if raw is None:
            return None
        return json.loads(raw)

    def write_json(self, path: str, data: dict[str, Any], message: str) -> None:
        self.write_text(path, _encode(data), message)

    def ensure_branch(
        self, *, course_id: str, course_name: str, timezone: str
    ) -> dict[str, Any]:
        existing = self.read_json(records.METADATA_PATH)
        if existing is not None:
            return existing
        metadata = _initial_metadata(
            course_id=course_id, course_name=course_name, timezone=timezone
        )
        self.write_json(
            records.METADATA_PATH, metadata, "chore: 初始化 grades 分支元数据"
        )
        self.write_text(
            records.README_PATH, _readme(course_name), "docs: 说明 grades 分支用途"
        )
        return metadata


class GitHubContentsStore:
    """The ``grades`` branch of one repository, via the Contents API."""

    def __init__(
        self,
        github: GitHubClient,
        repository: str,
        branch: str = "grades",
        *,
        sleep=time.sleep,
    ) -> None:
        self.github = github
        self.repository = repository
        self.branch = branch
        self._sleep = sleep

    def _contents_path(self, path: str) -> str:
        import urllib.parse

        repo = urllib.parse.quote(self.repository, safe="/")
        filename = urllib.parse.quote(_safe_path(path), safe="/")
        return f"/repos/{repo}/contents/{filename}"

    def read_text(self, path: str) -> str | None:
        entry = self.github.get_json(
            f"{self._contents_path(path)}?ref={self.branch}", allow_missing=True
        )
        if entry is None:
            return None
        if not isinstance(entry, dict) or entry.get("type") != "file":
            raise GradeStoreError(f"{path} 不是 grades 分支中的普通文件")
        if entry.get("encoding") != "base64" or not isinstance(entry.get("content"), str):
            raise GradeStoreError(f"{path} 的返回内容不是 base64 文本")
        if int(entry.get("size", 0)) > MAX_RECORD_BYTES:
            raise GradeStoreError(f"{path} 超过 {MAX_RECORD_BYTES} 字节安全上限")
        try:
            return base64.b64decode(entry["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GradeStoreError(f"{path} 内容无法解码：{exc}") from exc

    def _current_sha(self, path: str) -> str | None:
        entry = self.github.get_json(
            f"{self._contents_path(path)}?ref={self.branch}", allow_missing=True
        )
        if isinstance(entry, dict) and isinstance(entry.get("sha"), str):
            return entry["sha"]
        return None

    def write_text(self, path: str, content: str, message: str) -> None:
        _safe_path(path)
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        last_error: Exception | None = None
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            sha = self._current_sha(path)
            body: dict[str, Any] = {
                "message": message,
                "content": payload,
                "branch": self.branch,
            }
            if sha:
                body["sha"] = sha
            try:
                self.github.put_json(self._contents_path(path), body)
                return
            except ContentConflict as exc:
                last_error = exc
                if attempt < WRITE_ATTEMPTS:
                    self._sleep(min(2 ** attempt * 0.5, 8))
                    continue
            except ReviewSystemError as exc:
                raise GradeStoreError(
                    f"无法写入 {path}（{self.branch} 分支）：{exc}"
                ) from exc
        raise GradeStoreError(
            f"写入 {path} 连续 {WRITE_ATTEMPTS} 次冲突，请稍后由对账任务重试：{last_error}"
        )

    def delete(self, path: str, message: str) -> None:
        sha = self._current_sha(path)
        if sha is None:
            return
        self.github.request_json(
            "DELETE",
            self._contents_path(path),
            body={"message": message, "sha": sha, "branch": self.branch},
            expected=(200,),
        )

    def list_paths(self, directory: str) -> list[str]:
        entry = self.github.get_json(
            f"{self._contents_path(directory)}?ref={self.branch}", allow_missing=True
        )
        if entry is None:
            return []
        if isinstance(entry, dict):
            return [str(entry.get("path", directory))]
        if not isinstance(entry, list):
            raise GradeStoreError(f"{directory} 的目录列表格式无效")
        paths: list[str] = []
        for item in entry:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(item["path"])
        return sorted(paths)

    def ensure_branch(
        self, *, course_id: str, course_name: str, timezone: str
    ) -> dict[str, Any]:
        import urllib.parse

        repo = urllib.parse.quote(self.repository, safe="/")
        existing = self.github.get_json(
            f"/repos/{repo}/branches/{self.branch}", allow_missing=True
        )
        if existing is None:
            metadata = _initial_metadata(
                course_id=course_id, course_name=course_name, timezone=timezone
            )
            self._create_orphan_branch(metadata, course_name)
            return metadata
        metadata = self.read_json(records.METADATA_PATH)
        if metadata is None:
            metadata = _initial_metadata(
                course_id=course_id, course_name=course_name, timezone=timezone
            )
            self.write_json(
                records.METADATA_PATH, metadata, "chore: 初始化 grades 分支元数据"
            )
        return metadata

    def _create_orphan_branch(
        self, metadata: dict[str, Any], course_name: str
    ) -> None:
        """Create the branch from a parentless commit (no student content)."""
        import urllib.parse

        repo = urllib.parse.quote(self.repository, safe="/")
        blobs = []
        for path, text in (
            (records.README_PATH, _readme(course_name)),
            (records.METADATA_PATH, _encode(metadata)),
        ):
            created = self.github.post_json(
                f"/repos/{repo}/git/blobs",
                {
                    "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if not isinstance(created, dict) or not isinstance(created.get("sha"), str):
                raise GradeStoreError("创建 grades 分支 blob 失败")
            blobs.append({"path": path, "mode": "100644", "type": "blob", "sha": created["sha"]})
        tree = self.github.post_json(
            f"/repos/{repo}/git/trees", {"tree": blobs}
        )
        if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
            raise GradeStoreError("创建 grades 分支 tree 失败")
        commit = self.github.post_json(
            f"/repos/{repo}/git/commits",
            {
                "message": "chore: 初始化 grades 分支",
                "tree": tree["sha"],
                "parents": [],
            },
        )
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
            raise GradeStoreError("创建 grades 分支首个提交失败")
        self.github.request_json(
            "POST",
            f"/repos/{repo}/git/refs",
            body={"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]},
            expected=(201, 200),
        )

    def read_json(self, path: str) -> dict[str, Any] | None:
        raw = self.read_text(path)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GradeStoreError(f"{path} 不是有效 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise GradeStoreError(f"{path} 顶层必须是对象")
        return parsed

    def write_json(self, path: str, data: dict[str, Any], message: str) -> None:
        self.write_text(path, _encode(data), message)


def _initial_metadata(*, course_id: str, course_name: str, timezone: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    return {
        "schema_version": records.SCHEMA_VERSION,
        "course_id": course_id,
        "course_name": course_name,
        "timezone": timezone,
        "created_at": now.isoformat(),
        "created_by": "course-pr-reviewer",
        "notes": [
            "本分支由课程审核器写入，请勿手工修改 receipts/ 与 claims/。",
            "reports/ 可以随时由 receipts 重新生成。",
            "公开仓库中的本分支同样是公开的，不要在此记录姓名、联系方式或申诉材料。",
        ],
    }


def _readme(course_name: str) -> str:
    return f"""# {course_name} 成绩记录分支

本分支由 [course-pr-reviewer](https://github.com/LiuXiYing/course-pr-reviewer)
自动写入，保存可信的验收与计分事实。

- `metadata.json`：课程标识与 schema 版本。
- `claims/`：截止前有效提交的快照，用于 48 小时修正期判定。
- `receipts/`：每个学生每次作业的最终验收与得分事实。
- `overrides/`：教师审批的例外记录，不改写原始收据。
- `reports/`：由收据重新生成的汇总表，可随时删除重建。
- `audit/`：覆盖与更正操作日志。

请勿手工修改 `receipts/` 与 `claims/`；需要改分时由教师工作流写入 `overrides/`。
本分支在公开仓库中同样是公开的，不记录姓名、联系方式或申诉材料。
"""


def open_store(
    *,
    root: str | Path | None = None,
    github: GitHubClient | None = None,
    repository: str | None = None,
    branch: str = "grades",
    dry_run: bool = False,
) -> GradeStore:
    """Pick a store for the current invocation."""
    if root is not None:
        store: GradeStore = LocalDirStore(root, branch=branch)
    else:
        if github is None or not repository:
            raise GradeStoreError("必须提供 --dir 或 GitHub 凭据才能访问 grades 分支")
        store = GitHubContentsStore(github, repository, branch=branch)
    return DryRunStore(store) if dry_run else store
