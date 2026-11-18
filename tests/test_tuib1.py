"""TUIB1：首页最近工作区（访问历史，不是信任记录；重入仍每次问信任）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from textual.widgets import OptionList, Static

from agentscope_ext.fake_model import FakeChatModel
from core import keybinds, recents
from core.recents import EMPTY_TEXT, MAX_RECENTS
from core.tui import (
    CifixChatApp,
    HomeScreen,
    PromptInput,
    SessionScreen,
    WorkspaceTrustScreen,
)


class _TempHome:
    """把 HOME 与 XDG_CONFIG_HOME 都指到临时目录，绝不碰真实用户配置。"""

    def setUp(self) -> None:  # noqa: D401 - mixin
        self.tmp = Path(tempfile.mkdtemp(prefix="tuib1-")).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.xdg = self.tmp / "xdg"
        patcher = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.xdg)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.xdg / "drbug" / "recents.json"

    def mkdir(self, name: str, base: Path | None = None) -> Path:
        path = (base or self.home) / name
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def paths(self) -> list[str]:
        return [entry.path for entry in recents.load()]


class RecordTest(_TempHome, unittest.TestCase):
    def test_file_lives_under_user_config_dir(self) -> None:
        self.assertEqual(recents.recents_path(), self.file)
        recents.record(self.mkdir("a"))
        data = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(set(data["recents"][0]), {"path", "entered_at"})

    def test_reentry_dedupes_and_moves_to_top_with_new_time(self) -> None:
        a, b = self.mkdir("a"), self.mkdir("b")
        t0 = datetime(2026, 1, 1, 9, 0)
        recents.record(a, now=t0)
        recents.record(b, now=t0 + timedelta(minutes=1))
        recents.record(a, now=t0 + timedelta(minutes=2))
        entries = recents.load()
        self.assertEqual([e.path for e in entries], [str(a), str(b)])
        self.assertEqual(entries[0].display_time(), "2026-01-01 09:02")

    def test_cap_drops_oldest(self) -> None:
        dirs = [self.mkdir(f"p{i}") for i in range(MAX_RECENTS + 2)]
        for d in dirs:
            recents.record(d)
        self.assertEqual(MAX_RECENTS, 8)
        self.assertEqual(
            self.paths(), [str(d) for d in reversed(dirs)][:MAX_RECENTS]
        )

    def test_aliases_resolve_to_one_entry(self) -> None:
        real = self.mkdir("proj")
        link = self.home / "proj-link"
        link.symlink_to(real, target_is_directory=True)
        recents.record("~/proj")
        recents.record(str(link))
        recents.record(str(real / ".." / "proj"))
        cwd = os.getcwd()
        os.chdir(self.home)
        try:
            recents.record("proj")
        finally:
            os.chdir(cwd)
        self.assertEqual(self.paths(), [str(real)])

    def test_missing_or_non_dir_not_recorded(self) -> None:
        self.assertIsNone(recents.record(self.home / "nope"))
        afile = self.home / "f.txt"
        afile.write_text("x", encoding="utf-8")
        self.assertIsNone(recents.record(afile))
        self.assertFalse(self.file.exists())

    def test_corrupt_file_is_empty_and_rewritten(self) -> None:
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{not json", encoding="utf-8")
        self.assertEqual(recents.load(), [])
        self.assertEqual(recents.prune(), [])
        a = self.mkdir("a")
        recents.record(a)
        self.assertEqual(self.paths(), [str(a)])
        json.loads(self.file.read_text(encoding="utf-8"))

    def test_prune_drops_deleted_keeps_valid(self) -> None:
        keep, gone = self.mkdir("keep"), self.mkdir("gone")
        recents.record(keep)
        recents.record(gone)
        gone.rmdir()
        self.assertEqual([e.path for e in recents.prune()], [str(keep)])
        self.assertEqual(self.paths(), [str(keep)])


def _app(root: Path) -> CifixChatApp:
    return CifixChatApp(
        model=FakeChatModel(fallback_reply="收到"), workspace_root=root
    )


async def _settle(pilot, n: int = 4) -> None:
    for _ in range(n):
        await pilot.pause()


def _rows(app) -> list[str]:
    ol = app.screen.query_one("#home-recents", OptionList)
    return [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]


class HomeListTest(_TempHome, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _TempHome.setUp(self)
        self.plain = self.mkdir("plain")  # 非项目目录：启动待命，不绑定

    async def test_rows_match_file_with_tilde_and_time(self) -> None:
        a, b = self.mkdir("work/a"), self.mkdir("b", base=self.tmp)
        recents.record(a, now=datetime(2026, 3, 4, 5, 6))
        recents.record(b, now=datetime(2026, 3, 4, 7, 8))
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            rows = _rows(app)
            self.assertEqual(len(rows), len(recents.load()))
            self.assertEqual(rows[0], f"{b}  2026-03-04 07:08")
            self.assertEqual(rows[1], "~/work/a  2026-03-04 05:06")
            for row in rows:
                self.assertRegex(row, r"  \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
            empty = app.screen.query_one("#home-recents-empty", Static)
            self.assertEqual(str(empty.styles.display), "none")

    async def test_empty_shows_placeholder_line(self) -> None:
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            empty = app.screen.query_one("#home-recents-empty", Static)
            self.assertEqual(str(empty.render()), EMPTY_TEXT)
            self.assertEqual(str(empty.styles.display), "block")
            ol = app.screen.query_one("#home-recents", OptionList)
            self.assertEqual(ol.option_count, 0)
            self.assertEqual(str(ol.styles.display), "none")

    async def test_mount_prunes_deleted_entries(self) -> None:
        keep, gone = self.mkdir("keep"), self.mkdir("gone")
        recents.record(keep)
        recents.record(gone)
        gone.rmdir()
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            self.assertEqual(_rows(app), [recents.display_row(recents.load()[0])])
            self.assertEqual(self.paths(), [str(keep)])
            self.assertEqual(app.history, [])  # 不落提示行

    async def _focus_list(self, app, pilot) -> None:
        self.assertIsInstance(app.focused, PromptInput)
        await pilot.press("tab")  # Textual 默认焦点链
        await _settle(pilot)
        self.assertEqual(getattr(app.focused, "id", None), "home-recents")

    async def test_enter_on_list_asks_trust_and_deny_keeps_home(self) -> None:
        target = self.mkdir("proj")
        recents.record(target, now=datetime(2026, 1, 1, 0, 0))
        before = self.file.read_text(encoding="utf-8")
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one(PromptInput).text = "草稿保留"
            await self._focus_list(app, pilot)
            await pilot.press("enter")
            await _settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)
            self.assertEqual(app.screen.path, target)
            app.screen.dismiss(False)
            await _settle(pilot)
            self.assertIsInstance(app.screen, HomeScreen)
            self.assertIsNone(app._active_workspace)
            self.assertEqual(self.file.read_text(encoding="utf-8"), before)
            self.assertEqual(app.screen.query_one(PromptInput).text, "草稿保留")
            self.assertEqual(len(_rows(app)), 1)

    async def test_accept_binds_and_updates_record(self) -> None:
        other, target = self.mkdir("other"), self.mkdir("proj")
        recents.record(target, now=datetime(2026, 1, 1, 0, 0))
        recents.record(other, now=datetime(2026, 1, 1, 0, 1))
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await self._focus_list(app, pilot)
            await pilot.press("down", "enter")
            await _settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)
            self.assertEqual(app.screen.path, target)
            await pilot.press("enter")  # 高亮在「使用这个目录」
            await _settle(pilot)
            self.assertEqual(Path(app._active_workspace), target)
            entries = recents.load()
            self.assertEqual(entries[0].path, str(target))
            self.assertNotEqual(entries[0].display_time(), "2026-01-01 00:00")
            self.assertEqual(_rows(app)[0].split("  ")[0], "~/proj")

    async def test_recent_reentry_still_asks_trust(self) -> None:
        target = self.mkdir("proj")
        recents.record(target)
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._session_trusted.add(str(target))  # 哪怕本次已同意过
            app.enter_recent(str(target))
            await _settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)

    async def test_enter_in_prompt_still_submits(self) -> None:
        recents.record(self.mkdir("proj"))
        app = _app(self.plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "/status"
            await pilot.pause()
            # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
            if prompt.box is not None and prompt.box.suggestions_visible:
                await pilot.press("enter")
                await pilot.pause()
            await pilot.press("enter")
            await _settle(pilot)
            # 回车走提交：已离开首页进会话页（/status 再在其上开状态弹窗），没进列表
            stack = list(app.screen_stack)
            self.assertFalse(
                any(isinstance(s, WorkspaceTrustScreen) for s in stack)
            )
            self.assertTrue(any(isinstance(s, SessionScreen) for s in stack))
            self.assertEqual(app.page, "session")
            self.assertEqual(app.history[0][1], "/status")
            self.assertIsNone(app._active_workspace)


class CdRecordTest(_TempHome, unittest.IsolatedAsyncioTestCase):
    async def _cd(self, accept: bool) -> tuple[CifixChatApp, Path]:
        plain, target = self.mkdir("plain"), self.mkdir("proj")
        app = _app(plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._interactive_gates = True  # 只开 /cd 的信任问答
            app._cmd_cd(f"/cd {target}")
            await _settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)
            app.screen.dismiss(accept)
            await _settle(pilot)
        return app, target

    async def test_cd_trust_accept_records(self) -> None:
        _, target = await self._cd(True)
        self.assertEqual(self.paths(), [str(target)])

    async def test_cd_trust_deny_does_not_record(self) -> None:
        await self._cd(False)
        self.assertFalse(self.file.exists())

    async def test_cd_without_trust_question_does_not_record(self) -> None:
        plain, target = self.mkdir("plain"), self.mkdir("proj")
        app = _app(plain)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._cmd_cd(f"/cd {target}")  # 非交互闸门：没走信任问答
            await _settle(pilot)
            self.assertEqual(Path(app._active_workspace), target)
        self.assertFalse(self.file.exists())


class KeybindTest(unittest.TestCase):
    def test_no_new_bindings(self) -> None:
        self.assertEqual(
            HomeScreen.BINDINGS, keybinds.bindings_for(keybinds.SCOPE_PAGE)
        )
        # TUIC1 新增 sidebar_toggle / thought_toggle 两条（TUIB1 本身未新增）。
        # TUIC2 再增 timeline / timeline_close 两条。
        # BLK1 再增 thought_block_toggle 一条（单子要求的新键位）。
        # BLK2 再增 tool_block_toggle 一条（单子要求的新键位）。
        # COPY1 再增 copy_last_reply 一条（单子授权，默认不绑）。
        self.assertEqual(len(keybinds.DEFAULTS), 40)
        self.assertFalse(re.search(r"\bBinding\(", Path(recents.__file__).read_text()))


if __name__ == "__main__":
    unittest.main()
