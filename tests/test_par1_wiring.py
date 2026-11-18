# -*- coding: utf-8 -*-
"""Backlog 7: parallel attempts reachable from the CLI and the secretary tool."""

from __future__ import annotations

import contextlib
import io
import os
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PLATFORM = Path(__file__).resolve().parents[1]


class CliParallelTests(unittest.TestCase):
    def test_cli_run_parallel_writes_parallel_record(self) -> None:
        from core import cli
        from core import parallel as parallel_module

        real_run_parallel = parallel_module.run_parallel

        def scripted_parallel(**kwargs):
            kwargs["actor_backend"] = "scripted"
            return real_run_parallel(**kwargs)

        with tempfile.TemporaryDirectory(prefix="parwire-") as tmp:
            out = Path(tmp) / "rec.json"
            buffer = io.StringIO()
            with mock.patch.object(
                parallel_module, "run_parallel", scripted_parallel
            ), mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(buffer):
                code = cli.main(
                    [
                        "run",
                        "--arm",
                        "C",
                        "--workspace",
                        str(PLATFORM / "tests" / "fixtures" / "demo_repo"),
                        "--parallel",
                        "2",
                        "--out",
                        str(out),
                    ]
                )
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertIn("并行尝试: 2 份", buffer.getvalue())
            record = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(record["outcome"], "PASS")
            self.assertEqual(record["parallel"]["attempts"], 2)
            self.assertIn(record["parallel"]["winner_index"], (0, 1))

    def test_cli_run_rejects_bad_parallel(self) -> None:
        proc = subprocess.run(
            [
                str(PLATFORM / "drbug"),
                "run",
                "--arm",
                "C",
                "--workspace",
                str(PLATFORM / "tests" / "fixtures" / "demo_repo"),
                "--parallel",
                "9",
            ],
            cwd=PLATFORM,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("parallel 须在 1-8 之间", proc.stdout)


class DispatcherParallelTests(unittest.TestCase):
    def test_tool_parallel_summary_and_record(self) -> None:
        from core import dispatcher

        text = dispatcher.run_repair(
            actors="scripted", workspace=str(PLATFORM / "tests" / "fixtures" / "demo_repo"), parallel=2
        )
        self.assertIn("并行尝试: 共 2 份", text)
        match = re.search(r"记录路径=(\S+)", text)
        self.assertIsNotNone(match, text)
        record_path = Path(match.group(1)).expanduser()
        if not record_path.is_absolute():
            record_path = PLATFORM / record_path
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["parallel"]["attempts"], 2)
        self.assertEqual(record["outcome"], "PASS")

    def test_tool_rejects_bad_parallel(self) -> None:
        from core import dispatcher

        text = dispatcher.run_repair(
            actors="scripted", workspace="demo_repo", parallel=0
        )
        self.assertIn("parallel 须在 1-8 之间", text)


if __name__ == "__main__":
    unittest.main()
