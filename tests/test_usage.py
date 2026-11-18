# -*- coding: utf-8 -*-
"""TUIM1b：目录数据 pin + 用量数据契约（core/usage.py）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import model_config, usage
from core.model_catalog import (
    catalog_model,
    format_context,
    format_price,
    format_price_pair,
    PROVIDER_MODELS,
)
from core.model_config import _MeteredChatModel
from core.usage import UsageAccumulator, UsageRecord, compute_cost


def _rec(call_id="c1", source="provider", in_tok=100, out_tok=10,
         price_in=0.15, price_out=0.6, cache_read=None):
    return UsageRecord(
        call_id=call_id, ts=0.0, provider="deepseek", model="deepseek-flash",
        effort=None, input_tokens=in_tok, output_tokens=out_tok,
        cache_read_tokens=cache_read, cache_write_tokens=None, source=source,
        price_in_snapshot=price_in, price_out_snapshot=price_out,
        cost=compute_cost(in_tok, out_tok, price_in, price_out),
    )


class CatalogDataPinTest(unittest.TestCase):
    def test_deepseek_flash_four_columns(self) -> None:
        row = catalog_model("deepseek", "deepseek-flash")
        self.assertEqual(
            (row.context_limit, row.output_limit, row.price_in, row.price_out),
            (1048576, 393216, 0.15, 0.6),
        )

    def test_every_catalog_row_has_data(self) -> None:
        for rows in PROVIDER_MODELS.values():
            for row in rows:
                self.assertIsNotNone(row.price_in, row.id)
                self.assertIsNotNone(row.context_limit, row.id)

    def test_price_format(self) -> None:
        self.assertEqual(format_price(0.8906), "0.891")
        self.assertEqual(format_price(2), "2")
        self.assertEqual(format_price(0.15), "0.15")
        self.assertIsNone(format_price(None))

    def test_context_format(self) -> None:
        self.assertEqual(format_context(1048576), "1M")
        self.assertEqual(format_context(1000000), "1M")
        self.assertEqual(format_context(262144), "262K")
        self.assertIsNone(format_context(None))

    def test_unknown_price_pair_not_shown(self) -> None:
        row = catalog_model("deepseek", "deepseek-flash")
        self.assertEqual(format_price_pair(row), "$0.15/$0.6 每百万token")
        from dataclasses import replace
        self.assertIsNone(format_price_pair(replace(row, price_in=None)))


class AccumulatorTest(unittest.TestCase):
    def test_provider_record_accumulates_as_is(self) -> None:
        acc = UsageAccumulator()
        acc.add(_rec("a", in_tok=100, out_tok=10))
        acc.add(_rec("b", in_tok=50, out_tok=5))
        t = acc.totals()
        self.assertEqual(t["input_tokens"], {"sum": 150, "missing": 0})
        self.assertEqual(t["output_tokens"], {"sum": 15, "missing": 0})
        self.assertAlmostEqual(t["cost"]["sum"], (150 * 0.15 + 15 * 0.6) / 1e6)
        self.assertEqual(t["cost"]["unknown"], 0)

    def test_estimate_is_marked(self) -> None:
        acc = UsageAccumulator()
        acc.add(_rec("a", source="estimate"))
        self.assertEqual(acc.records()[0].source, "estimate")
        self.assertEqual(acc.totals()["estimated_calls"], 1)

    def test_duplicate_call_id_counted_once(self) -> None:
        acc = UsageAccumulator()
        self.assertTrue(acc.add(_rec("same")))
        self.assertFalse(acc.add(_rec("same")))
        self.assertEqual(acc.totals()["calls"], 1)
        self.assertEqual(acc.totals()["input_tokens"]["sum"], 100)

    def test_unknown_price_cost_none_and_counted(self) -> None:
        acc = UsageAccumulator()
        rec = _rec("a", price_in=None, price_out=None)
        self.assertIsNone(rec.cost)
        acc.add(rec)
        acc.add(_rec("b"))
        t = acc.totals()
        self.assertEqual(t["cost"]["unknown"], 1)
        self.assertAlmostEqual(t["cost"]["sum"], (100 * 0.15 + 10 * 0.6) / 1e6)

    def test_unknown_tokens_not_counted_as_zero(self) -> None:
        acc = UsageAccumulator()
        rec = _rec("a", in_tok=None)
        self.assertIsNone(rec.cost)
        acc.add(rec)
        acc.add(_rec("b", in_tok=40))
        t = acc.totals()
        self.assertEqual(t["input_tokens"], {"sum": 40, "missing": 1})
        self.assertEqual(t["cache_read_tokens"], {"sum": 0, "missing": 2})


class _Resp:
    def __init__(self, text, usage_obj):
        self.content = [{"type": "text", "text": text}]
        self.usage = usage_obj


class _Plain:
    def __init__(self, usage_obj):
        self.usage_obj = usage_obj

    async def __call__(self, *args, **kwargs):
        return _Resp("好的 done", self.usage_obj)


class _Chunk:
    def __init__(self, text, usage_obj=None):
        self.content = [{"type": "text", "text": text}]
        self.usage = usage_obj


class _Stream:
    def __init__(self, usage_obj):
        self.usage_obj = usage_obj

    async def __call__(self, *args, **kwargs):
        async def gen():
            yield _Chunk("a")
            yield _Chunk("ab", self.usage_obj)
        return gen()


_USAGE = SimpleNamespace(input_tokens=7, output_tokens=3,
                         cache_input_tokens=2, cache_creation_input_tokens=0)


class MeterWiringTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.acc = usage.reset_current()

    async def test_provider_usage_becomes_record_with_price(self) -> None:
        m = _MeteredChatModel(_Plain(_USAGE), provider="deepseek",
                              model="deepseek-flash", effort="high")
        await m([{"role": "user", "content": "hi"}])
        (rec,) = self.acc.records()
        self.assertEqual(rec.source, "provider")
        self.assertEqual((rec.input_tokens, rec.output_tokens, rec.cache_read_tokens), (7, 3, 2))
        self.assertEqual((rec.price_in_snapshot, rec.effort), (0.15, "high"))
        self.assertAlmostEqual(rec.cost, (7 * 0.15 + 3 * 0.6) / 1e6)

    async def test_missing_usage_is_estimate(self) -> None:
        # CLEAN1：原名 ..._and_old_counter_untouched；老计数器已删，去掉其断言。
        m = _MeteredChatModel(_Plain(None), provider="deepseek", model="deepseek-flash")
        await m([{"role": "user", "content": "abcdefgh"}])
        (rec,) = self.acc.records()
        self.assertEqual(rec.source, "estimate")
        self.assertEqual(rec.input_tokens, 2)
        self.assertIsNone(rec.cache_read_tokens)

    async def test_custom_model_has_no_price(self) -> None:
        m = _MeteredChatModel(_Plain(_USAGE), provider="deepseek", model="my-custom")
        await m([])
        (rec,) = self.acc.records()
        self.assertIsNone(rec.price_in_snapshot)
        self.assertIsNone(rec.cost)

    async def test_stream_records_once(self) -> None:
        m = _MeteredChatModel(_Stream(_USAGE), provider="deepseek", model="deepseek-flash")
        res = await m([])
        _ = [c async for c in res]
        self.assertEqual(len(self.acc.records()), 1)
        self.assertEqual(self.acc.records()[0].output_tokens, 3)

    async def test_two_calls_two_records(self) -> None:
        m = _MeteredChatModel(_Plain(_USAGE), provider="deepseek", model="deepseek-flash")
        await m([])
        await m([])
        self.assertEqual(self.acc.totals()["calls"], 2)


if __name__ == "__main__":
    unittest.main()
