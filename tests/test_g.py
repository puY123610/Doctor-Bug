"""G：用量口径统一（侧边栏与 /status 同一读取口）。

口径逐条一测：厂商 usage 优先、估算标记、未知不记 0、调用时价格快照、
调用 ID 去重；外加侧边栏与 /status 同一会话同一轮次数字一致。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from core import model_config, status_view, usage
from core.model_catalog import catalog_model
from core.model_config import _MeteredChatModel
from core.usage import UsageRecord, compute_cost, summarize
from tests.test_tuic1 import _settle, _sidebar_text, _to_session
from tests.test_tui import FakeChatModel
from core.tui import CifixChatApp, PLATFORM_ROOT  # noqa: F401


class _Resp:
    def __init__(self, text, usage_obj):
        self.content = [{"type": "text", "text": text}]
        self.usage = usage_obj


class _Plain:
    def __init__(self, usage_obj):
        self.usage_obj = usage_obj

    async def __call__(self, *args, **kwargs):
        return _Resp("好的 done", self.usage_obj)


_PROVIDER = SimpleNamespace(
    input_tokens=7, output_tokens=3, cache_input_tokens=0, cache_creation_input_tokens=0
)


def _rec(call_id, in_tok, out_tok, cost, source="provider"):
    return UsageRecord(
        call_id=call_id, ts=0.0, provider="deepseek", model="deepseek-flash",
        effort=None, input_tokens=in_tok, output_tokens=out_tok,
        cache_read_tokens=None, cache_write_tokens=None, source=source,
        price_in_snapshot=None, price_out_snapshot=None, cost=cost,
    )


class CaliberTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.acc = usage.reset_current()
        self.addCleanup(usage.reset_current)

    async def test_provider_usage_preferred(self) -> None:
        m = _MeteredChatModel(_Plain(_PROVIDER), provider="deepseek", model="deepseek-flash")
        await m([{"role": "user", "content": "x" * 400}])  # 估算会远大于 7
        (rec,) = self.acc.records()
        self.assertEqual(rec.source, "provider")
        self.assertEqual((rec.input_tokens, rec.output_tokens), (7, 3))
        s = summarize()
        self.assertEqual(s.tokens_text, "10")
        self.assertEqual(s.estimated_calls, 0)

    async def test_no_provider_usage_marked_estimate(self) -> None:
        m = _MeteredChatModel(_Plain(None), provider="deepseek", model="deepseek-flash")
        await m([{"role": "user", "content": "abcdefgh"}])
        (rec,) = self.acc.records()
        self.assertEqual(rec.source, "estimate")
        s = summarize()
        self.assertIn("含估算", s.tokens_text)
        self.assertIn("其中 1 次为估算", s.lines())

    def test_unknown_never_zero(self) -> None:
        empty = summarize()
        self.assertEqual(empty.tokens_text, usage.NO_CALLS_TEXT)
        self.assertEqual(empty.cost_text, usage.NO_CALLS_TEXT)
        self.assertNotIn("0.0000", empty.one_line())
        usage.record_usage(_rec("a", None, None, None))
        s = summarize()
        self.assertEqual((s.tokens_text, s.cost_text), ("未知", "未知"))
        usage.record_usage(_rec("b", 100, 20, 0.5))
        s = summarize()
        self.assertEqual(s.tokens_text, "120（1 次未知）")
        self.assertEqual(s.cost_text, "$0.5000（1 次未知）")

    async def test_price_snapshot_at_call_time(self) -> None:
        row = catalog_model("deepseek", "deepseek-flash")
        m = _MeteredChatModel(_Plain(_PROVIDER), provider="deepseek", model="deepseek-flash")
        await m([])
        expected = compute_cost(7, 3, row.price_in, row.price_out)
        dearer = SimpleNamespace(price_in=row.price_in * 100, price_out=row.price_out * 100)
        with mock.patch("core.model_catalog.catalog_model", return_value=dearer):
            (rec,) = self.acc.records()
            self.assertAlmostEqual(rec.cost, expected)
            self.assertEqual(summarize().cost_text, f"${expected:.4f}")

    def test_call_id_dedup(self) -> None:
        self.assertTrue(usage.record_usage(_rec("same", 10, 5, 0.1)))
        self.assertFalse(usage.record_usage(_rec("same", 10, 5, 0.1)))
        s = summarize()
        self.assertEqual(s.calls, 1)
        self.assertEqual(s.tokens_text, "15")


class SameNumbersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        usage.reset_current()
        self.addCleanup(usage.reset_current)

    async def test_sidebar_and_status_agree(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="收到"), workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await _to_session(app, pilot)
            await pilot.press("ctrl+b")
            await _settle(pilot)
            for step in (
                [],
                [_rec("a", None, None, None, source="estimate")],
                [_rec("b", 100, 20, 0.5)],
            ):
                for rec in step:
                    usage.record_usage(rec)
                app._refresh_sidebar()
                await _settle(pilot)
                sidebar = _sidebar_text(app)
                row = next(r for r in app.collect_status_rows() if r.name == "本会话用量")
                for line in summarize().lines():
                    self.assertIn(line, sidebar)
                    self.assertIn(line, row.note)
                self.assertEqual(row.note, summarize().one_line())

    def test_status_row_states(self) -> None:
        self.assertEqual(status_view.usage_row().state, status_view.STATE_UNKNOWN)
        usage.record_usage(_rec("x", 1, 1, None))
        self.assertEqual(status_view.usage_row().state, status_view.STATE_OK)


if __name__ == "__main__":
    unittest.main()
