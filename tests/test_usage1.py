# -*- coding: utf-8 -*-
"""USAGE1：运行记录用量改读统一口径（core.usage），与 summarize() 一致。"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import usage as U
from core.runner import run_arm

PLATFORM = Path(__file__).resolve().parents[1]


def _rec(call_id=None, *, inp=10, out=2, cr=0, cw=0, source="provider",
         pin=1.0, pout=2.0) -> U.UsageRecord:
    return U.UsageRecord(
        call_id=call_id or uuid.uuid4().hex,
        ts=0.0,
        provider="deepseek",
        model="deepseek-flash",
        effort=None,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        source=source,
        price_in_snapshot=pin,
        price_out_snapshot=pout,
        cost=U.compute_cost(inp, out, pin, pout),
    )


def _run_with(records):
    """跑一轮 scripted 运行，run_started 时登记 records，返回 (record, 本轮累计器)。"""
    U.reset_current()
    U.record_usage(_rec())  # 运行前已有的调用：不应计入本轮
    before = U.call_ids()

    def bump(event: dict) -> None:
        if event.get("event") == "run_started":
            for r in records:
                U.record_usage(r)

    with tempfile.TemporaryDirectory(prefix="usage1-") as tmp:
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
    return record, U.accumulator_since(before)


class RunRecordMatchesSummarizeTest(unittest.TestCase):
    def tearDown(self) -> None:
        U.reset_current()

    def test_record_usage_matches_summarize(self) -> None:
        records = [
            _rec(inp=100, out=20, cr=5, cw=1),
            _rec(inp=30, out=4, source="estimate", cr=None, cw=None),
        ]
        record, acc = _run_with(records)
        usage = record.usage
        totals = acc.totals()
        summary = U.summarize(acc)
        self.assertEqual(usage["calls"], summary.calls)
        self.assertEqual(usage["calls"], 2)  # 运行前那 1 次不计入
        self.assertEqual(usage["input_tokens"], totals["input_tokens"]["sum"])
        self.assertEqual(usage["output_tokens"], totals["output_tokens"]["sum"])
        self.assertEqual(
            f"{usage['input_tokens'] + usage['output_tokens']:,}（含估算）",
            summary.tokens_text,
        )
        self.assertEqual(usage["estimated_calls"], summary.estimated_calls)
        self.assertAlmostEqual(usage["cost"], totals["cost"]["sum"])
        self.assertEqual(usage["summary"], summary.one_line())
        self.assertEqual(record.to_dict()["usage"]["calls"], 2)


class MissingFieldNotZeroTest(unittest.TestCase):
    def tearDown(self) -> None:
        U.reset_current()

    def test_field_all_missing_is_none_not_zero(self) -> None:
        record, acc = _run_with([_rec(cr=None, cw=None), _rec(cr=None, cw=None)])
        usage = record.usage
        self.assertIsNone(usage["cache_input_tokens"])
        self.assertIsNone(usage["cache_creation_input_tokens"])
        self.assertEqual(usage["missing"]["cache_input_tokens"], 2)
        line = U.format_run_usage_line(usage)
        self.assertIn("缓存读 未知", line)
        self.assertNotIn("缓存读 0", line)

    def test_partial_missing_counts_known_and_marks_unknown(self) -> None:
        record, acc = _run_with([_rec(inp=7, out=1), _rec(inp=None, out=None, pin=None)])
        usage = record.usage
        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(usage["missing"]["input_tokens"], 1)
        self.assertEqual(usage["cost_unknown"], 1)
        self.assertIn("输入 7（1 次未知）", U.format_run_usage_line(usage))
        summary = U.summarize(acc)
        self.assertEqual(summary.tokens_text, "8（1 次未知）")
        self.assertEqual(usage["summary"], summary.one_line())

    def test_no_calls_writes_no_usage(self) -> None:
        record, _ = _run_with([])
        self.assertEqual(record.usage, {})
        self.assertNotIn("usage", record.to_dict())
        self.assertEqual(U.format_run_usage_line(record.usage), U.NOT_CAPTURED_LINE)


class CallIdDedupTest(unittest.TestCase):
    def tearDown(self) -> None:
        U.reset_current()

    def test_same_call_id_counted_once(self) -> None:
        cid = uuid.uuid4().hex
        record, acc = _run_with([_rec(cid, inp=50, out=5), _rec(cid, inp=50, out=5), _rec(inp=1, out=1)])
        self.assertEqual(record.usage["calls"], 2)
        self.assertEqual(record.usage["input_tokens"], 51)
        self.assertEqual(U.summarize(acc).calls, 2)


class FormatLineCompatTest(unittest.TestCase):
    def test_all_known_matches_old_format(self) -> None:
        usage = {"calls": 2, "input_tokens": 10, "output_tokens": 3,
                 "cache_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.assertEqual(
            U.format_run_usage_line(usage),
            "Token 用量: 输入 10 / 输出 3 / 缓存读 0 / 缓存写 0 / 模型调用 2 次",
        )

    def test_old_counter_not_read_by_runner(self) -> None:
        source = (PLATFORM / "core" / "runner.py").read_text(encoding="utf-8")
        self.assertNotIn("usage_snapshot", source)
        source = (PLATFORM / "core" / "parallel.py").read_text(encoding="utf-8")
        self.assertNotIn("usage_snapshot", source)


if __name__ == "__main__":
    unittest.main()
