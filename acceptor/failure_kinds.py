"""VAR1：失败分类器——从 pytest 输出按失败节点逐条抽异常类型。

只读输出：不改测试、不重跑、不参与 PASS/FAIL 判定。抽不到就归
「未分类」，不猜。类别是给派单拼对症提醒用的，不进冻结清单。

抽取顺序（逐节点，先命中先用）：
1. 短摘要行 ``FAILED <node> - <msg>``：msg 以 ``异常名:`` 开头或只有
   异常名，取该名；msg 以 ``assert `` 开头是 pytest 断言改写，记断言错。
2. 长回溯段（``____ <名> ____`` 到下一段）末尾的 ``路径:行号: 异常名``。
两处都抽不到 → 未分类。
"""

from __future__ import annotations

import re

ASSERTION = "断言错"
IMPORT = "导入失败"
TIMEOUT = "超时"
OTHER = "其他异常"
UNCLASSIFIED = "未分类"

KINDS = (ASSERTION, IMPORT, TIMEOUT, OTHER, UNCLASSIFIED)

_SUMMARY = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE)
_EXC_HEAD = re.compile(r"^([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)(?::\s*(.*))?$")
_SECTION = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.MULTILINE)
_SECTION_END = re.compile(r"^(?:={3,}|_{3,}).*$", re.MULTILINE)
_LOCATION = re.compile(r"^\S+:\d+:\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*$", re.MULTILINE)
_TIMEOUT_WORD = re.compile(r"\btime(?:d)?\s*out\b|\btimeout\b", re.IGNORECASE)

_IMPORT_NAMES = {"ImportError", "ModuleNotFoundError"}


def _looks_like_exception(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return bool(re.match(r"^[A-Z]", last)) and (
        last.endswith(("Error", "Exception", "Exit", "Interrupt", "Expired"))
        or last in {"Failed", "StopIteration", "Timeout"}
    )


def kind_of_exception(name: str, message: str = "") -> str:
    """异常名（+消息）→ 类别。不是异常名的一律未分类。"""
    last = name.rsplit(".", 1)[-1]
    if last == "AssertionError":
        return ASSERTION
    if last in _IMPORT_NAMES:
        return IMPORT
    if "Timeout" in last or last == "TimeoutExpired":
        return TIMEOUT
    if last == "Failed" and _TIMEOUT_WORD.search(message or ""):
        # pytest-timeout 抛的是 Failed: Timeout >Ns
        return TIMEOUT
    if _looks_like_exception(name):
        return OTHER
    return UNCLASSIFIED


def _from_summary(message: str | None) -> str:
    if not message:
        return UNCLASSIFIED
    text = message.strip()
    if text.startswith("assert ") or text == "assert":
        return ASSERTION
    match = _EXC_HEAD.match(text)
    if not match:
        return UNCLASSIFIED
    name = match.group(1)
    if match.group(2) is None and not _looks_like_exception(name):
        return UNCLASSIFIED
    if match.group(2) is not None and not _looks_like_exception(name):
        return UNCLASSIFIED
    return kind_of_exception(name, match.group(2) or "")


def _section_title(node_id: str) -> str:
    """tests/x.py::Cls::test_y[p] → Cls.test_y[p]（pytest 长回溯段标题）。"""
    parts = node_id.split("::")[1:]
    return ".".join(parts)


def _sections(output: str) -> dict[str, str]:
    found: dict[str, str] = {}
    heads = list(_SECTION.finditer(output))
    for index, head in enumerate(heads):
        start = head.end()
        end = heads[index + 1].start() if index + 1 < len(heads) else len(output)
        body = output[start:end]
        stop = re.search(r"^={3,}", body, re.MULTILINE)
        if stop:
            body = body[: stop.start()]
        found.setdefault(head.group(1), body)
    return found


def _from_section(body: str) -> str:
    locations = _LOCATION.findall(body)
    if not locations:
        return UNCLASSIFIED
    name = locations[-1]
    message = ""
    for line in body.splitlines():
        if line.startswith("E "):
            message += line[1:].strip() + "\n"
    return kind_of_exception(name, message)


def classify_pytest_failures(output: str, failed_tests: list[str]) -> dict[str, str]:
    """{失败节点 id: 类别}。只覆盖 failed_tests 里的节点，顺序同 failed_tests。"""
    summary: dict[str, str | None] = {}
    for match in _SUMMARY.finditer(output or ""):
        summary.setdefault(match.group(1), match.group(2))
    sections = _sections(output or "")
    kinds: dict[str, str] = {}
    for node in failed_tests:
        kind = _from_summary(summary.get(node))
        if kind == UNCLASSIFIED:
            body = sections.get(_section_title(node))
            if body is not None:
                kind = _from_section(body)
        kinds[node] = kind
    return kinds
