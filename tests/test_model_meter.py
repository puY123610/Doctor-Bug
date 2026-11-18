"""计量包装层回归测试：包装后的流式返回必须仍是 async generator。"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from core import usage as _usage
from core.model_config import _MeteredChatModel


class _FakeChunk:
    def __init__(self, text: str, is_last: bool, usage=None) -> None:
        self.text = text
        self.is_last = is_last
        self.usage = usage


async def _fake_stream():
    yield _FakeChunk("a", False)
    yield _FakeChunk(
        "b",
        True,
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            cache_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _FakeStreamModel:
    async def __call__(self, *args, **kwargs):
        return _fake_stream()


class _FakeResponse:
    def __init__(self) -> None:
        self.usage = SimpleNamespace(
            input_tokens=5,
            output_tokens=1,
            cache_input_tokens=0,
            cache_creation_input_tokens=0,
        )


class _FakePlainModel:
    async def __call__(self, *args, **kwargs):
        return _FakeResponse()


class MeteredModelStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_stays_async_generator(self) -> None:
        model = _MeteredChatModel(_FakeStreamModel())
        res = await model([])
        self.assertTrue(
            inspect.isasyncgen(res),
            "计量包装后流式返回不再是 async generator，框架会误判",
        )
        chunks = [chunk async for chunk in res]
        self.assertEqual([c.text for c in chunks], ["a", "b"])
        self.assertTrue(chunks[-1].is_last)

    async def test_stream_usage_is_metered(self) -> None:
        # CLEAN1：老计数器已删，改断言统一累计器里的记录（同一组数）。
        acc = _usage.reset_current()
        self.addCleanup(_usage.reset_current)
        model = _MeteredChatModel(_FakeStreamModel())
        res = await model([])
        _ = [chunk async for chunk in res]
        (rec,) = acc.records()
        self.assertEqual(rec.source, "provider")
        self.assertEqual(rec.input_tokens, 3)
        self.assertEqual(rec.output_tokens, 2)

    async def test_plain_response_passes_through_and_meters(self) -> None:
        # CLEAN1：老计数器已删，改断言统一累计器里的记录（同一组数）。
        acc = _usage.reset_current()
        self.addCleanup(_usage.reset_current)
        model = _MeteredChatModel(_FakePlainModel())
        res = await model([])
        self.assertIsInstance(res, _FakeResponse)
        (rec,) = acc.records()
        self.assertEqual(rec.source, "provider")
        self.assertEqual(rec.input_tokens, 5)


if __name__ == "__main__":
    unittest.main()
