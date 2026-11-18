"""Offline tests for the bugdoc CLI facade and runner progress events."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.cli import main as cli_main
from core.runner import run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"


class CliTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cli-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def _capture_main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv)
        return code, buffer.getvalue()

    def test_runner_event_sequence_on_scripted_c(self):
        events: list[dict] = []

        record = run_arm(
            "C",
            DEMO_REPO,
            work_root=self.tmp / "work",
            on_event=events.append,
            vault_dir=DEMO_REPO.parent / "acceptor_vault",
            kb_path=PLATFORM_ROOT / "kb" / "dispatch_table.json",
        )

        self.assertEqual(record.outcome, "PASS")
        self.assertGreaterEqual(len(events), 5)

        kinds = [item["event"] for item in events]
        self.assertEqual(kinds[0], "run_started")
        self.assertEqual(kinds[-1], "run_finished")
        for kind in ("verdict", "dispatch", "actor_done"):
            self.assertIn(kind, kinds)

        self.assertEqual(events[0]["arm"], "C")
        self.assertEqual(events[0]["actors"], "scripted")
        self.assertTrue(events[0]["kb_enabled"])
        self.assertEqual(events[-1]["outcome"], "PASS")
        self.assertEqual(events[-1]["final_failed"], [])
        self.assertEqual(events[-1]["rounds"], len(record.rounds))

        verdicts = [item for item in events if item["event"] == "verdict"]
        self.assertEqual(verdicts[0]["round"], 0)
        self.assertEqual(verdicts[0]["status"], "FAIL")
        self.assertEqual(verdicts[-1]["status"], "PASS")
        self.assertEqual(verdicts[-1]["failed_count"], 0)

    def test_runner_event_listener_exception_is_swallowed(self):
        def exploding_listener(event):
            raise RuntimeError("listener must not break dispatch")

        record = run_arm(
            "C",
            DEMO_REPO,
            work_root=self.tmp / "work-boom",
            on_event=exploding_listener,
            vault_dir=DEMO_REPO.parent / "acceptor_vault",
            kb_path=PLATFORM_ROOT / "kb" / "dispatch_table.json",
        )

        self.assertEqual(record.outcome, "PASS")

    def test_check_returns_zero_and_reports_key_status_without_secret(self):
        sentinel = "sk-cli-test-secret-value"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": sentinel}):
            code, output = self._capture_main(["check"])

        self.assertEqual(code, 0)
        self.assertIn("ENV_CHECK_PASS", output)
        self.assertIn("密钥状态", output)
        self.assertTrue("已设置" in output or "未设置" in output)
        self.assertNotIn(sentinel, output)

    def test_run_actors_flag_is_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._capture_main(
                [
                    "run",
                    "--arm",
                    "C",
                    "--actors",
                    "scripted",
                    "--workspace",
                    str(DEMO_REPO),
                ]
            )
        self.assertEqual(ctx.exception.code, 2)

if __name__ == "__main__":
    unittest.main()
