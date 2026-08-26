"""Load and semantically validate course and student YAML files."""

from __future__ import annotations

import datetime as dt
import string
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .exceptions import ConfigurationError


@dataclass(frozen=True)
class Student:
    student_id: str
    name: str
    github: str
    class_name: str | None = None
    active: bool = True

    @property
    def identity(self) -> str:
        return f"{self.student_id}{self.name}"


@dataclass(frozen=True)
class StudentRoster:
    by_student_id: dict[str, Student]
    by_github: dict[str, Student]

    def find_by_github(self, login: str) -> Student | None:
        return self.by_github.get(login.casefold())


@dataclass(frozen=True)
class CourseConfiguration:
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return self.data["course"]["name"]

    @property
    def timezone(self) -> str:
        return self.data["course"]["timezone"]

    @property
    def assignments(self) -> dict[str, dict[str, Any]]:
        return self.data["assignments"]

    @property
    def ai(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "model": "glm-4.7-flash",
            "min_confidence": 0.8,
            "timeout_seconds": 60,
            "max_attempts": 3,
            "max_file_bytes": 200_000,
            "max_total_bytes": 500_000,
            "max_output_tokens": 2048,
        }
        defaults.update(self.data.get("ai", {}))
        return defaults

    @property
    def ocr(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "detection_model": "PP-OCRv6_small_det",
            "recognition_model": "PP-OCRv6_small_rec",
            "min_line_confidence": 0.65,
            "max_text_chars": 20_000,
        }
        defaults.update(self.data.get("ocr", {}))
        return defaults

    @property
    def vision(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "model": "glm-4.6v-flash",
            "min_confidence": 0.85,
            "fail_confidence": 0.9,
            "timeout_seconds": 90,
            "max_attempts": 3,
            "max_images": 6,
            "max_image_bytes": 5_000_000,
            "max_total_bytes": 12_000_000,
            "max_pixels": 25_000_000,
            "max_side": 4096,
            "max_output_tokens": 2048,
        }
        defaults.update(self.data.get("vision", {}))
        return defaults

    def assignment(self, assignment_id: str) -> dict[str, Any] | None:
        return self.assignments.get(assignment_id)

    def feature_enabled(self, name: str) -> bool:
        return bool(self.data.get("features", {}).get(name, False))

    def assignment_feature_enabled(self, assignment_id: str, name: str) -> bool:
        assignment = self.assignments[assignment_id]
        return bool(assignment.get(name, self.feature_enabled(name)))

    def expected_title(self, student: Student, assignment_id: str) -> str:
        template = self.data["course"].get(
            "title_template", "[{student_id}{student_name}]{assignment_id}作业提交"
        )
        return template.format(
            student_id=student.student_id,
            student_name=student.name,
            student_identity=student.identity,
            assignment_id=assignment_id,
        )

    def expected_submission_dir(self, student: Student, assignment_id: str) -> str:
        template = self.data["course"].get(
            "submission_path_template", "{student_id}{student_name}/{assignment_id}"
        )
        return template.format(
            student_id=student.student_id,
            student_name=student.name,
            student_identity=student.identity,
            assignment_id=assignment_id,
        ).strip("/")


def _schema(name: str) -> dict[str, Any]:
    schema_path = files("course_pr_reviewer").joinpath("schemas", name)
    return yaml.safe_load(schema_path.read_text(encoding="utf-8"))


def _load_yaml(path: str | Path) -> dict[str, Any]:
    yaml_path = Path(path)
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置文件不存在：{yaml_path}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"无法读取 YAML：{yaml_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"YAML 顶层必须是对象：{yaml_path}")
    return data


def _validate_schema(data: dict[str, Any], schema_name: str, label: str) -> None:
    validator = Draft202012Validator(
        _schema(schema_name), format_checker=FormatChecker()
    )
    errors = sorted(
        validator.iter_errors(data), key=lambda error: list(error.absolute_path)
    )
    if not errors:
        return
    messages = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        messages.append(f"{location}: {error.message}")
    raise ConfigurationError(f"{label}格式无效：\n- " + "\n- ".join(messages))


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and value != "."
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
    )


def load_course_config(path: str | Path) -> CourseConfiguration:
    data = _load_yaml(path)
    _validate_schema(data, "course-review.schema.json", "课程配置")

    timezone = data["course"]["timezone"]
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(
            f"course.timezone 不是有效的 IANA 时区：{timezone}"
        ) from exc

    allowed_fields = {"student_id", "student_name", "student_identity", "assignment_id"}
    for key in ("title_template", "submission_path_template"):
        template = data["course"].get(key, "")
        if not template:
            continue
        try:
            parsed_template = tuple(string.Formatter().parse(template))
        except ValueError as exc:
            raise ConfigurationError(f"course.{key} 不是有效模板") from exc
        fields = [field for _, field, _, _ in parsed_template if field is not None]
        if any(
            format_spec or conversion
            for _, _, format_spec, conversion in parsed_template
        ):
            raise ConfigurationError(f"course.{key} 不允许格式化选项或类型转换")
        if set(fields) - allowed_fields:
            unknown = ", ".join(sorted(set(fields) - allowed_fields))
            raise ConfigurationError(f"course.{key} 包含未知字段：{unknown}")
        has_identity = "student_identity" in fields or {
            "student_id",
            "student_name",
        }.issubset(fields)
        if "assignment_id" not in fields or not has_identity:
            raise ConfigurationError(
                f"course.{key} 必须包含 assignment_id，并包含 student_identity "
                "或 student_id + student_name"
            )
        if len(fields) != len(set(fields)):
            raise ConfigurationError(f"course.{key} 中的占位符不能重复")

    submission_template = data["course"].get(
        "submission_path_template", "{student_id}{student_name}/{assignment_id}"
    )
    sample_path = submission_template.format(
        student_id="2023000000",
        student_name="张三",
        student_identity="2023000000张三",
        assignment_id="Lab1",
    )
    if not _safe_relative_path(sample_path) or sample_path.endswith("/"):
        raise ConfigurationError(
            "course.submission_path_template 必须生成安全的相对目录"
        )

    for assignment_id, assignment in data["assignments"].items():
        ai_enabled = assignment.get("ai_review", data["features"]["ai_review"])
        vision_enabled = assignment.get(
            "vision_review", data["features"]["vision_review"]
        )
        ocr_enabled = assignment.get("ocr_review", data["features"]["ocr_review"])
        if (ai_enabled or vision_enabled) and not assignment.get("review_points"):
            raise ConfigurationError(
                f"{assignment_id} 已启用 AI 或图片审核，必须配置至少一条 review_points"
            )
        if ocr_enabled and not vision_enabled:
            raise ConfigurationError(
                f"{assignment_id} 启用 ocr_review 时也必须启用 vision_review"
            )
        deadline = assignment["deadline"]
        try:
            parsed = dt.datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise ConfigurationError(f"{assignment_id}.deadline 不是有效时间") from exc
        if parsed.tzinfo is None:
            raise ConfigurationError(f"{assignment_id}.deadline 必须包含时区")

        for entry in assignment["required_files"]:
            values = [entry] if isinstance(entry, str) else entry["one_of"]
            for value in values:
                if not _safe_relative_path(value):
                    raise ConfigurationError(
                        f"{assignment_id} 包含不安全文件路径：{value}"
                    )
        for pattern in assignment.get("vision_files", []):
            if not _safe_relative_path(pattern):
                raise ConfigurationError(
                    f"{assignment_id} 包含不安全图片模式：{pattern}"
                )
    vision = CourseConfiguration(data).vision
    if vision["fail_confidence"] < vision["min_confidence"]:
        raise ConfigurationError(
            "vision.fail_confidence 不能低于 vision.min_confidence"
        )
    return CourseConfiguration(data)


def load_student_roster(path: str | Path) -> StudentRoster:
    data = _load_yaml(path)
    _validate_schema(data, "students.schema.json", "学生名单")
    by_student_id: dict[str, Student] = {}
    by_github: dict[str, Student] = {}
    for student_id, entry in data["students"].items():
        student = Student(
            student_id=student_id,
            name=entry["name"].strip(),
            github=entry["github"].strip(),
            class_name=entry.get("class"),
            active=entry.get("active", True),
        )
        if student.name != entry["name"]:
            raise ConfigurationError(f"学生 {student_id} 的姓名首尾不能包含空白")
        if student.github != entry["github"]:
            raise ConfigurationError(
                f"学生 {student_id} 的 GitHub 账号首尾不能包含空白"
            )
        login_key = student.github.casefold()
        if login_key in by_github:
            other = by_github[login_key]
            raise ConfigurationError(
                f"GitHub 用户名 {student.github} 同时绑定了 {other.student_id} 和 {student_id}"
            )
        by_student_id[student_id] = student
        by_github[login_key] = student
    return StudentRoster(by_student_id=by_student_id, by_github=by_github)
