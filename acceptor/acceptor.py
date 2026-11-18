"""Frozen acceptor: verify the workspace, then judge with vaulted tests."""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .evidence import Evidence, evidence_from_failed
from .frozen import (
    DEFAULT_CHECK_COMMAND,
    DEFAULT_PACKAGE,
    MANIFEST_NAME,
    normalise_package,
    verify_against_manifest,
)
from .parsers import ParseResult, get_parser, parse_pytest

_PYTEST_TIMEOUT_SECONDS = 300


@dataclass
class Verdict:
    status: str
    failed_tests: list[str] = field(default_factory=list)
    tampered_files: list[str] = field(default_factory=list)
    passed_count: int = 0
    failed_count: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    detail: str = ""
    # EVD1：纯增量，只透传解析器的 error 数与 error 节点 id，供成绩单
    # 分类；不参与 status 判定，也不进 evidence。to_dict 只在非零/非空时
    # 带出，旧输出逐字节不变。
    error_count: int = 0
    error_tests: list[str] = field(default_factory=list)
    # VAR1：纯增量，只透传解析器的失败分类 {节点 id: 类别}，供派单拼
    # 对症提醒与运行记录；不参与 status 判定、不进 evidence、不进
    # to_dict（判卷输出逐字节不变）。
    failure_kinds: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "failed_tests": list(self.failed_tests),
            "tampered_files": list(self.tampered_files),
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "evidence": [
                {"node_id": item.node_id, "module": item.module}
                for item in self.evidence
            ],
            "detail": self.detail,
            **(
                {
                    "error_count": self.error_count,
                    "error_tests": list(self.error_tests),
                }
                if self.error_count or self.error_tests
                else {}
            ),
        }


def parse_pytest_output(output: str) -> tuple[list[str], int, int]:
    """兼容旧签名的薄封装；解析实现在 acceptor/parsers.py。"""

    result = parse_pytest(output, 0)
    return result.failed_tests, result.passed_count, result.failed_count


class FrozenAcceptor:
    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        manifest_path = self.vault_dir / MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"vault manifest not found: {manifest_path}")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(self.manifest.get("files"), dict):
            raise ValueError(f"invalid vault manifest: {manifest_path}")
        self.package = normalise_package(
            self.manifest.get("package", DEFAULT_PACKAGE)
        )
        command = self.manifest.get("check_command")
        self.check_command = (
            list(command) if command else list(DEFAULT_CHECK_COMMAND)
        )
        self.parser_name = str(self.manifest.get("parser") or "pytest")
        self.parser = get_parser(self.parser_name)

        vault_mismatch = verify_against_manifest(
            self.manifest, self.vault_dir, strict_hidden=True
        )
        if vault_mismatch:
            raise RuntimeError(
                "vault does not match its manifest: "
                + ", ".join(vault_mismatch)
            )

    def _prepare_run_dir(self, workspace: Path, run_dir: Path) -> None:
        # 整个工作区都要进运行目录：测试常会引用包目录之外的
        # 辅助文件（仓库根的 conftest/工具模块、JS 的仓库根
        # test/ 辅助与配置的相对引用）。只拷包目录会让这类
        # 测试在运行目录里根本跑不起来、把裁决拖成 FAIL
        # （E2E 发现项 F4）。判卷文件仍随后由 vault 覆盖，
        # 运行目录里的判卷面与冻结版本逐字一致。
        if workspace.is_dir():
            shutil.copytree(
                workspace,
                run_dir,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git", ".drbug"
                ),
                dirs_exist_ok=True,
                symlinks=True,
            )

        overlay = dict(self.manifest["files"])
        overlay.update(self.manifest.get("hidden_files", {}))
        for relative in sorted(overlay):
            source = self.vault_dir / relative
            target = run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _run_check(self, workspace: Path) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="acceptor-run-") as temporary:
            run_dir = Path(temporary)
            self._prepare_run_dir(workspace, run_dir)

            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env.pop("PYTEST_ADDOPTS", None)
            env.pop("PYTHONPATH", None)

            argv = [
                sys.executable if token == "{python}" else token
                for token in self.check_command
            ]
            try:
                completed = subprocess.run(
                    argv,
                    cwd=run_dir,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    env=env,
                    timeout=_PYTEST_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return 1, "check command timed out"
            except OSError as exc:
                return 1, f"check command failed to start: {exc}"
            output = completed.stdout + completed.stderr
            # 工具链（如 vitest 5）在管道里也吐 ANSI 颜色码，
            # 不剥掉会把计数/失败行的正则全打碎（E2E 案例 3）。
            output = re.sub(r"\x1b\[[0-9;:]*[A-Za-z]", "", output)
            return completed.returncode, output

    def check(self, workspace: Path) -> Verdict:
        workspace = Path(workspace)
        if self.manifest.get("kind") == "behavior":
            # 行为金库的冻结文件不在工作区里（只在金库中，
            # 构造时已验完整性），工作区篡改核对不适用。
            tampered_files: list[str] = []
        else:
            tampered_files = verify_against_manifest(
                self.manifest, workspace
            )
        if tampered_files:
            return Verdict(status="TAMPERED", tampered_files=tampered_files)

        returncode, output = self._run_check(workspace)
        result: ParseResult = self.parser(output, returncode)
        if not result.executed:
            return Verdict(
                status="FAIL",
                detail=(
                    "判卷未真正执行：检查命令跑完但没有解析到"
                    "任何已执行的检查（疑似空跑或输出异常）"
                ),
            )
        status = (
            "PASS"
            if (
                returncode == 0
                and result.failed_count == 0
                and not result.failed_tests
            )
            else "FAIL"
        )
        return Verdict(
            status=status,
            failed_tests=result.failed_tests,
            passed_count=result.passed_count,
            failed_count=result.failed_count,
            evidence=evidence_from_failed(result.failed_tests),
            error_count=result.error_count,
            error_tests=list(result.error_tests),
            failure_kinds=dict(result.failure_kinds),
        )
