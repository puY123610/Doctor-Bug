"""LINK1：回复区点击 Markdown 链接打开（只认 style.link，http/https 白名单）。

全部在真实 CifixChatApp 上用 Pilot 真点（mouse_down → mouse_up → click 同一格）
真拖；打开动作在标准库 webbrowser.open 上截获（mock.patch("webbrowser.open")），
记下每次调用的 URL。剪贴板沿用 COPY1 的假 pbcopy 截获。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from core import tui
from core.tui import PromptInput, RichLog
from tests.test_copy1 import _CopyCase

REPLY = (
    "参考资料如下：\n\n"
    "- 规范见 [例子](https://example.com/a?b=1#x) 这一节\n"
    "- 本地说明 [文档](docs/a.md) 和 [口令](/etc/passwd) 两处\n"
    "- 危险写法 [密码](file:///etc/passwd) 与 [弹窗](javascript:alert(1)) 两处\n\n"
    "普通文字结尾，这里没有链接。"
)

HINT = "这个链接不是网址，没有打开"


class _LinkCase(_CopyCase):
    def setUp(self) -> None:
        super().setUp()
        self.opened: list[str] = []

        def fake_open(url, *args, **kwargs):
            self.opened.append(url)
            return True

        patcher = mock.patch("webbrowser.open", side_effect=fake_open)
        self.web_open = patcher.start()
        self.addCleanup(patcher.stop)

    def _app(self, reply: str = REPLY):
        return super()._app(reply)

    def _at(self, log: RichLog, needle: str, offset: int = 0) -> tuple[int, int]:
        y, line = self._row_of(log, needle)
        return self._cell(log, y, line.index(needle) + offset)

    async def _click(self, pilot, log, cell, **mods) -> None:
        await pilot.click(log, offset=cell, **mods)
        for _ in range(5):
            await pilot.pause()

    @staticmethod
    def _links_on_row(log: RichLog, needle: str) -> list[str]:
        for strip in log.lines:
            if needle in strip.text:
                return [s.style.link for s in strip if s.style and s.style.link]
        raise AssertionError(f"日志里找不到 {needle!r}")


class ClickOpenTest(_LinkCase):
    # a. 点链接文字 → webbrowser.open 恰好一次，URL 逐字对
    async def test_a_click_link_opens_exact_url_once(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            # 数据来源：Markdown 渲染出的链接段，style.link 即原 URL
            self.assertIn("https://example.com/a?b=1#x", self._links_on_row(log, "例子"))
            await self._click(pilot, log, self._at(log, "例子", 1))
            self.assertEqual(self.opened, ["https://example.com/a?b=1#x"])
            self.assertEqual(notes, [])
            self.assertIsNone(self._pbcopy_got())

    # b. 点普通文字 → 零调用、零提示
    async def test_b_click_plain_text_does_nothing(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            await self._click(pilot, log, self._at(log, "普通文字", 1))
            await self._click(pilot, log, self._at(log, "规范见"))
            self.assertEqual(self.opened, [])
            self.assertEqual(notes, [])
            self.assertIsNone(self._pbcopy_got())

    # c. 拖过链接文字 → 走 COPY1 复制，零打开
    async def test_c_drag_over_link_copies_not_opens(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            log = await self._ask(app, pilot)
            y, line = self._row_of(log, "例子")
            i, j = line.index("规范见"), line.index("这一节") + 2
            await self._drag(pilot, log, self._cell(log, y, i), self._cell(log, y, j))
            self.assertEqual(self._pbcopy_got(), "规范见 例子 这一节")
            self.assertEqual(self.opened, [])

    # 修饰键：按住 ctrl/meta/shift 点链接不算点击
    async def test_f_click_with_modifier_does_not_open(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            log = await self._ask(app, pilot)
            cell = self._at(log, "例子", 1)
            await self._click(pilot, log, cell, control=True)
            await self._click(pilot, log, cell, meta=True)
            await self._click(pilot, log, cell, shift=True)
            self.assertEqual(self.opened, [])

    # 打开失败（浏览器起不来）→ 不崩，给一行提示
    async def test_g_open_failure_hints_without_crash(self) -> None:
        self.web_open.side_effect = lambda url, *a, **k: False
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            await self._click(pilot, log, self._at(log, "例子", 1))
            self.assertEqual(self.web_open.call_count, 1)
            self.assertEqual(len(notes), 1)
            self.assertIn("没能打开浏览器", notes[0][0])
            self.assertEqual(notes[0][1], "warning")


class NotWebLinkTest(_LinkCase):
    # d1. 渲染得出来的本地路径链接 → 零调用 + 一行提示
    async def test_d1_local_path_links_hint_not_open(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            links = self._links_on_row(log, "文档")
            self.assertIn("docs/a.md", links)
            self.assertIn("/etc/passwd", links)
            await self._click(pilot, log, self._at(log, "文档", 1))
            await self._click(pilot, log, self._at(log, "口令", 1))
            self.assertEqual(self.opened, [])
            self.assertEqual(len(notes), 2)
            self.assertTrue(all(HINT in text for text, _ in notes), notes)
            self.assertIn("docs/a.md", notes[0][0])
            self.assertIn("/etc/passwd", notes[1][0])

    # d2. file: / javascript: 被 markdown-it 拦成纯文字 → 无 style.link、零调用、无提示
    async def test_d2_file_and_javascript_render_as_plain_text(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            y, line = self._row_of(log, "危险写法")
            self.assertIn("[密码](file:///etc/passwd)", line)
            self.assertIn("[弹窗](javascript:alert(1))", line)
            self.assertEqual(self._links_on_row(log, "危险写法"), [])
            await self._click(pilot, log, self._at(log, "密码", 1))
            await self._click(pilot, log, self._at(log, "弹窗", 1))
            await self._click(pilot, log, self._at(log, "javascript"))
            self.assertEqual(self.opened, [])
            self.assertEqual(notes, [])

    # d3. 白名单函数：只认 http/https 且 netloc 非空
    def test_d3_whitelist(self) -> None:
        ok = ["https://example.com/a?b=1#x", "http://x.org", "HTTPS://Example.COM/p"]
        bad = [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,hi",
            "http:///no-host",
            "http:foo",
            "https://",
            "docs/a.md",
            "/etc/passwd",
            "ftp://x.org/f",
            "mailto:a@b.c",
            "",
        ]
        for url in ok:
            self.assertTrue(tui.is_web_link(url), url)
        for url in bad:
            self.assertFalse(tui.is_web_link(url), url)


class ConfirmTest(_LinkCase):
    # e. 等待确认时点链接：打开一次，确认不被打扰，答 n 照常拒绝
    async def test_e_click_link_during_confirm_keeps_answer(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            log = await self._ask(app, pilot)
            task = asyncio.ensure_future(app._present_confirm())
            for _ in range(50):
                await pilot.pause()
                if app._awaiting_confirm:
                    break
            self.assertTrue(app._awaiting_confirm)
            await self._click(pilot, log, self._at(log, "例子", 1))
            self.assertEqual(self.opened, ["https://example.com/a?b=1#x"])
            self.assertTrue(app._awaiting_confirm)
            self.assertFalse(task.done())
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "n"
            await pilot.pause()
            await pilot.press("enter")
            self.assertEqual(await asyncio.wait_for(task, 5), "n")


if __name__ == "__main__":
    unittest.main()
