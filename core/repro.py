"""GEN4: repro-first path for repositories that ship no tests.

A repository without tests cannot be judged by the frozen acceptor: there is
no judging surface to freeze.  This module closes that gap with a two-stage
hand-off:

1. :func:`propose_repro` — a triage Agent reads the repository with the
   read-only ``Read`` / ``Glob`` tools and writes one candidate pytest file.
   The candidate is written to the *staging* area only
   (``profiles/<name>/repro/test_repro.py``), never to the target repository.
   A **programmatic** gate then copies the repository, drops the candidate
   into ``tests/<final name>`` and runs pytest.  Only a test that fails with an
   ``AssertionError`` counts as a reproduction; a pass, a collection/import
   error, a non-assertion failure or a timeout is rejected and fed back to the
   Agent for another attempt.  No model participates in this judgement.

2. :func:`freeze_repro` — a hard confirmation gate.  ``confirm`` must be the
   literal ``True``; anything else refuses and returns the candidate plus the
   verification output for human review.  The confirmation the product asks
   for is about the *symptom* ("is this the problem you hit?"), presented as a
   three-part comparison: the user's own wording, what actually happened on
   the original code (input -> actual result), and the expected result.  A
   rejected symptom voids the candidate via :func:`reject_repro`.  Once
   confirmed, the verified test is copied into the repository as
   ``tests/test_repro_<slug>.py`` and :func:`core.autoprofile.build_profile`
   builds the vault/profile that the existing ``--profile auto`` repair loop
   consumes.  A freeze receipt (path, sha256, ``origin="repro"``, TAMPERED
   warning) is written next to the staging candidate.  This module then steps
   out of the repair path.

The module is offline-testable: ``model`` can be any ``ChatModelBase``
(``agentscope_ext.fake_model.FakeChatModel`` in tests) and no API key is read
unless ``model`` is omitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from agentscope.agent import Agent
from agentscope.message import Msg, TextBlock
from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
)
from agentscope.state import AgentState
from agentscope.tool import Glob, LocalBackend, Read, Toolkit

from .autoprofile import PROFILES_ROOT, build_profile, default_profile_name

REPRO_READY = "REPRO_READY"
CANNOT_REPRODUCE = "CANNOT_REPRODUCE"

FROZEN = "FROZEN"
REFUSED = "REFUSED"
NO_CANDIDATE = "NO_CANDIDATE"
ERROR = "ERROR"
REJECTED = "REJECTED"

STAGING_TEST_NAME = "test_repro.py"
CANDIDATE_NAME = "candidate.json"
ATTEMPTS_NAME = "attempts.json"
RECEIPT_NAME = "freeze_receipt.json"

PYTEST_TIMEOUT_SECONDS = 300

_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}

_COPY_IGNORE = shutil.ignore_patterns(
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "venv",
)

_SUMMARY_LINE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|RERUN)\s+(.+)$",
    re.MULTILINE,
)
_ASSERTION_LINE = re.compile(r"^E\s+AssertionError", re.MULTILINE)
_ASSERTION_FAIL_LINE = re.compile(
    r"^\s*E\s+AssertionError: assert (.+?)\s*$", re.MULTILINE
)
_WHERE_LINE = re.compile(
    r"^\s*E\s+\+\s+where\s+.*?=\s*(.+?)\s*$", re.MULTILINE
)
_EQ_SPLIT = re.compile(r"\s+==\s+")
_COLLECTION_MARKERS = (
    "ERROR collecting",
    "error during collection",
    "Errors during collection",
    "no tests ran",
)
_FENCE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class ReproResult:
    """Outcome of a triage run.

    ``test_path`` is the staging file (the only place a candidate is allowed
    to live before confirmation).  ``attempts`` keeps every candidate and its
    raw pytest output, including failed attempts on a CANNOT_REPRODUCE run.
    ``symptom`` is the user's own wording; the comparison shown to the user
    is built from it plus the verification run (see :meth:`comparison`).
    """

    status: str
    repo_path: Path
    name: str
    test_path: Path | None = None
    test_source: str = ""
    failing_output: str = ""
    attempts: list[dict] = field(default_factory=list)
    reason: str = ""
    final_test_name: str = ""
    symptom: str = ""

    @property
    def comparison(self) -> "ReproComparison":
        """User-facing symptom comparison (never the test code itself)."""
        return build_comparison(
            self.symptom, self.test_source, self.failing_output
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "repo_path": str(self.repo_path),
            "name": self.name,
            "test_path": str(self.test_path) if self.test_path else None,
            "test_source": self.test_source,
            "failing_output": self.failing_output,
            "attempts": [dict(item) for item in self.attempts],
            "reason": self.reason,
            "final_test_name": self.final_test_name,
            "symptom": self.symptom,
        }


@dataclass
class FreezeResult:
    """Outcome of :func:`freeze_repro`; every refusal carries the evidence."""

    status: str
    repo_path: Path
    name: str
    reason: str = ""
    staging_path: Path | None = None
    test_source: str = ""
    failing_output: str = ""
    repo_test_path: Path | None = None
    profile_dir: Path | None = None
    dispatch_table_path: Path | None = None
    vault_dir: Path | None = None
    symptom: str = ""
    sha256: str = ""
    origin: str = ""
    receipt_path: Path | None = None

    @property
    def profile_path(self) -> Path | None:
        """Alias for callers that call the generated directory a path."""
        return self.profile_dir

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "repo_path": str(self.repo_path),
            "name": self.name,
            "reason": self.reason,
            "staging_path": str(self.staging_path) if self.staging_path else None,
            "test_source": self.test_source,
            "failing_output": self.failing_output,
            "repo_test_path": (
                str(self.repo_test_path) if self.repo_test_path else None
            ),
            "profile_dir": str(self.profile_dir) if self.profile_dir else None,
            "dispatch_table_path": (
                str(self.dispatch_table_path) if self.dispatch_table_path else None
            ),
            "vault_dir": str(self.vault_dir) if self.vault_dir else None,
            "symptom": self.symptom,
            "sha256": self.sha256,
            "origin": self.origin,
            "receipt_path": (
                str(self.receipt_path) if self.receipt_path else None
            ),
        }


@dataclass(frozen=True)
class ReproComparison:
    """What the user is asked to recognise: their symptom, not the test.

    ``symptom`` is the user's own wording; ``observed`` and ``expected`` are
    plain-language restatements derived from the verification run on the
    original code.  ``assertion_line`` / ``input_expr`` keep the raw evidence
    for the technical-detail section.
    """

    symptom: str
    observed: str
    expected: str
    assertion_line: str = ""
    input_expr: str = ""


@dataclass(frozen=True)
class FixVerification:
    """Result of re-running the frozen repro test on a repaired workspace."""

    ok: bool
    kind: str
    reason: str
    output: str = ""
    test_name: str = ""


@dataclass(frozen=True)
class _VerifyOutcome:
    ok: bool
    kind: str
    reason: str
    output: str


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _slug(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", str(value), flags=re.UNICODE).strip(
        "-._"
    )
    return cleaned or "repro"


def final_test_name(name: str) -> str:
    """Repository-relative file name used when a candidate is frozen."""
    return f"test_repro_{_slug(name)}.py"


def _profiles_root(profiles_root: str | Path | None) -> Path:
    if profiles_root is None:
        return Path(PROFILES_ROOT)
    return Path(profiles_root).expanduser()


def _profile_origin(profile_dir: Path) -> str:
    """Read ``origin`` from ``profile.json`` (empty when unavailable)."""
    try:
        metadata = json.loads(
            (Path(profile_dir) / "profile.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return ""
    origin = metadata.get("origin") if isinstance(metadata, dict) else ""
    return origin if isinstance(origin, str) else ""


def staging_dir(
    repo_path: str | Path,
    name: str | None = None,
    *,
    profiles_root: str | Path | None = None,
) -> Path:
    """Return ``profiles/<name>/repro`` for *repo_path*."""
    profile_name = name or default_profile_name(repo_path)
    return _profiles_root(profiles_root) / _slug(profile_name) / "repro"


def _profile_name(repo_path: str | Path, name: str | None) -> str:
    return _slug(name or default_profile_name(repo_path))


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…（前文省略）\n" + text[-limit:]


def _clear_staging(staging: Path) -> None:
    for filename in (
        STAGING_TEST_NAME,
        CANDIDATE_NAME,
        ATTEMPTS_NAME,
        RECEIPT_NAME,
    ):
        (staging / filename).unlink(missing_ok=True)


def _readable_sources(repo: Path) -> list[Path]:
    """Readable non-test ``.py`` files; an empty list means "no source"."""
    found: list[Path] = []
    try:
        candidates = sorted(repo.rglob("*.py"))
    except OSError:
        return found
    for path in candidates:
        try:
            relative = path.relative_to(repo)
        except ValueError:  # pragma: no cover - defensive
            continue
        parts = relative.parts
        if not parts:
            continue
        if any(
            part in _EXCLUDED_DIR_NAMES or part.startswith(".")
            for part in parts
        ):
            continue
        if parts[0] == "tests" or path.name == "conftest.py":
            continue
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found.append(path)
    return found


def _extract_test_source(reply: str) -> str:
    """Pull the single test file out of the triage Agent's reply.

    The prompt asks for exactly one fenced Python block.  Explanations around
    it are tolerated; a reply without a ``def test_`` function yields ``""``.
    """
    text = str(reply or "")
    for match in _FENCE.finditer(text):
        block = match.group(1).strip()
        if "def test_" in block:
            return block + "\n"
    stripped = text.strip()
    if "def test_" in stripped:
        # Raw (unfenced) source is accepted only when it is valid Python; this
        # keeps explanatory prose from being mistaken for a test file.
        try:
            compile(stripped, "<candidate>", "exec")
        except SyntaxError:
            pass
        else:
            return stripped + "\n"
    return ""


def build_comparison(
    symptom: str, test_source: str, failing_output: str
) -> ReproComparison:
    """Build the three-part symptom comparison shown before freezing.

    The user is asked to recognise *their* problem, so the comparison is
    derived from their own wording plus the verification run on the original
    code: input -> actual result, and the expected result the failing
    assertion encodes.  ``test_source`` is deliberately not parsed: it is the
    technical detail, never the confirmation surface.
    """
    del test_source  # reserved: the test is evidence, not the confirmation
    text = str(symptom or "").strip()
    output = str(failing_output or "")

    assertion = ""
    actual = ""
    expected = ""
    matches = _ASSERTION_FAIL_LINE.findall(output)
    if matches:
        assertion = matches[-1].strip()
        parts = _EQ_SPLIT.split(assertion, maxsplit=1)
        if len(parts) == 2:
            actual = parts[0].strip()
            expected = parts[1].strip()

    input_expr = ""
    where_matches = _WHERE_LINE.findall(output)
    if where_matches:
        input_expr = where_matches[-1].strip()

    if input_expr and actual and expected:
        observed = (
            f"输入 {input_expr} → 实际得到 {actual}（而不是期望的 {expected}）"
        )
    elif actual and expected:
        observed = f"原始代码上的实际结果是 {actual}，而不是期望的 {expected}"
    elif assertion:
        observed = f"复现测试在原始代码上断言失败：assert {assertion}"
    else:
        observed = "复现测试在原始代码上以断言失败告终（原始输出见技术细节）。"

    if expected:
        expected_text = f"结果应当是 {expected}"
    else:
        expected_text = "按你上面的描述，代码应当给出你期望的结果"

    return ReproComparison(
        symptom=text,
        observed=observed,
        expected=expected_text,
        assertion_line=assertion,
        input_expr=input_expr,
    )


def comparison_lines(comparison: ReproComparison) -> list[str]:
    """Plain-language three-part comparison shared by CLI and dispatcher."""
    return [
        "-- 症状复现对照（请确认复现的是不是你遇到的问题，而不是确认测试代码） --",
        f"  ① 你原话的症状: {comparison.symptom or '（未提供症状描述）'}",
        f"  ② 原始代码上的实际表现: {comparison.observed}",
        f"  ③ 按你的描述应有的结果: {comparison.expected}",
        "  说明: 复现测试全文只是技术细节，可展开查看，不作为确认依据。",
    ]


def _candidate_payload(
    *,
    status: str,
    repo: Path,
    name: str,
    test_source: str,
    failing_output: str,
    attempts: list[dict],
    reason: str,
    final_name: str,
    symptom: str = "",
) -> dict:
    return {
        "status": status,
        "repo_path": str(repo),
        "name": name,
        "symptom": symptom,
        "test_source": test_source,
        "failing_output": failing_output,
        "attempts": [dict(item) for item in attempts],
        "reason": reason,
        "final_test_name": final_name,
    }


def _write_candidate(staging: Path, payload: dict) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    (staging / CANDIDATE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (staging / ATTEMPTS_NAME).write_text(
        json.dumps(
            payload.get("attempts", []), ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


def load_candidate(
    repo_path: str | Path,
    name: str | None = None,
    *,
    profiles_root: str | Path | None = None,
) -> dict | None:
    """Read the staging candidate metadata (``None`` when absent/invalid)."""
    staging = staging_dir(repo_path, name, profiles_root=profiles_root)
    try:
        data = json.loads(
            (staging / CANDIDATE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_receipt(
    repo_path: str | Path,
    name: str | None = None,
    *,
    profiles_root: str | Path | None = None,
) -> dict | None:
    """Read the on-disk freeze receipt (``None`` when absent/invalid)."""
    staging = staging_dir(repo_path, name, profiles_root=profiles_root)
    try:
        data = json.loads(
            (staging / RECEIPT_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Programmatic verification gate
# ---------------------------------------------------------------------------
def _classify_pytest_run(returncode: int, output: str) -> _VerifyOutcome:
    """Decide whether a pytest run is an honest reproduction.

    Accepted: at least one ``FAILED`` node whose terminal exception is
    ``AssertionError``, and no collection/teardown ``ERROR``.  Everything
    else (pass, collection/import error, non-assertion exception, no tests,
    timeout, unknown failure) is rejected with a reason the triage Agent can
    act on.
    """
    statuses = _SUMMARY_LINE.findall(output)
    failed = [payload for status, payload in statuses if status == "FAILED"]
    errors = [payload for status, payload in statuses if status == "ERROR"]
    has_collection_error = any(
        marker in output for marker in _COLLECTION_MARKERS
    ) or "ERROR collecting" in output

    if has_collection_error or errors or returncode == 2:
        return _VerifyOutcome(
            False,
            "collection_error",
            "收集/导入错误不算复现（必须是一条断言失败的测试）。",
            output,
        )
    if returncode == 0:
        return _VerifyOutcome(
            False,
            "passed",
            "候选测试在原始代码上通过（没有复现症状）。",
            output,
        )
    if returncode == 5:
        return _VerifyOutcome(
            False,
            "no_tests",
            "候选文件没有收集到任何测试。",
            output,
        )
    if returncode not in (1,) or not failed:
        return _VerifyOutcome(
            False,
            "pytest_error",
            f"pytest 未给出可判定的断言失败（exit={returncode}）。",
            output,
        )
    assertion_failure = bool(_ASSERTION_LINE.search(output)) or any(
        "AssertionError" in payload for payload in failed
    )
    if not assertion_failure:
        failing = failed[0].strip() if failed else ""
        return _VerifyOutcome(
            False,
            "not_assertion",
            "测试失败了，但不是 AssertionError（例如被测代码抛了别的异常）；"
            f"首条失败：{failing}",
            output,
        )
    return _VerifyOutcome(
        True,
        "assertion_failed",
        "候选测试在原始代码副本上以 AssertionError 失败（复现成立）。",
        output,
    )


@dataclass(frozen=True)
class _PytestRun:
    """Raw pytest result on a private copy: ``kind`` is ran/timeout/copy_error."""

    kind: str
    returncode: int = 0
    output: str = ""
    reason: str = ""


def _pytest_on_copy(
    repo: Path, test_source: str, final_name: str
) -> _PytestRun:
    """Run *test_source* as ``tests/<final_name>`` on a throwaway copy.

    Shared by the verification gate (which then classifies the failure) and
    the post-repair re-check (which only needs pass/fail).  The target
    repository is never written to.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="repro-verify-") as temporary:
            copy_root = Path(temporary) / "repo"
            shutil.copytree(
                repo,
                copy_root,
                ignore=_COPY_IGNORE,
                symlinks=True,
                ignore_dangling_symlinks=True,
            )
            target = copy_root / "tests" / final_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(test_source, encoding="utf-8")

            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env.pop("PYTEST_ADDOPTS", None)
            env.pop("PYTHONPATH", None)
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "-rA",
                        "-p",
                        "no:cacheprovider",
                        "--tb=long",
                        f"tests/{final_name}",
                    ],
                    cwd=copy_root,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=PYTEST_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return _PytestRun(
                    "timeout",
                    output="pytest timed out",
                    reason=(
                        f"候选测试运行超过 {PYTEST_TIMEOUT_SECONDS}s，已终止。"
                    ),
                )
            output = completed.stdout + completed.stderr
            return _PytestRun(
                "ran", returncode=completed.returncode, output=output
            )
    except OSError as exc:
        return _PytestRun(
            "copy_error",
            reason=f"无法复制仓库做验真：{type(exc).__name__}: {exc}",
        )


def _verify_candidate(
    repo: Path, test_source: str, final_name: str
) -> _VerifyOutcome:
    """Run *test_source* against a private copy of *repo*.

    The copy is thrown away afterwards: the target repository is never
    written to and the staged candidate is only the input to this gate.
    """
    run = _pytest_on_copy(repo, test_source, final_name)
    if run.kind != "ran":
        return _VerifyOutcome(False, run.kind, run.reason, run.output)
    return _classify_pytest_run(run.returncode, run.output)


def verify_fix(
    workspace: str | Path,
    *,
    name: str | None = None,
    expected_sha256: str = "",
) -> FixVerification | None:
    """Re-run the frozen repro test on a repaired workspace copy.

    Uses the same test (therefore the same inputs) that was verified before
    freezing.  Returns ``None`` when the workspace has no frozen repro test;
    ``expected_sha256`` (from the freeze receipt) turns a modified test file
    into a TAMPERED verdict instead of re-running different inputs.
    """
    root = Path(workspace).expanduser()
    if not root.is_dir():
        return None
    profile_name = _profile_name(root, name)
    final_name = final_test_name(profile_name)
    frozen = root / "tests" / final_name
    try:
        test_source = frozen.read_text(encoding="utf-8")
    except OSError:
        return None
    if expected_sha256:
        actual = hashlib.sha256(test_source.encode("utf-8")).hexdigest()
        if actual != expected_sha256:
            return FixVerification(
                False,
                "tampered",
                "冻结的复现测试已被改动（TAMPERED），不能用于复验。",
                "",
                final_name,
            )

    run = _pytest_on_copy(root, test_source, final_name)
    if run.kind != "ran":
        return FixVerification(
            False, run.kind, run.reason, run.output, final_name
        )
    if run.returncode == 0:
        return FixVerification(
            True,
            "passed",
            "同样的输入现在得到期望结果（复现测试通过）。",
            run.output,
            final_name,
        )
    comparison = build_comparison("", test_source, run.output)
    return FixVerification(
        False,
        "failed",
        f"复现测试仍然失败（{comparison.observed}）。",
        run.output,
        final_name,
    )


def repair_comparison_lines(
    repo_path: str | Path,
    repaired_workspace: str | Path | None,
    *,
    name: str | None = None,
    profiles_root: str | Path | None = None,
) -> list[str]:
    """Before/after lines for a repair run on a repro-frozen repository.

    Returns ``[]`` when the repository has no freeze receipt (not a repro
    profile) or the repaired workspace is unknown: callers then print
    nothing extra, so non-repro runs stay byte-for-byte unchanged.
    """
    if repaired_workspace is None:
        return []
    receipt = load_receipt(repo_path, name, profiles_root=profiles_root)
    if receipt is None:
        return []
    before = receipt.get("before") or {}
    before_line = str(before.get("observed") or "").strip()
    if not before_line:
        before_line = "当时在原始代码上复现出了你描述的问题"
    fix = verify_fix(
        repaired_workspace,
        name=name,
        expected_sha256=str(receipt.get("sha256") or ""),
    )
    if fix is None:
        return []
    if fix.ok:
        after_line = "用同一组输入再跑同一条复现测试：通过（现在得到期望结果）。"
    else:
        after_line = f"用同一组输入再跑同一条复现测试：仍然失败（{fix.reason}）"
    return [
        "-- 修复后复验对照（与生成复现时同一组输入） --",
        f"  修复前: {before_line}",
        f"  修复后: {after_line}",
        "  提示: 平台复验只覆盖这一条复现；最终以你自己的实际使用为准。",
    ]


# ---------------------------------------------------------------------------
# Triage Agent (read-only)
# ---------------------------------------------------------------------------
TRIAGE_SYSTEM_PROMPT = """\
你是 Doctor Bug 的「复现测试分诊员」。目标是：根据用户给出的症状，写一条 pytest
测试，让它在当前仓库的原始代码上稳定地以断言失败（AssertionError）告终。

你只有只读工具：
- Glob：查看仓库结构、寻找文件（默认从仓库根目录开始）。
- Read：读取源码（必须用仓库内的绝对路径）。
你没有写入工具，也绝对不要修改仓库；不要读取环境变量、密钥或 .env 文件。

写测试的规则：
1. 先用 Glob/Read 了解仓库结构与相关源码，找到症状对应的函数/类。
2. 只写一个测试文件，文件内只包含一个测试函数（可含多个断言），只针对
   该症状，不要顺手测试无关行为，也不要创建额外文件。
3. 测试文件会被放到仓库副本的 tests/<文件名>，pytest 的工作目录是仓库
   根目录；请用普通 import（例如 from pkg.module import func）。
4. 必须断言「正确行为」：在带 bug 的原始代码上应因断言不成立而失败。
   不要用 pytest.raises 把当前异常当作复现；不要写 assert False 之类的
   无关失败，也不要依赖网络、随机数、真实时间或外部服务。
5. 测试必须自包含、确定性、可重复运行；只导入标准库、pytest 和仓库自身
   的模块。
6. 最终回复只包含一个 ```python 代码块（完整测试文件内容），不要输出
   代码块之外的任何解释。
"""


class _RepoBackend(LocalBackend):
    """Local backend whose default cwd is the repository under triage."""

    def __init__(self, repo: Path) -> None:
        self._repo = str(repo)

    async def getcwd(self) -> str:
        return self._repo


def _inside(root: Path, raw: str) -> bool:
    try:
        Path(raw).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


class _ScopedRead(Read):
    """``Read`` restricted to files inside the repository."""

    def __init__(self, root: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[call-arg]
        self._root = Path(root).resolve()

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        raw = str(tool_input.get("file_path", "")).strip()
        if not raw or not _inside(self._root, raw):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="Read is restricted to the repository under triage",
                decision_reason="triage reads must stay inside the repository",
            )
        return await super().check_permissions(tool_input, context)


class _ScopedGlob(Glob):
    """``Glob`` restricted to the repository under triage."""

    def __init__(self, root: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[call-arg]
        self._root = Path(root).resolve()

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        pattern = str(tool_input.get("pattern", ""))
        raw = str(tool_input.get("path") or "").strip()
        unsafe_pattern = pattern.startswith("/") or ".." in PurePosixPath(
            pattern
        ).parts
        if unsafe_pattern or (raw and not _inside(self._root, raw)):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="Glob is restricted to the repository under triage",
                decision_reason="triage searches must stay inside the repository",
            )
        return await super().check_permissions(tool_input, context)


def _build_triage_agent(repo: Path, model: object) -> Agent:
    backend = _RepoBackend(repo)
    toolkit = Toolkit(
        tools=[
            _ScopedRead(repo, backend=backend),
            _ScopedGlob(repo, backend=backend),
        ]
    )
    root = str(repo)
    state = AgentState(
        permission_context=PermissionContext(
            mode=PermissionMode.DONT_ASK,
            working_directories={
                root: AdditionalWorkingDirectory(
                    path=root,
                    source="repro-triage",
                )
            },
        )
    )
    return Agent(
        name="复现测试分诊员",
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        model=model,
        toolkit=toolkit,
        state=state,
    )


def _task_message(repo: Path, symptom: str, final_name: str) -> Msg:
    text = "\n".join(
        [
            f"仓库根目录（绝对路径）：{repo}",
            f"候选测试最终文件名：tests/{final_name}",
            "",
            "症状：",
            symptom,
            "",
            "请先用 Glob/Read 定位相关源码，再写出复现该症状的测试。",
            "最终回复只给一个 ```python 代码块。",
        ]
    )
    return Msg(
        name="dispatcher",
        role="user",
        content=[TextBlock(text=text)],
    )


def _feedback_message(
    attempt: int,
    outcome_kind: str,
    reason: str,
    test_source: str,
    output: str,
) -> Msg:
    lines = [
        f"第 {attempt} 次候选未通过程序验真（判定：{outcome_kind}）。",
        reason,
        "",
    ]
    if test_source:
        lines.extend(
            [
                "上一次候选：",
                "```python",
                test_source.rstrip(),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "验真原始输出（节选）：",
            _tail(output, 6000),
            "",
            "请根据输出重写测试，使它仍然只测该症状，但在原始代码上以",
            "AssertionError 失败。最终回复只给一个 ```python 代码块。",
        ]
    )
    return Msg(
        name="dispatcher",
        role="user",
        content=[TextBlock(text="\n".join(lines))],
    )


async def _run_triage(
    *,
    repo: Path,
    symptom: str,
    model: object,
    max_attempts: int,
    staging: Path,
    final_name: str,
) -> dict:
    """Drive the triage Agent and the programmatic gate to a verdict."""
    agent = _build_triage_agent(repo, model)
    message = _task_message(repo, symptom, final_name)
    attempts: list[dict] = []

    for attempt in range(1, max_attempts + 1):
        try:
            reply = await agent.reply(message)
            reply_text = reply.get_text_content() or ""
        except Exception as exc:  # noqa: BLE001 - report, try the next attempt
            reason = f"分诊模型调用失败：{type(exc).__name__}: {exc}"
            attempts.append(
                {
                    "attempt": attempt,
                    "test_source": "",
                    "output": reason,
                    "verdict": "model_error",
                    "reason": reason,
                }
            )
            message = _feedback_message(
                attempt, "model_error", reason, "", reason
            )
            continue

        test_source = _extract_test_source(reply_text)
        if not test_source:
            reason = "模型回复中没有可用的 “def test_...” 代码块。"
            attempts.append(
                {
                    "attempt": attempt,
                    "test_source": "",
                    "output": reply_text,
                    "verdict": "no_test_code",
                    "reason": reason,
                }
            )
            message = _feedback_message(
                attempt, "no_test_code", reason, "", reply_text
            )
            continue

        staging.mkdir(parents=True, exist_ok=True)
        (staging / STAGING_TEST_NAME).write_text(
            test_source, encoding="utf-8"
        )
        outcome = _verify_candidate(repo, test_source, final_name)
        attempts.append(
            {
                "attempt": attempt,
                "test_source": test_source,
                "output": outcome.output,
                "verdict": outcome.kind,
                "reason": outcome.reason,
            }
        )
        if outcome.ok:
            return {
                "status": REPRO_READY,
                "test_source": test_source,
                "failing_output": outcome.output,
                "attempts": attempts,
                "reason": outcome.reason,
            }
        message = _feedback_message(
            attempt,
            outcome.kind,
            outcome.reason,
            test_source,
            outcome.output,
        )

    last = attempts[-1] if attempts else {}
    last_with_source = next(
        (item for item in reversed(attempts) if item.get("test_source")), {}
    )
    return {
        "status": CANNOT_REPRODUCE,
        "test_source": last_with_source.get("test_source", ""),
        "failing_output": (
            last_with_source.get("output") or last.get("output", "")
        ),
        "attempts": attempts,
        "reason": (
            f"{max_attempts} 次尝试均未得到“在原始代码上断言失败”的候选；"
            "全部尝试记录已保留。"
        ),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def propose_repro(
    repo_path: str | Path,
    symptom: str,
    *,
    model: object = None,
    max_attempts: int = 2,
    name: str | None = None,
    profiles_root: str | Path | None = None,
) -> ReproResult:
    """Write and programmatically verify one candidate repro test.

    Args:
        repo_path: repository under triage (never written to).
        symptom: human description of the bug to reproduce.
        model: injected ``ChatModelBase`` for offline tests; ``None`` builds
            the platform model through :func:`core.model_config.build_model`.
        max_attempts: maximum number of Agent rewrites (>= 1).
        name: optional profile name (staging ``profiles/<name>/repro``);
            defaults to the repository directory name, matching
            :func:`freeze_repro` and :func:`core.autoprofile.build_profile`.
        profiles_root: optional profiles directory override (defaults to
            :data:`core.autoprofile.PROFILES_ROOT`); facades pass their own
            module constant so tests can isolate it.

    Returns:
        :class:`ReproResult` with ``status`` either ``REPRO_READY`` or
        ``CANNOT_REPRODUCE``.  Every attempt (test source plus raw pytest
        output) is kept in ``attempts`` and mirrored to
        ``profiles/<name>/repro/attempts.json``.  The candidate file itself
        only ever lives in the staging directory.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be an integer >= 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be an integer >= 1")

    name = _profile_name(repo, name)
    final_name = final_test_name(name)
    staging = staging_dir(repo, name, profiles_root=profiles_root)
    staging.mkdir(parents=True, exist_ok=True)
    _clear_staging(staging)

    cleaned_symptom = str(symptom or "").strip()
    if not cleaned_symptom:
        reason = "症状为空：没有可复现的目标，未调用模型。"
        payload = _candidate_payload(
            status=CANNOT_REPRODUCE,
            repo=repo,
            name=name,
            test_source="",
            failing_output="",
            attempts=[],
            reason=reason,
            final_name=final_name,
            symptom="",
        )
        _write_candidate(staging, payload)
        return ReproResult(
            status=CANNOT_REPRODUCE,
            repo_path=repo,
            name=name,
            test_path=None,
            attempts=[],
            reason=reason,
            final_test_name=final_name,
            symptom="",
        )

    sources = _readable_sources(repo)
    if not sources:
        reason = "仓库没有可读源码：未找到 tests/ 之外可读的 .py 文件，未调用模型。"
        payload = _candidate_payload(
            status=CANNOT_REPRODUCE,
            repo=repo,
            name=name,
            test_source="",
            failing_output="",
            attempts=[],
            reason=reason,
            final_name=final_name,
            symptom=cleaned_symptom,
        )
        _write_candidate(staging, payload)
        return ReproResult(
            status=CANNOT_REPRODUCE,
            repo_path=repo,
            name=name,
            test_path=None,
            attempts=[],
            reason=reason,
            final_test_name=final_name,
            symptom=cleaned_symptom,
        )

    active_model = model
    if active_model is None:
        from .model_config import build_model

        active_model = build_model()

    outcome = asyncio.run(
        _run_triage(
            repo=repo,
            symptom=cleaned_symptom,
            model=active_model,
            max_attempts=max_attempts,
            staging=staging,
            final_name=final_name,
        )
    )
    payload = _candidate_payload(
        status=outcome["status"],
        repo=repo,
        name=name,
        test_source=outcome["test_source"],
        failing_output=outcome["failing_output"],
        attempts=outcome["attempts"],
        reason=outcome["reason"],
        final_name=final_name,
        symptom=cleaned_symptom,
    )
    _write_candidate(staging, payload)
    return ReproResult(
        status=outcome["status"],
        repo_path=repo,
        name=name,
        test_path=(
            staging / STAGING_TEST_NAME
            if outcome["test_source"]
            else None
        ),
        test_source=outcome["test_source"],
        failing_output=outcome["failing_output"],
        attempts=list(outcome["attempts"]),
        reason=outcome["reason"],
        final_test_name=final_name,
        symptom=cleaned_symptom,
    )


def _refusal(
    *,
    status: str,
    repo: Path,
    name: str,
    reason: str,
    staging: Path,
    test_source: str,
    failing_output: str,
) -> FreezeResult:
    return FreezeResult(
        status=status,
        repo_path=repo,
        name=name,
        reason=reason,
        staging_path=staging / STAGING_TEST_NAME,
        test_source=test_source,
        failing_output=failing_output,
    )


def freeze_repro(
    repo_path: str | Path,
    *,
    name: str | None = None,
    confirm: bool = False,
    profiles_root: str | Path | None = None,
) -> FreezeResult:
    """Freeze a verified staging candidate into the repository and profile.

    This is the hard confirmation gate: only the literal ``True`` proceeds.
    Any other value refuses and returns the candidate source plus the cached
    verification output so a human can review it.

    When confirmed, the candidate is written to
    ``<repo>/tests/test_repro_<slug>.py`` and
    :func:`core.autoprofile.build_profile` builds
    ``profiles/<name>/`` (vault + dispatch table).  The profile is marked
    ``origin="repro"``: this path has only the single repro test as a safety
    net, so :mod:`core.runner` turns on the extra write-scope guard for the
    native actor backend.  Subsequent repair runs use the existing
    ``--profile auto`` path; this module is not involved again.

    ``profiles_root`` overrides the profiles directory (facades pass their own
    module constant so tests can isolate it); it defaults to
    :data:`core.autoprofile.PROFILES_ROOT`.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")

    profile_name = _profile_name(repo, name)
    final_name = final_test_name(profile_name)
    staging = staging_dir(repo, profile_name, profiles_root=profiles_root)
    candidate = load_candidate(
        repo, profile_name, profiles_root=profiles_root
    )

    if candidate is None:
        return _refusal(
            status=NO_CANDIDATE,
            repo=repo,
            name=profile_name,
            reason=(
                "暂存区没有候选记录；请先运行 propose_repro 并取得 "
                "REPRO_READY 候选。"
            ),
            staging=staging,
            test_source="",
            failing_output="",
        )

    test_source = str(candidate.get("test_source", ""))
    failing_output = str(candidate.get("failing_output", ""))
    recorded_repo = candidate.get("repo_path", "")
    try:
        same_repo = Path(str(recorded_repo)).resolve() == repo
    except (OSError, RuntimeError):
        same_repo = False

    if candidate.get("status") != REPRO_READY or not test_source:
        return _refusal(
            status=NO_CANDIDATE,
            repo=repo,
            name=profile_name,
            reason=(
                "暂存候选项状态为 "
                f"{candidate.get('status')!r}，不是 REPRO_READY；"
                "请先重新分诊并通过程序验真。"
            ),
            staging=staging,
            test_source=test_source,
            failing_output=failing_output,
        )
    if not same_repo:
        return _refusal(
            status=NO_CANDIDATE,
            repo=repo,
            name=profile_name,
            reason=(
                f"暂存候选属于另一个仓库（{recorded_repo}），拒绝跨仓库冻结。"
            ),
            staging=staging,
            test_source=test_source,
            failing_output=failing_output,
        )
    if confirm is not True:
        return _refusal(
            status=REFUSED,
            repo=repo,
            name=profile_name,
            reason=(
                "冻结会写入仓库 tests/ 并生成验收金库，必须先向用户展示"
                "「症状复现对照」（你遇到的问题 → 原始代码上的实际表现 → "
                "期望结果），取得用户对症状的明确同意；"
                "确认后以 confirm=True 重试。"
            ),
            staging=staging,
            test_source=test_source,
            failing_output=failing_output,
        )

    symptom = str(candidate.get("symptom", ""))
    comparison = build_comparison(symptom, test_source, failing_output)

    destination = repo / "tests" / final_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.read_bytes() if destination.is_file() else None
    destination.write_text(test_source, encoding="utf-8")

    try:
        profile = build_profile(
            repo,
            out_root=_profiles_root(profiles_root),
            name=profile_name,
            origin="repro",
        )
    except Exception as exc:  # noqa: BLE001 - roll back the copied test file
        if backup is None:
            destination.unlink(missing_ok=True)
        else:
            destination.write_bytes(backup)
        return FreezeResult(
            status=ERROR,
            repo_path=repo,
            name=profile_name,
            reason=(
                f"候选已验真，但自动建档失败：{type(exc).__name__}: {exc}"
                "（已回滚仓库中的测试文件）。"
            ),
            staging_path=staging / STAGING_TEST_NAME,
            test_source=test_source,
            failing_output=failing_output,
            symptom=symptom,
        )

    origin = _profile_origin(profile.profile_dir)
    sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt = {
        "status": FROZEN,
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "repo_path": str(repo),
        "name": profile_name,
        "symptom": symptom,
        "before": {
            "observed": comparison.observed,
            "expected": comparison.expected,
            "assertion_line": comparison.assertion_line,
            "input_expr": comparison.input_expr,
        },
        "final_test_name": final_name,
        "repo_test_path": str(destination),
        "sha256": sha256,
        "origin": origin,
        "profile_dir": str(profile.profile_dir),
        "dispatch_table_path": str(profile.dispatch_table_path),
        "vault_dir": str(profile.vault_dir),
        "note": "此后改动该文件即判 TAMPERED。",
    }
    receipt_path = staging / RECEIPT_NAME
    receipt_warning = ""
    try:
        staging.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - disk failure is reportable
        receipt_warning = (
            f"（回执落盘失败：{type(exc).__name__}: {exc}）"
        )

    return FreezeResult(
        status=FROZEN,
        repo_path=repo,
        name=profile_name,
        reason=(
            "候选已冻结进仓库 tests/ 并完成自动建档；后续修复使用 "
            "--profile auto。" + receipt_warning
        ),
        staging_path=staging / STAGING_TEST_NAME,
        test_source=test_source,
        failing_output=failing_output,
        repo_test_path=destination,
        profile_dir=profile.profile_dir,
        dispatch_table_path=profile.dispatch_table_path,
        vault_dir=profile.vault_dir,
        symptom=symptom,
        sha256=sha256,
        origin=origin,
        receipt_path=receipt_path if not receipt_warning else None,
    )


def reject_repro(
    repo_path: str | Path,
    *,
    name: str | None = None,
    reason: str = "",
    profiles_root: str | Path | None = None,
) -> ReproResult:
    """Void the staging candidate after the user says "这不是我的问题".

    The candidate record is kept for audit (status ``REJECTED`` with the
    rejection reason) but can never be frozen: :func:`freeze_repro` only
    accepts ``REPRO_READY``.  Nothing is written to the target repository.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")
    profile_name = _profile_name(repo, name)
    staging = staging_dir(repo, profile_name, profiles_root=profiles_root)
    candidate = load_candidate(
        repo, profile_name, profiles_root=profiles_root
    )
    rejection = str(reason or "").strip() or "用户表示这不是其遇到的问题。"
    if candidate is None:
        return ReproResult(
            status=NO_CANDIDATE,
            repo_path=repo,
            name=profile_name,
            reason="暂存区没有候选记录，无需作废。",
            final_test_name=final_test_name(profile_name),
        )

    candidate["status"] = REJECTED
    candidate["rejection_reason"] = rejection
    candidate["rejected_at"] = datetime.now().isoformat(timespec="seconds")
    _write_candidate(staging, candidate)
    return ReproResult(
        status=REJECTED,
        repo_path=repo,
        name=profile_name,
        test_path=None,
        test_source=str(candidate.get("test_source", "")),
        failing_output=str(candidate.get("failing_output", "")),
        attempts=list(candidate.get("attempts") or []),
        reason=(
            f"候选已作废（{rejection}）；未冻结任何文件。"
            "如症状理解有偏差，请补充描述后重新分诊。"
        ),
        final_test_name=final_test_name(profile_name),
        symptom=str(candidate.get("symptom", "")),
    )


__all__ = [
    "CANNOT_REPRODUCE",
    "CANDIDATE_NAME",
    "ERROR",
    "FROZEN",
    "FixVerification",
    "FreezeResult",
    "NO_CANDIDATE",
    "RECEIPT_NAME",
    "REFUSED",
    "REJECTED",
    "REPRO_READY",
    "ReproComparison",
    "ReproResult",
    "STAGING_TEST_NAME",
    "build_comparison",
    "comparison_lines",
    "final_test_name",
    "freeze_repro",
    "load_candidate",
    "load_receipt",
    "propose_repro",
    "reject_repro",
    "repair_comparison_lines",
    "staging_dir",
    "verify_fix",
]

@dataclass
class BehaviorFreezeResult:
    """``freeze_behavior_check`` 的结果（秘书行为检查冻结）。"""

    status: str
    reason: str = ""
    profile_dir: Path | None = None
    vault_dir: Path | None = None
    script: str = ""
    sha256: str = ""


def freeze_behavior_check(
    repo_path: str | Path,
    script: str,
    *,
    name: str | None = None,
    profiles_root: str | Path | None = None,
) -> BehaviorFreezeResult:
    """把仓库里一个现成脚本冻结成行为检查 profile。

    脚本进金库受哈希保护；此后 ``accept_check`` 对该仓库的判卷
    就是跑这个脚本（退出 0 = PASS）。已存在同名 profile 时拒绝，
    不覆盖。脚本不得是密钥/凭据文件，不得越出仓库。
    """
    from acceptor.frozen import freeze_behavior

    from . import policy as _policy

    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")
    script_rel = Path(script)
    if script_rel.is_absolute() or ".." in script_rel.parts:
        return BehaviorFreezeResult(
            status="REFUSED",
            reason=f"脚本路径必须是仓库内相对路径：{script}",
            script=str(script),
        )
    script_path = (repo / script_rel).resolve()
    try:
        script_path.relative_to(repo)
    except ValueError:
        return BehaviorFreezeResult(
            status="REFUSED",
            reason=f"脚本不在仓库内：{script}",
            script=str(script),
        )
    if not script_path.is_file():
        return BehaviorFreezeResult(
            status="REFUSED",
            reason=f"脚本不存在：{script}",
            script=str(script),
        )
    if _policy.is_secret_path(script_path):
        return BehaviorFreezeResult(
            status="REFUSED",
            reason=f"脚本路径像密钥/凭据文件，拒绝冻结：{script}",
            script=str(script),
        )
    profile_name = _profile_name(repo, name)
    root = (
        Path(profiles_root)
        if profiles_root is not None
        else Path(PROFILES_ROOT)
    )
    profile_dir = root / profile_name
    if profile_dir.exists():
        return BehaviorFreezeResult(
            status="REFUSED",
            reason=(
                f"已有同名 profile（{profile_dir}），拒绝覆盖；"
                "先改名或删掉旧的再冻结。"
            ),
            script=script_rel.as_posix(),
        )
    parent = script_rel.parent.as_posix()
    package = parent if parent not in ("", ".") else "backoffice"
    vault_dir = profile_dir / "vault"
    manifest = freeze_behavior(repo, vault_dir, script_rel, package=package)
    digest = next(iter(manifest.get("files", {}).values()), "")
    (profile_dir / "profile.json").write_text(
        json.dumps(
            {
                "name": profile_name,
                "origin": "behavior",
                "behavior_script": script_rel.as_posix(),
                "packages": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (profile_dir / "dispatch_table.json").write_text(
        "{}\n", encoding="utf-8"
    )
    return BehaviorFreezeResult(
        status="FROZEN",
        profile_dir=profile_dir,
        vault_dir=vault_dir,
        script=script_rel.as_posix(),
        sha256=digest,
    )

