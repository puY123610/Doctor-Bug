# -*- coding: utf-8 -*-
"""Backlog 10: usage persistence into records + audit log viewing."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import model_config
from core.runs import (
    format_audit_line,
    format_run_detail,
    read_audit_entries,
)
from core.runner import RunRecord, run_arm

PLATFORM = Path(__file__).resolve().parents[1]


class UsageShapeTests(unittest.TestCase):
    def test_usage_key_only_when_nonzero(self) -> None:
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        self.assertNotIn("usage", record.to_dict())
        record.usage = {"calls": 2, "input_tokens": 10, "output_tokens": 3}
        self.assertEqual(record.to_dict()["usage"]["calls"], 2)
        record.usage = {"calls": 0, "input_tokens": 0}
        self.assertNotIn("usage", record.to_dict())


class UsageDeltaTests(unittest.TestCase):
    def test_scripted_run_records_no_usage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="usage-") as tmp:
            record = run_arm(
                "C",
                PLATFORM / "tests" / "fixtures" / "demo_repo",
                max_rounds=3,
                work_root=Path(tmp) / "work",
                actor_backend="scripted",
                vault_dir=PLATFORM / "tests" / "fixtures" / "acceptor_vault",
                kb_path=PLATFORM / "kb" / "dispatch_table.json",
            )
            self.assertEqual(record.outcome, "PASS")
            self.assertNotIn("usage", record.to_dict())

    def test_meter_bump_during_run_lands_in_record(self) -> None:
        # USAGE1：runner 改读统一累计器（core.usage），不再读老计数器 _USAGE；
        # 原断言「往 _USAGE 加 3 次/42 输入 → record.usage 同值」改为往
        # 统一累计器登记 3 次调用（输入 14×3=42），断言不变。
        from core import usage as _usage

        def bump(event: dict) -> None:
            if event.get("event") == "run_started":
                for _ in range(3):
                    _usage.record_usage(
                        _usage.UsageRecord(
                            call_id=uuid.uuid4().hex,
                            ts=0.0,
                            provider="deepseek",
                            model="deepseek-flash",
                            effort=None,
                            input_tokens=14,
                            output_tokens=1,
                            cache_read_tokens=0,
                            cache_write_tokens=0,
                            source="provider",
                            price_in_snapshot=None,
                            price_out_snapshot=None,
                            cost=None,
                        )
                    )

        with tempfile.TemporaryDirectory(prefix="usage-") as tmp:
            record = run_arm(
                "C",
                PLATFORM / "tests" / "fixtures" / "demo_repo",
                max_rounds=3,
                work_root=Path(tmp) / "work",
                actor_backend="scripted",
                on_event=bump,
                vault_dir=PLATFORM / "tests" / "fixtures" / "acceptor_vault",
                kb_path=PLATFORM / "kb" / "dispatch_table.json",
            )
            self.assertEqual(record.usage.get("calls"), 3)
            self.assertEqual(record.usage.get("input_tokens"), 42)


class UsageDetailLineTests(unittest.TestCase):
    def test_detail_shows_token_line(self) -> None:
        from core.runs import RunSummary

        summary = RunSummary(
            run_id="x",
            kind="api",
            when="2026-11-16 00:00:00",
            arm="C",
            backend=None,
            kb_enabled=True,
            outcome="PASS",
            rounds_count=1,
            final_failed_count=0,
            review_verdict=None,
            scope_denials_count=0,
            record_path=Path("/nonexistent"),
            bundle_dir=None,
        )
        detail = {
            "summary": summary,
            "record": {
                "rounds": [],
                "usage": {"calls": 5, "input_tokens": 100, "output_tokens": 7},
            },
            "report_header": None,
        }
        text = "\n".join(format_run_detail(detail))
        self.assertIn("token：输入 100 · 输出 7 · 调用 5 次", text)


class AuditReaderTests(unittest.TestCase):
    def test_read_and_format(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-") as tmp:
            ws = Path(tmp)
            log_dir = ws / ".drbug"
            log_dir.mkdir(parents=True)
            lines = [
                json.dumps(
                    {
                        "ts": "2026-11-16T06:03:14+00:00",
                        "actor": "worker-a",
                        "action": "read_file",
                        "target": "x.py",
                        "verdict": "deny",
                        "tier": 1,
                        "reason": "r",
                    }
                ),
                "{corrupt",
                json.dumps(
                    {
                        "ts": "2026-11-16T06:04:14+00:00",
                        "actor": "worker-a",
                        "action": "shell_exec",
                        "target": "git diff",
                        "verdict": "allow",
                        "tier": 4,
                        "reason": "r",
                    }
                ),
            ]
            (log_dir / "audit.log").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            entries, corrupt = read_audit_entries(ws, limit=10)
            self.assertEqual(len(entries), 2)
            self.assertEqual(corrupt, 1)
            line = format_audit_line(entries[0])
            self.assertIn("worker-a", line)
            self.assertIn("deny（tier 1）", line)
            limited, _ = read_audit_entries(ws, limit=1)
            self.assertEqual(len(limited), 1)
            self.assertEqual(limited[0]["action"], "shell_exec")

    def test_missing_log_is_empty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="audit-") as tmp:
            entries, corrupt = read_audit_entries(Path(tmp))
            self.assertEqual(entries, [])
            self.assertEqual(corrupt, 0)


if __name__ == "__main__":
    unittest.main()
