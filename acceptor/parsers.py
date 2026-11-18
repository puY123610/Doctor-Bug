"""判卷输出解析器注册表：名字 → 解析函数。

冻结时把解析器名与检查命令一起写进 vault manifest（见
frozen.freeze），判卷时按名取解析器——命令与解析器是同一
份冻结契约的两半。LANG1 的新语言解析器也注册到这里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .failure_kinds import classify_pytest_failures


@dataclass
class ParseResult:
    failed_tests: list[str] = field(default_factory=list)
    passed_count: int = 0
    failed_count: int = 0
    executed: bool = False  # 有证据表明检查真的跑起来了
    # EVD1：纯增量。pytest 汇总里的「N errors」与 ERROR 行节点 id
    # （完整保留，收集错误时只有文件路径）。只进成绩单分类，
    # 不参与 executed/PASS/FAIL 判定；其他解析器不产出时为 0/空。
    error_count: int = 0
    error_tests: list[str] = field(default_factory=list)
    # VAR1：纯增量。{失败节点 id: 类别}，由 failure_kinds 只读抽取，
    # 只给派单拼对症提醒，不参与 executed/PASS/FAIL 判定。
    failure_kinds: dict[str, str] = field(default_factory=dict)


_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_PASSED_COUNT = re.compile(r"(\d+)\s+passed")
_FAILED_COUNT = re.compile(r"(\d+)\s+failed")
_ERROR_LINE = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
_ERROR_COUNT = re.compile(r"(\d+)\s+errors?\b")


def parse_pytest(output: str, returncode: int) -> ParseResult:
    """从 ``pytest -q`` 输出提取失败节点与通过/失败计数。

    executed 的判据：至少解析到一个通过或失败——退出 0 但
    总数为 0 视为没真跑起来（防假绿的第一道保险在调用方）。
    """

    failed_tests: list[str] = []
    seen: set[str] = set()
    for match in _FAILED_LINE.finditer(output):
        node_id = match.group(1)
        if node_id not in seen:
            seen.add(node_id)
            failed_tests.append(node_id)

    passed_matches = _PASSED_COUNT.findall(output)
    failed_matches = _FAILED_COUNT.findall(output)
    passed_count = int(passed_matches[-1]) if passed_matches else 0
    failed_count = (
        int(failed_matches[-1]) if failed_matches else len(failed_tests)
    )
    error_tests: list[str] = []
    seen_errors: set[str] = set()
    for match in _ERROR_LINE.finditer(output):
        node_id = match.group(1)
        if node_id not in seen_errors:
            seen_errors.add(node_id)
            error_tests.append(node_id)
    error_matches = _ERROR_COUNT.findall(output)
    error_count = int(error_matches[-1]) if error_matches else len(error_tests)
    return ParseResult(
        failed_tests=failed_tests,
        passed_count=passed_count,
        failed_count=failed_count,
        executed=(passed_count + failed_count) > 0,
        error_count=error_count,
        error_tests=error_tests,
        failure_kinds=classify_pytest_failures(output, failed_tests),
    )


def parse_exitcode(output: str, returncode: int) -> ParseResult:
    """行为检查：退出码就是裁决，进程跑完即视为已执行。"""

    return ParseResult(executed=True)


_JEST_FAIL_FILE = re.compile(r"^FAIL\s+(\S+)")
_VITEST_FAIL_NODE = re.compile(
    r"^\s*FAIL\s+(?:\S+\s+)?(\S+(?:[./](?:test|spec))\.[tj]sx?)\s+>\s+(.+?)\s*$"
)
_VITEST_FILE = re.compile(
    r"^\s*[❯>]\s+(?:\S+\s+)?(\S+(?:[./](?:test|spec))\.[tj]sx?)\s+\(\d+ tests?"
)
_JEST_FAIL_TEST = re.compile(r"^\s*●\s+(.+?)\s*$")
_VITEST_FAIL_TEST = re.compile(r"^\s*[×✗]\s+(.+?)\s*$")
_JEST_SUMMARY = re.compile(r"^Tests:\s+(.+)$", re.MULTILINE)
_VITEST_SUMMARY = re.compile(r"^\s*Tests\s+(.+)$", re.MULTILINE)
_COUNT_PART = re.compile(r"(\d+)\s+(failed|passed)")


def _family_counts(summary_text: str) -> tuple[int, int]:
    passed = failed = 0
    for num, kind in _COUNT_PART.findall(summary_text):
        if kind == "failed":
            failed = int(num)
        else:
            passed = int(num)
    return passed, failed


def parse_jest_family(output: str, returncode: int) -> ParseResult:
    """jest 与 vitest 默认输出的解析（同一家族两种方言）。

    失败节点 = ``<测试文件>::<用例名>``：文件头在前、用例行
    在后，顺序扫描归属。计数取最后一条 Tests 汇总行。
    executed = 找到汇总行（真跑完才有汇总）。
    """

    failed_tests: list[str] = []
    seen: set[str] = set()
    current_file = ""
    for line in output.splitlines():
        fail_node = _VITEST_FAIL_NODE.match(line)
        if fail_node:
            node = f"{fail_node.group(1)}::{fail_node.group(2)}"
            if node not in seen:
                seen.add(node)
                failed_tests.append(node)
            continue
        header = _JEST_FAIL_FILE.match(line) or _VITEST_FILE.match(
            line
        )
        if header:
            current_file = header.group(1)
            continue
        fail = _JEST_FAIL_TEST.match(line) or _VITEST_FAIL_TEST.match(
            line
        )
        if fail and current_file:
            name = re.sub(
                r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$", "", fail.group(1)
            )
            if name == "Console":
                continue
            node = f"{current_file}::{name}"
            if node not in seen:
                seen.add(node)
                failed_tests.append(node)
    summaries = _JEST_SUMMARY.findall(output) or _VITEST_SUMMARY.findall(
        output
    )
    if summaries:
        passed, failed = _family_counts(summaries[-1])
        executed = True
    else:
        passed, failed = 0, len(failed_tests)
        executed = bool(failed_tests)
    return ParseResult(
        failed_tests=failed_tests,
        passed_count=passed,
        failed_count=failed,
        executed=executed,
    )


_GO_FAIL_TEST = re.compile(r"^\s*--- FAIL:\s+(\S+)")
_GO_PASS_TEST = re.compile(r"^\s*--- PASS:\s+(\S+)")
_GO_PKG_RESULT = re.compile(r"^(ok|FAIL)\s+(\S+)")


def parse_go(output: str, returncode: int) -> ParseResult:
    """``go test`` 输出解析（-v 时有逐用例行，节点最全）。

    失败节点 = ``<包路径>::<Test 名>``：go 按包顺序输出，
    失败行先于该包的结果行，顺序扫描时先攒着、见到包结果
    行再归属。构建失败记一个 ``<包>::(build failed)`` 节点。
    executed = 见到任何逐用例行或包结果行。
    """

    failed_tests: list[str] = []
    seen: set[str] = set()
    pending: list[str] = []
    passed_count = 0
    failed_count = 0
    executed = False

    def flush(pkg: str) -> None:
        for name in pending:
            node = f"{pkg}::{name}"
            if node not in seen:
                seen.add(node)
                failed_tests.append(node)
        pending.clear()

    for line in output.splitlines():
        fail = _GO_FAIL_TEST.match(line)
        if fail:
            executed = True
            failed_count += 1
            pending.append(fail.group(1))
            continue
        if _GO_PASS_TEST.match(line):
            executed = True
            passed_count += 1
            continue
        result = _GO_PKG_RESULT.match(line)
        if result:
            executed = True
            pkg = result.group(2)
            if "[build failed]" in line:
                node = f"{pkg}::(build failed)"
                if node not in seen:
                    seen.add(node)
                    failed_tests.append(node)
            flush(pkg)
    if pending:
        flush("unknown")
    return ParseResult(
        failed_tests=failed_tests,
        passed_count=passed_count,
        failed_count=failed_count,
        executed=executed,
    )


PARSERS = {
    "pytest": parse_pytest,
    "exitcode": parse_exitcode,
    "jest": parse_jest_family,
    "vitest": parse_jest_family,
    "go": parse_go,
}


def get_parser(name: str):
    """按名取解析器；未知名字抛 ValueError（绝不静默回退）。"""

    try:
        return PARSERS[name]
    except KeyError:
        raise ValueError(f"unknown verdict parser: {name!r}") from None
