# -*- coding: utf-8 -*-
"""SHELL1 tests: whitelist shape, engine layering, sandbox execution.

Everything is offline and local: verdicts run against a temp
workspace, executions run pytest/git inside a temp copy under
Seatbelt.  No model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.shell_tool import (
    ShellTool,
    _execute,
    _scrubbed_env,
    command_verdict,
    run_shell,
)

ALLOWED = [
    "pytest -q",
    "pytest --collect-only",
    "python -m pytest tests/test_a.py -q",
    "python3 -m pytest -q",
    "git status",
    "git status --short",
    "git diff",
    "git diff --stat",
    "pytest -q && git diff",
]

REFUSED = [
    "rm -rf build",
    "python -c 'print(1)'",
    "python script.py",
    "pip install requests",
    "curl https://example.com",
    "sed -i s/a/b/ pkg/a.py",
    "cat README.md",
    "git log --oneline",
    "git checkout main",
    "git diff --output=/tmp/x",
    "pytest > out.txt",
    "pytest -q | tee log.txt",
    "pytest $(cat list.txt)",
    "echo hello",
    "ls",
    "",
]


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="shell1-")
        self.workspace = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_whitelist_shapes_allow(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                decision = command_verdict(command, workspace=self.workspace)
                self.assertEqual(decision.verdict, "allow", decision.reason)

    def test_everything_else_refused(self) -> None:
        for command in REFUSED:
            with self.subTest(command=command):
                decision = command_verdict(command, workspace=self.workspace)
                self.assertEqual(decision.verdict, "deny", decision.reason)

    def test_verdict_is_audited_once_with_final_outcome(self) -> None:
        command_verdict("pytest -q", workspace=self.workspace)
        command_verdict("rm -rf build", workspace=self.workspace)
        log = self.workspace / ".drbug" / "audit.log"
        lines = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        shell_lines = [r for r in lines if r["action"] == "shell_exec"]
        self.assertEqual(len(shell_lines), 2)
        by_target = {r["target"]: r["verdict"] for r in shell_lines}
        self.assertEqual(by_target["pytest -q"], "allow")
        self.assertEqual(by_target["rm -rf build"], "deny")


class EnvScrubTests(unittest.TestCase):
    def test_catalog_key_names_are_dropped(self) -> None:
        from core import model_config

        catalog = model_config.load_profile_catalog()
        profiles = getattr(catalog, "profiles", {}) or {}
        values = (
            profiles.values() if isinstance(profiles, dict) else profiles
        )
        names = [
            getattr(p, "api_key_env", None)
            for p in values
        ]
        names = [n for n in names if isinstance(n, str) and n]
        self.assertTrue(names, "catalog must name at least one key env")
        with mock.patch.dict(
            os.environ, {names[0]: "sentinel-value"}, clear=False
        ):
            env = _scrubbed_env()
        self.assertNotIn(names[0], env)


class ExecuteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="shell1x-")
        self.workspace = Path(self._tmp.name) / "copy"
        self.workspace.mkdir()
        (self.workspace / "test_ok.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_shell_pytest_returns_output(self) -> None:
        out = run_shell("pytest -q", workspace=self.workspace)
        self.assertIn("退出码 0", out)
        self.assertIn("1 passed", out)

    def test_run_shell_git_status(self) -> None:
        subprocess.run(
            ["git", "init", "-q"], cwd=self.workspace, check=True
        )
        out = run_shell("git status", workspace=self.workspace)
        self.assertIn("退出码 0", out)

    def test_run_shell_refusal_does_not_execute(self) -> None:
        marker = self.workspace / "should_not_exist.txt"
        out = run_shell(
            f"pytest > {marker.name}", workspace=self.workspace
        )
        self.assertIn("命令被拒绝", out)
        self.assertFalse(marker.exists())

    def test_execute_write_outside_set_blocked_by_os(self) -> None:
        escape = Path(self._tmp.name) / "escape.txt"
        _execute(f"echo hacked > {escape}", workspace=self.workspace)
        self.assertFalse(escape.exists())

    def test_execute_read_ssh_blocked_by_os(self) -> None:
        target = Path.home() / ".ssh" / "id_rsa"
        out = _execute(f"cat {target}", workspace=self.workspace)
        self.assertNotIn("PRIVATE KEY", out)
        self.assertIn("退出码 1", out)

    def test_execute_timeout_kills(self) -> None:
        out = _execute("sleep 10", workspace=self.workspace, timeout_s=1)
        self.assertIn("超时", out)


class ToolGateTests(unittest.TestCase):
    def test_toolkit_contains_shell(self) -> None:
        from core.agentscope_actors import build_toolkit

        with tempfile.TemporaryDirectory(prefix="shell1t-") as tmp:
            toolkit = build_toolkit(Path(tmp), ("pkg/a.py",))
            # Toolkit 无 .tools（工具在 tool_groups[].tools，见
            # agentscope/tool/_toolkit.py 127-134 与
            # tests/test_policy_worker_paths.py WorkerGateStructureTests）。
            # 断言仍是名单里有 Shell。
            names = [
                tool.name
                for group in toolkit.tool_groups
                for tool in group.tools
            ]
        self.assertIn("Shell", names)

    def test_check_permissions_denies_and_records(self) -> None:
        denials: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory(prefix="shell1g-") as tmp:
            tool = ShellTool(
                workspace=Path(tmp),
                actor="worker",
                on_denial=lambda c, r: denials.append((c, r)),
            )
            decision = asyncio.run(
                tool.check_permissions({"command": "rm -rf x"}, None)
            )
        self.assertEqual(decision.behavior.value, "deny")
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0][0], "rm -rf x")


class ToolBaseContractTests(unittest.TestCase):
    """SHELL1-fix2 回归：ToolBase 要求子类自带 is_concurrency_safe
    与 is_read_only；缺失时框架并行分发会 AttributeError 崩回合。
    本类保证工具包里每个工具都带齐这两个属性。"""

    def test_shell_tool_declares_required_attributes(self) -> None:
        self.assertIs(ShellTool.is_concurrency_safe, False)
        self.assertIs(ShellTool.is_read_only, True)

    def test_every_toolkit_tool_declares_required_attributes(
        self,
    ) -> None:
        from core.agentscope_actors import build_toolkit

        with tempfile.TemporaryDirectory(prefix="shellfix-") as tmp:
            toolkit = build_toolkit(Path(tmp))
            tools = []
            for group in toolkit.tool_groups:
                tools.extend(group.tools)
            self.assertTrue(tools)
            for tool in tools:
                self.assertIsInstance(tool.is_concurrency_safe, bool)
                self.assertIsInstance(tool.is_read_only, bool)


if __name__ == "__main__":
    unittest.main()
