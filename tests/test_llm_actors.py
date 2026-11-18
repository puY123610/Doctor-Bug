"""Offline tests for the real-agent actor backend and runner pluggability.

No real ``opencode`` binary or model call is made here: every test that runs
``llm_act`` points ``opencode_path`` at a temporary fake shell script.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.actors import FixResult
from core.llm_actors import (
    OPENCODE_BIN,
    build_prompt,
    llm_act,
    snapshot_python,
)
from core.runner import RunRecord, run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_VAULT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "acceptor_vault"
DEMO_KB = (
    Path(__file__).resolve().parents[1] / "kb" / "dispatch_table.json"
)
IGNORE_CACHES = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

ASSIGNED = [
    "tests/test_billing.py::test_tax_on_discounted_total",
]
FAILED = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
]


class PromptTest(unittest.TestCase):
    def test_specialist_prompt_has_role_domain_nodes_and_symptoms(self):
        prompt = build_prompt("billing-specialist", ASSIGNED, FAILED)

        self.assertIn("billing-specialist", prompt)
        self.assertIn("只负责 backoffice/billing.py", prompt)
        self.assertIn("只允许修改 backoffice/billing.py", prompt)
        for node in ASSIGNED:
            self.assertIn(node, prompt)
        for symptom in (
            "用券订单金额不对",
            "库存下降过快",
            "支持账号可以退款",
            "finance",
        ):
            self.assertIn(symptom, prompt)
        self.assertIn("绝不修改 tests/、conftest.py、pytest.ini", prompt)
        self.assertIn("最小改动", prompt)
        self.assertIn("根因不在自己的领域文件", prompt)
        self.assertNotIn("backoffice/inventory.py", prompt)
        self.assertNotIn("backoffice/auth.py", prompt)

    def test_generalist_prompt_allows_three_domains_and_differs(self):
        specialist = build_prompt("billing-specialist", ASSIGNED, FAILED)
        generalist = build_prompt("generalist", ASSIGNED, FAILED)

        for allowed in (
            "backoffice/billing.py",
            "backoffice/inventory.py",
            "backoffice/auth.py",
        ):
            self.assertIn(allowed, generalist)
        self.assertIn(
            "只允许修改 backoffice/billing.py、backoffice/inventory.py、"
            "backoffice/auth.py 这三个文件",
            generalist,
        )
        self.assertIn("只允许修改 backoffice/billing.py；", specialist)
        self.assertNotEqual(specialist, generalist)
        self.assertIn("generalist", generalist)
        for node in ASSIGNED:
            self.assertIn(node, generalist)

    def test_db_specialist_prompt_is_restricted_to_db(self):
        prompt = build_prompt("db-specialist", ASSIGNED, FAILED)

        self.assertIn("db-specialist", prompt)
        self.assertIn("只负责 backoffice/db.py", prompt)
        self.assertIn("只允许修改 backoffice/db.py", prompt)
        self.assertNotIn("backoffice/billing.py", prompt)

    def test_unknown_actor_is_rejected(self):
        with self.assertRaises(KeyError):
            build_prompt("nobody", ASSIGNED, FAILED)


class LlmActTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wire2-llm-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = self.tmp / "workspace"
        backoffice = self.workspace / "backoffice"
        backoffice.mkdir(parents=True)
        for name in ("billing", "inventory", "auth", "db"):
            (backoffice / f"{name}.py").write_text(
                f'NAME = "{name}"\n', encoding="utf-8"
            )

    def _fake_opencode(self, body: str) -> Path:
        """Create a fake executable that captures its last arg (the prompt)."""
        path = self.tmp / "fake-opencode"
        path.write_text(
            "#!/bin/sh\n"
            "set -e\n"
            "for last do :; done\n"
            "printf '%s' \"$last\" > \"$PWD/prompt-capture.txt\"\n" + body,
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _captured_prompt(self) -> str:
        return (self.workspace / "prompt-capture.txt").read_text(encoding="utf-8")

    def test_real_opencode_path_remains_the_default(self):
        self.assertEqual(OPENCODE_BIN, "/opt/homebrew/bin/opencode")
        parameters = inspect.signature(llm_act).parameters
        self.assertEqual(parameters["opencode_path"].default, OPENCODE_BIN)
        self.assertEqual(parameters["timeout_s"].default, 600)

    def test_snapshot_covers_only_backoffice_python_files(self):
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
        (self.workspace / "backoffice" / "notes.txt").write_text(
            "not python\n", encoding="utf-8"
        )

        snapshot = snapshot_python(self.workspace)

        self.assertEqual(
            set(snapshot),
            {f"backoffice/{name}.py" for name in ("auth", "billing", "db", "inventory")},
        )

    def test_file_change_is_detected_as_applied(self):
        fake = self._fake_opencode(
            'echo "fake applied billing fix"\n'
            'echo "FIXED = True" > backoffice/billing.py\n'
        )

        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            opencode_path=fake,
        )

        self.assertIsInstance(result, FixResult)
        self.assertEqual(result.actor_id, "billing-specialist")
        self.assertTrue(result.applied)
        self.assertIn("exit=0", result.note)
        self.assertIn("elapsed=", result.note)
        self.assertIn("backoffice/billing.py", result.note)
        self.assertIn("fake applied billing fix", result.note)
        prompt = self._captured_prompt()
        self.assertIn("billing-specialist", prompt)
        self.assertIn(ASSIGNED[0], prompt)

    def test_new_python_file_counts_as_change(self):
        fake = self._fake_opencode('echo "EXTRA = 1" > backoffice/extra.py\n')

        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            opencode_path=fake,
        )

        self.assertTrue(result.applied)
        self.assertIn("backoffice/extra.py", result.note)

    def test_no_change_is_not_applied_and_does_not_raise(self):
        fake = self._fake_opencode('echo "nothing to do"\n')

        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            opencode_path=fake,
        )

        self.assertFalse(result.applied)
        self.assertIn("exit=0", result.note)
        self.assertIn("changed=none", result.note)
        self.assertIn("nothing to do", result.note)

    def test_nonzero_exit_is_not_applied_and_records_code(self):
        fake = self._fake_opencode(
            'echo "FIXED = True" > backoffice/billing.py\n'
            'echo "simulated failure" >&2\n'
            "exit 3\n"
        )

        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            opencode_path=fake,
        )

        self.assertFalse(result.applied)
        self.assertIn("exit=3", result.note)
        self.assertIn("simulated failure", result.note)
        # The file did change, but a failed subprocess must not claim success.
        self.assertIn("backoffice/billing.py", result.note)

    def test_timeout_is_not_applied_and_does_not_raise(self):
        fake = self._fake_opencode("exec sleep 5\n")

        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            timeout_s=0.2,
            opencode_path=fake,
        )

        self.assertFalse(result.applied)
        self.assertIn("timeout", result.note)
        self.assertIn("elapsed=", result.note)

    def test_missing_binary_is_not_applied_and_does_not_raise(self):
        result = llm_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            opencode_path=self.tmp / "does-not-exist",
        )

        self.assertFalse(result.applied)
        self.assertIn("error=", result.note)


class RunnerBackendTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wire2-runner-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.work = self.tmp / "work"

    def test_scripted_is_the_default_and_llm_is_not_used(self):
        self.assertEqual(
            inspect.signature(run_arm).parameters["actor_backend"].default,
            "scripted",
        )

        with mock.patch("core.llm_actors.llm_act") as llm_mock:
            record = run_arm(
                "A",
                DEMO_REPO,
                max_rounds=1,
                work_root=self.work / "scripted",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )

        llm_mock.assert_not_called()
        self.assertEqual(record.outcome, "CAPPED")
        self.assertTrue(record.rounds[0]["fix_results"][0]["applied"])

    def test_llm_backend_is_selected_and_receives_assignments(self):
        mock_result = FixResult(
            actor_id="generalist", applied=False, note="mock llm backend"
        )

        with mock.patch(
            "core.llm_actors.llm_act", return_value=mock_result
        ) as llm_mock:
            record = run_arm(
                "A",
                DEMO_REPO,
                max_rounds=1,
                work_root=self.work / "llm",
                actor_backend="llm",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )

        self.assertEqual(llm_mock.call_count, 1)
        actor_id, workspace, assigned, failed = llm_mock.call_args.args
        self.assertEqual(actor_id, "generalist")
        self.assertTrue(Path(workspace).is_dir())
        self.assertEqual(len(assigned), 8)
        self.assertIn(ASSIGNED[0], assigned)
        self.assertEqual(len(failed), 8)
        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(
            record.rounds[0]["fix_results"][0]["note"], "mock llm backend"
        )

    def test_unknown_backend_is_rejected_before_copying(self):
        with self.assertRaises(ValueError):
            run_arm(
                "A",
                DEMO_REPO,
                max_rounds=0,
                work_root=self.work / "bad",
                actor_backend="bogus",
            )

        self.assertFalse((self.work / "bad").exists())


class RunnerCliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wire2-cli-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_actors_flag_reaches_run_arm(self):
        from core.__main__ import main

        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        cases = [([], "agentscope")]

        for extra, expected in cases:
            with self.subTest(expected=expected):
                out = self.tmp / f"{expected}.json"
                argv = [
                    "run",
                    "--arm",
                    "C",
                    "--workspace",
                    str(DEMO_REPO),
                    "--out",
                    str(out),
                    "--vault",
                    str(DEMO_VAULT),
                    "--kb",
                    str(DEMO_KB),
                    *extra,
                ]
                with mock.patch(
                    "core.__main__.run_arm", return_value=record
                ) as run_mock:
                    self.assertEqual(main(argv), 0)

                self.assertEqual(
                    run_mock.call_args.kwargs["actor_backend"], expected
                )
                self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()
