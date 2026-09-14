"""Opt-in checks for required addresses in Markdown observation tables."""

from __future__ import annotations

import re

from .models import Issue, ReasonCode


_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEX_ADDRESS = re.compile(r"(?:0[xX])?[0-9a-fA-F]+")


def _plain(value: str) -> str:
    return re.sub(r"[\s`*_]", "", value)


def _section_rows(content: str, section: str):
    # Hidden comments and fenced examples are not submitted table entries.
    content = re.sub(
        r"<!--.*?-->", lambda match: "\n" * match[0].count("\n"),
        content, flags=re.S,
    )
    level: int | None = None
    fence: str | None = None
    for line_number, line in enumerate(content.splitlines(), 1):
        marker = _FENCE.match(line)
        if fence is not None:
            if marker and marker[1].startswith(fence):
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        heading = _HEADING.match(line)
        if heading:
            label = heading[2]
            if label == section or label.startswith(section + " "):
                level = len(heading[1])
            elif level is not None and len(heading[1]) <= level:
                level = None
            continue
        if level is not None and "|" in line:
            cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
            yield line_number, line, cells


def required_address_issues(
    content: str, requirements: list[dict], filename: str,
) -> list[Issue]:
    issues: list[Issue] = []
    for requirement in requirements:
        section, row = requirement["section"], requirement["row"]
        matches = [
            (number, line, cells)
            for number, line, cells in _section_rows(content, section)
            if _plain(cells[0]) == _plain(row)
        ]
        rule = f"{section} 的 `{row}` 必须填写实际十六进制地址，不能留空或保留‘填写’。"
        if not matches:
            issues.append(Issue(
                code=ReasonCode.REQUIRED_ADDRESS_MISSING,
                message=f"未找到 {section} 的 `{row}` 地址记录行，请保留表格并填写地址。",
                file=filename, location=section, rule=rule,
            ))
            continue
        for number, line, cells in matches:
            for column in requirement["columns"]:
                value = cells[column] if column < len(cells) else ""
                if _HEX_ADDRESS.fullmatch(_plain(value).strip("()（）")):
                    continue
                issues.append(Issue(
                    code=ReasonCode.REQUIRED_ADDRESS_MISSING,
                    message=(f"{section} 的 `{row}` 第 {column} 个数据列未填写十六进制地址；"
                             "请把空白或占位文字替换为实际地址。"),
                    file=filename, location=f"第 {number} 行，第 {column} 个数据列",
                    rule=rule, evidence=line.strip(),
                ))
    return issues
