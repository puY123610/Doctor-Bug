# -*- coding: utf-8 -*-
"""Reproduction probe for the fullscreen tearing bug:
write long records in a narrow terminal, resize wide, check reflow."""
import asyncio

from textual.widgets import RichLog


def widths(log: RichLog) -> list[int]:
    return [len(line.text) for line in log.lines if line.text.strip()]


async def main() -> None:
    from core.tui import CifixChatApp, SessionScreen

    long_text = "alpha beta gamma delta " * 12  # ~276 chars, wraps hard at 60
    app = CifixChatApp(model=None)
    async with app.run_test(size=(60, 24)) as pilot:
        await pilot.pause()
        app.push_screen(SessionScreen(app))
        await pilot.pause()
        app._append_record("user", "please fix the tax calculation module now")
        app._append_record("assistant", long_text)
        app._append_record("plain", long_text)
        await pilot.pause()
        log = app.screen.query_one("#log", RichLog)
        print("LOG WIDTH at 60:", log.size.width)
        before = widths(log)
        print("BEFORE max/avg:", max(before), sum(before) / len(before))
        await pilot.resize_terminal(120, 40)
        await pilot.pause(0.5)
        after = widths(log)
        print("LOG WIDTH at 120:", log.size.width)
        print("AFTER max/avg:", max(after), sum(after) / len(after))
        reflowed = max(after) > 100
        print("REFLOWED:", reflowed)
        await pilot.resize_terminal(50, 20)
        await pilot.pause(0.6)
        shrunk = widths(log)
        print("LOG WIDTH at 50:", log.size.width)
        print("SHRUNK max:", max(shrunk))
        print("SHRUNK_OK:", max(shrunk) <= log.size.width)


asyncio.run(main())
