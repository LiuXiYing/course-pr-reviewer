"""Trusted pull-request metadata and GitHub API loading."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .exceptions import ContentLimitExceeded, ReviewSystemError

SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_FILE_STATUSES = {"added", "modified", "removed", "renamed", "copied", "changed"}


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    previous_filename: str | None = None
    blob_sha: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class PullRequestSnapshot:
    repository: str
    number: int
    title: str
    author_login: str
    captured_head_sha: str
    current_head_sha: str
    event_at: dt.datetime
    files: tuple[ChangedFile, ...]


def _aware_datetime(value: str, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ReviewSystemError(f"{field} 不是有效时间")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReviewSystemError(f"{field} 不是有效时间") from exc
    if parsed.tzinfo is None:
        raise ReviewSystemError(f"{field} 必须包含时区")
    return parsed


def _safe_file_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and value != "."
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and not any(character in value for character in ("\n", "\r", "\0"))
    )


def _changed_file(data: dict[str, Any]) -> ChangedFile:
    filename = data.get("filename")
    status = data.get("status")
    previous = data.get("previous_filename")
    blob_sha = data.get("blob_sha", data.get("sha"))
    content = data.get("content")
    if not isinstance(filename, str) or not _safe_file_path(filename):
        raise ReviewSystemError("变更文件包含不安全路径")
    if status not in ALLOWED_FILE_STATUSES:
        raise ReviewSystemError(f"未知文件状态：{status}")
    if previous is not None and (
        not isinstance(previous, str) or not _safe_file_path(previous)
    ):
        raise ReviewSystemError("重命名文件包含不安全原路径")
    if blob_sha is not None and (
        not isinstance(blob_sha, str) or not SHA_RE.fullmatch(blob_sha)
    ):
        raise ReviewSystemError("变更文件包含无效 blob SHA")
    if content is not None and not isinstance(content, str):
        raise ReviewSystemError("变更文件的 content 必须是文本")
    return ChangedFile(
        filename=filename,
        status=status,
        previous_filename=previous,
        blob_sha=blob_sha.lower() if blob_sha else None,
        content=content,
    )


def snapshot_from_dict(data: dict[str, Any]) -> PullRequestSnapshot:
    required_strings = (
        "repository",
        "title",
        "author_login",
        "captured_head_sha",
        "current_head_sha",
    )
    for field in required_strings:
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ReviewSystemError(f"PR 快照缺少 {field}")
    if not REPOSITORY_RE.fullmatch(data["repository"]):
        raise ReviewSystemError("PR 快照中的 repository 无效")
    if not SHA_RE.fullmatch(data["captured_head_sha"]) or not SHA_RE.fullmatch(
        data["current_head_sha"]
    ):
        raise ReviewSystemError("PR 快照中的 head SHA 无效")
    number = data.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ReviewSystemError("PR 快照中的 number 无效")
    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not all(
        isinstance(item, dict) for item in raw_files
    ):
        raise ReviewSystemError("PR 快照中的 files 无效")
    return PullRequestSnapshot(
        repository=data["repository"],
        number=number,
        title=data["title"],
        author_login=data["author_login"],
        captured_head_sha=data["captured_head_sha"].lower(),
        current_head_sha=data["current_head_sha"].lower(),
        event_at=_aware_datetime(data.get("event_at"), "event_at"),
        files=tuple(_changed_file(item) for item in raw_files),
    )


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise ReviewSystemError("缺少 GitHub Token，无法获取 PR 当前状态")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        encoded_body = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            data=encoded_body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "course-pr-reviewer",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in expected:
                    raise ReviewSystemError(
                        f"GitHub API {method} {path} 返回 HTTP {response.status}"
                    )
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ReviewSystemError("GitHub API 响应超过 2 MB 安全上限")
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(501).decode("utf-8", errors="replace")
            except OSError:
                detail = ""
            detail = detail.replace("\n", " ")[:500]
            suffix = f"：{detail}" if detail else ""
            raise ReviewSystemError(
                f"GitHub API {method} {path} 返回 HTTP {exc.code}{suffix}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ReviewSystemError(f"GitHub API 请求失败：{exc}") from exc

    def get_json(self, path: str) -> Any:
        return self.request_json("GET", path)

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self.request_json("POST", path, body=body, expected=(200, 201))

    def patch_json(self, path: str, body: dict[str, Any]) -> Any:
        return self.request_json("PATCH", path, body=body, expected=(200,))

    def put_json(self, path: str, body: dict[str, Any]) -> Any:
        return self.request_json("PUT", path, body=body, expected=(200, 201))

    def pull_request(self, repository: str, number: int) -> dict[str, Any]:
        repo = urllib.parse.quote(repository, safe="/")
        result = self.get_json(f"/repos/{repo}/pulls/{number}")
        if not isinstance(result, dict):
            raise ReviewSystemError("GitHub API 返回了无效的 PR 数据")
        return result

    def changed_files(self, repository: str, number: int) -> list[dict[str, Any]]:
        repo = urllib.parse.quote(repository, safe="/")
        files: list[dict[str, Any]] = []
        for page in range(1, 31):
            batch = self.get_json(
                f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}"
            )
            if not isinstance(batch, list) or not all(
                isinstance(item, dict) for item in batch
            ):
                raise ReviewSystemError("GitHub API 返回了无效的文件列表")
            files.extend(batch)
            if len(batch) < 100:
                return files
        raise ReviewSystemError("PR 变更文件超过 3000 个自动审核上限")

    def blob_bytes(self, repository: str, sha: str, *, max_bytes: int) -> bytes:
        if not SHA_RE.fullmatch(sha):
            raise ReviewSystemError("GitHub blob SHA 无效")
        repo = urllib.parse.quote(repository, safe="/")
        result = self.get_json(f"/repos/{repo}/git/blobs/{sha}")
        if not isinstance(result, dict):
            raise ReviewSystemError("GitHub API 返回了无效的 blob 数据")
        size = result.get("size")
        encoded = result.get("content")
        encoding = result.get("encoding")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ReviewSystemError("GitHub blob 缺少有效大小")
        if size > max_bytes:
            raise ContentLimitExceeded(
                f"文件大小 {size} 字节，超过 AI 审核上限 {max_bytes} 字节"
            )
        if encoding != "base64" or not isinstance(encoded, str):
            raise ReviewSystemError("GitHub blob 不是可支持的 base64 内容")
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ReviewSystemError("GitHub blob base64 内容无效") from exc
        if len(raw) != size:
            raise ReviewSystemError("GitHub blob 解码后大小不一致")
        return raw

    def text_blob(self, repository: str, sha: str, *, max_bytes: int) -> str | None:
        raw = self.blob_bytes(repository, sha, max_bytes=max_bytes)
        if b"\0" in raw:
            return None
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None


def load_snapshot(
    metadata_dir: str | Path,
    *,
    github_token: str | None = None,
    repository: str | None = None,
    api_url: str = "https://api.github.com",
) -> PullRequestSnapshot:
    directory = Path(metadata_dir)
    snapshot_path = directory / "snapshot.json"
    if snapshot_path.is_file():
        try:
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReviewSystemError(f"无法读取 PR 快照：{exc}") from exc
        if not isinstance(data, dict):
            raise ReviewSystemError("snapshot.json 顶层必须是对象")
        return snapshot_from_dict(data)

    event_path = directory / "event.json"
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewSystemError(f"缺少 PR 元数据：{event_path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewSystemError(f"无法读取 PR 元数据：{exc}") from exc
    if not isinstance(event, dict):
        raise ReviewSystemError("event.json 顶层必须是对象")

    event_repository = event.get("repository")
    if not isinstance(event_repository, str) or event_repository != repository:
        raise ReviewSystemError("PR 元数据的仓库与当前运行仓库不一致")
    number = event.get("number")
    captured_title = event.get("title")
    captured_sha = event.get("head_sha")
    event_at = event.get("event_at")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ReviewSystemError("event.json 中的 PR 编号无效")
    if not isinstance(captured_sha, str) or not SHA_RE.fullmatch(captured_sha):
        raise ReviewSystemError("event.json 中的 head SHA 无效")
    if not isinstance(captured_title, str) or not captured_title:
        raise ReviewSystemError("event.json 中的 PR 标题无效")

    client = GitHubClient(github_token or "", api_url=api_url)
    pr = client.pull_request(event_repository, number)
    files = client.changed_files(event_repository, number)
    if pr.get("title") != captured_title:
        raise ReviewSystemError("PR 标题在信息收集后又发生了更新")
    try:
        snapshot_data = {
            "repository": event_repository,
            "number": number,
            "title": pr["title"],
            "author_login": pr["user"]["login"],
            "captured_head_sha": captured_sha,
            "current_head_sha": pr["head"]["sha"],
            "event_at": event_at,
            "files": files,
        }
    except (KeyError, TypeError) as exc:
        raise ReviewSystemError("GitHub API PR 数据缺少必要字段") from exc
    return snapshot_from_dict(snapshot_data)
