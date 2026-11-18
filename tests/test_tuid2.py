"""TUID2：输入框 @ 提及文件。三段 P0 红线逐条有测试。

1. 补全只发现：列表阶段不打开任何文件、不读内容。
2. 读取过门禁：PolicyEngine 拒绝 / 需确认 / 读不到 → 明确报错、不发送。
3. 发送前复验：越界（../、绝对路径、符号链接）拒绝；选中后被删、
   被改（大小或 mtime 变）→ 报错不发（定版：报错，不重读）。
"""

from __future__ import annotations

import builtins
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, file_mention, keybinds
from core.file_mention import MentionError
from core.tui import CifixChatApp, PromptInput

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


class _Engine:
    """门禁桩：记录调用，按给定裁决返回。"""

    def __init__(self, verdict: str = "allow") -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, str, str]] = []

    def decide(self, actor: str, action: str, target: str):
        self.calls.append((actor, action, target))
        return SimpleNamespace(verdict=self.verdict, tier=3, reason="测试规则")


def _no_open(*_args, **_kwargs):
    raise AssertionError("补全阶段不许打开文件")


class _WsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.ws = base / "ws"
        self.outside = base / "outside"
        (self.ws / "src").mkdir(parents=True)
        (self.ws / ".git").mkdir()
        self.outside.mkdir()
        (self.ws / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.ws / "README.md").write_text("# 说明\n", encoding="utf-8")
        (self.ws / ".git" / "config").write_text("x", encoding="utf-8")
        (self.outside / "secret.txt").write_text("外面的", encoding="utf-8")


class DiscoverOnlyTest(_WsCase):
    def test_listing_never_opens_files(self) -> None:
        with mock.patch.object(builtins, "open", _no_open), mock.patch.object(
            io, "open", _no_open
        ), mock.patch.object(os, "open", _no_open), mock.patch.object(
            Path, "open", _no_open
        ), mock.patch.object(Path, "read_bytes", _no_open), mock.patch.object(
            Path, "read_text", _no_open
        ):
            found = file_mention.discover_files(self.ws, "")
        self.assertIn("src/app.py", found)
        self.assertIn("README.md", found)

    def test_relative_paths_filter_and_skip_dirs(self) -> None:
        found = file_mention.discover_files(self.ws, "app")
        self.assertEqual(found[0], "src/app.py")
        self.assertNotIn("README.md", found)
        for rel in file_mention.discover_files(self.ws, ""):
            self.assertFalse(Path(rel).is_absolute())
            self.assertFalse(rel.startswith(".git/"))
        self.assertEqual(file_mention.discover_files(self.ws, "zzzqqq"), [])

    def test_symlink_outside_not_listed(self) -> None:
        (self.ws / "link.txt").symlink_to(self.outside / "secret.txt")
        (self.ws / "outdir").symlink_to(self.outside, target_is_directory=True)
        (self.ws / "inner.md").symlink_to(self.ws / "README.md")
        found = file_mention.discover_files(self.ws, "")
        self.assertNotIn("link.txt", found)
        self.assertFalse(any(rel.startswith("outdir/") for rel in found))
        self.assertIn("inner.md", found)

    def test_mention_query_parse(self) -> None:
        self.assertEqual(file_mention.mention_query("看看 @sr"), "sr")
        self.assertEqual(file_mention.mention_query("@"), "")
        self.assertIsNone(file_mention.mention_query("mail@host"))
        self.assertIsNone(file_mention.mention_query("@src/app.py 然后"))
        self.assertEqual(
            file_mention.replace_mention_tail("看看 @ap", "src/app.py"),
            "看看 @src/app.py ",
        )


class BoundaryTest(_WsCase):
    def test_dotdot_absolute_symlink_all_rejected(self) -> None:
        (self.ws / "link.txt").symlink_to(self.outside / "secret.txt")
        cases = {
            "../outside/secret.txt": "../",
            str(self.outside / "secret.txt"): "绝对路径",
            "link.txt": "工作区外",
        }
        for rel, words in cases.items():
            engine = _Engine()
            with self.assertRaises(MentionError) as ctx:
                file_mention.read_attachment(self.ws, rel, engine)
            self.assertIn(words, str(ctx.exception))
            self.assertEqual(engine.calls, [], "越界在门禁前就拒绝，不读")

    def test_missing_and_directory_rejected(self) -> None:
        with self.assertRaises(MentionError) as ctx:
            file_mention.read_attachment(self.ws, "nope.py", _Engine())
        self.assertIn("不存在", str(ctx.exception))
        with self.assertRaises(MentionError):
            file_mention.read_attachment(self.ws, "src", _Engine())


class GateTest(_WsCase):
    def test_deny_and_ask_rejected_without_reading(self) -> None:
        for verdict, words in (("deny", "被门禁拒绝"), ("ask", "需要确认")):
            engine = _Engine(verdict)
            with mock.patch.object(Path, "read_bytes", _no_open):
                with self.assertRaises(MentionError) as ctx:
                    file_mention.read_attachment(self.ws, "src/app.py", engine)
            self.assertIn(words, str(ctx.exception))
            self.assertIn("tier 3", str(ctx.exception))
            self.assertEqual(
                engine.calls,
                [("secretary", "read_file", str(self.ws / "src" / "app.py"))],
            )

    def test_real_engine_denies_secret_file(self) -> None:
        (self.ws / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
        with self.assertRaises(MentionError) as ctx:
            file_mention.read_attachment(self.ws, ".env")
        self.assertIn("被门禁拒绝", str(ctx.exception))
        self.assertNotIn("TOKEN=abc", str(ctx.exception))

    def test_real_engine_allows_workspace_file(self) -> None:
        att = file_mention.read_attachment(self.ws, "src/app.py")
        self.assertEqual(att.text, "print('hi')\n")
        self.assertEqual(att.rel, "src/app.py")


class LimitAndTypeTest(_WsCase):
    def test_size_limit(self) -> None:
        (self.ws / "ok.txt").write_bytes(b"a" * file_mention.MENTION_MAX_BYTES)
        (self.ws / "big.txt").write_bytes(b"a" * (file_mention.MENTION_MAX_BYTES + 1))
        self.assertEqual(
            file_mention.read_attachment(self.ws, "ok.txt", _Engine()).size,
            100 * 1024,
        )
        with self.assertRaises(MentionError) as ctx:
            file_mention.read_attachment(self.ws, "big.txt", _Engine())
        self.assertIn("100 KB", str(ctx.exception))

    def test_binary_rejected(self) -> None:
        (self.ws / "a.bin").write_bytes(b"ab\x00cd")
        (self.ws / "b.dat").write_bytes(b"\xff\xfe\xfa")
        for rel in ("a.bin", "b.dat"):
            with self.assertRaises(MentionError) as ctx:
                file_mention.read_attachment(self.ws, rel, _Engine())
            self.assertIn("二进制", str(ctx.exception))

    def test_note_and_block_show_name_and_size(self) -> None:
        att = file_mention.read_attachment(self.ws, "src/app.py", _Engine())
        self.assertEqual(file_mention.attachment_note(att), "已附文件：src/app.py（12 B）")
        msg = dispatcher.build_user_message("看一下", files=[att])
        texts = [getattr(b, "text", None) for b in msg.content]
        self.assertEqual(texts[0], "看一下")
        self.assertIn("[附件 src/app.py（12 B）]", texts[1])
        self.assertIn("print('hi')", texts[1])
        plain = dispatcher.build_user_message("看一下")
        self.assertEqual(len(plain.content), 1)


class RecheckTest(_WsCase):
    def _att(self):
        return file_mention.read_attachment(self.ws, "src/app.py", _Engine())

    def test_unchanged_passes(self) -> None:
        att = self._att()
        self.assertIs(file_mention.recheck_attachment(self.ws, att, _Engine()), att)

    def test_deleted_rejected(self) -> None:
        att = self._att()
        (self.ws / "src" / "app.py").unlink()
        with self.assertRaises(MentionError) as ctx:
            file_mention.recheck_attachment(self.ws, att, _Engine())
        self.assertIn("不存在", str(ctx.exception))

    def test_modified_size_or_mtime_rejected(self) -> None:
        path = self.ws / "src" / "app.py"
        att = self._att()
        path.write_text("print('changed!')\n", encoding="utf-8")
        with self.assertRaises(MentionError) as ctx:
            file_mention.recheck_attachment(self.ws, att, _Engine())
        self.assertIn("被改过", str(ctx.exception))
        att = self._att()
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        with self.assertRaises(MentionError):
            file_mention.recheck_attachment(self.ws, att, _Engine())

    def test_swapped_to_outside_symlink_rejected(self) -> None:
        path = self.ws / "src" / "app.py"
        att = self._att()
        path.unlink()
        path.symlink_to(self.outside / "secret.txt")
        with self.assertRaises(MentionError) as ctx:
            file_mention.recheck_attachment(self.ws, att, _Engine())
        self.assertIn("工作区外", str(ctx.exception))

    def test_gate_rechecked_at_send(self) -> None:
        att = self._att()
        with self.assertRaises(MentionError):
            file_mention.recheck_attachment(self.ws, att, _Engine("deny"))


class KeybindRegistryTest(unittest.TestCase):
    def test_no_new_entries_needed(self) -> None:
        # @ 补全复用 prompt_submit / prompt_complete / prompt_dismiss_suggestions。
        for name in ("prompt_submit", "prompt_complete", "prompt_dismiss_suggestions"):
            self.assertIsNotNone(keybinds.key_of(name))


def _app(ws: Path) -> CifixChatApp:
    model = FakeChatModel(fallback_reply="收到")
    app = CifixChatApp(model=model, workspace_root=ws)
    app.fake_model_for_test = model
    app._active_workspace = ws
    return app


def _lines(app: CifixChatApp) -> list[str]:
    return [str(text) for _, text, _ in app.history]


class MentionAppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve() / "ws"
        (self.ws / "src").mkdir(parents=True)
        (self.ws / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.ws / "README.md").write_text("# 说明\n", encoding="utf-8")
        (self.ws / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

    async def _prompt(self, app, pilot) -> PromptInput:
        await pilot.pause()
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        return prompt

    async def _type(self, pilot, prompt: PromptInput, text: str) -> None:
        prompt.text = text
        prompt.cursor_location = (0, len(text))
        await pilot.pause()

    async def test_popup_filter_and_escape(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "看 @app")
            box = prompt.box
            self.assertTrue(box.mention_mode)
            self.assertEqual([i[0] for i in box._suggestion_items], ["src/app.py"])
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(box.suggestions_visible)
            self.assertEqual(prompt.text, "看 @app")
            self.assertIsNone(app._mention_attachment)

    async def test_no_match_one_line_hint(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "@zzzqqq")
            self.assertFalse(prompt.box.suggestions_visible)
            self.assertIn(
                file_mention.NO_MATCH_TEXT, str(prompt.box._suggest.render())
            )

    async def test_enter_selects_then_send_attaches(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "看看 @app")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(prompt.text, "看看 @src/app.py ")
            self.assertEqual(app._mention_attachment.rel, "src/app.py")
            with mock.patch(
                "core.dispatcher.build_user_message",
                wraps=dispatcher.build_user_message,
            ) as built:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
            files = built.call_args.kwargs.get("files")
            self.assertEqual([f.rel for f in files], ["src/app.py"])
            self.assertIn("已附文件：src/app.py（12 B）", _lines(app))
            self.assertIsNone(app._mention_attachment)

    async def test_gate_denied_selection_not_inserted(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "@.en")
            self.assertTrue(app.select_mention(prompt, ".env") is False)
            await pilot.pause()
            self.assertEqual(prompt.text, "@.en")
            self.assertIsNone(app._mention_attachment)
            self.assertTrue(any("被门禁拒绝" in line for line in _lines(app)))
            self.assertFalse(any("TOKEN=abc" in line for line in _lines(app)))

    async def test_deleted_before_send_not_sent(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "看看 @app")
            await pilot.press("enter")
            await pilot.pause()
            (self.ws / "src" / "app.py").unlink()
            calls_before = len(app.fake_model_for_test.calls)
            with mock.patch(
                "core.dispatcher.build_user_message",
                wraps=dispatcher.build_user_message,
            ) as built:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
            built.assert_not_called()
            self.assertEqual(len(app.fake_model_for_test.calls), calls_before)
            self.assertTrue(any("不存在" in line for line in _lines(app)))
            await pilot.pause()
            current = app.screen.query_one(PromptInput)
            self.assertEqual(current.text, "看看 @src/app.py")
            self.assertFalse(app._agent_busy)

    async def test_modified_before_send_not_sent(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "@app")
            await pilot.press("enter")
            await pilot.pause()
            (self.ws / "src" / "app.py").write_text("print('changed!')\n", encoding="utf-8")
            with mock.patch(
                "core.dispatcher.build_user_message",
                wraps=dispatcher.build_user_message,
            ) as built:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
            built.assert_not_called()
            self.assertTrue(any("被改过" in line for line in _lines(app)))

    async def test_mention_removed_sends_plain(self) -> None:
        app = _app(self.ws)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "@app")
            await pilot.press("enter")
            await pilot.pause()
            await self._type(pilot, prompt, "算了不附了")
            with mock.patch(
                "core.dispatcher.build_user_message",
                wraps=dispatcher.build_user_message,
            ) as built:
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
            self.assertIsNone(built.call_args.kwargs.get("files"))
            self.assertFalse(any("已附文件" in line for line in _lines(app)))


if __name__ == "__main__":
    unittest.main()
