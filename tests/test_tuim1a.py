# -*- coding: utf-8 -*-
"""TUIM1a：三层模型语义、截图式选择器、ctrl+t、单次实际。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import ChunkedFakeChatModel, FakeChatModel
from core import model_config
from core.model_config import ModelProfile
from core.model_state import (
    CUSTOM_EFFORT_NOTICE,
    MAX_EFFORT_WARNING,
    SESSION_MARKER,
    CustomModelError,
    ModelTriple,
    SessionModelState,
    cycle_effort,
    session_effort_line,
)
from core.tui import CifixChatApp, ModelPicker, PromptInput
from textual.widgets import OptionList, Static

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def _write_config(data: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "model_config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _flash_config(**extra: object) -> dict:
    data: dict = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key_env": "TUIM1A_KEY",
        "active_profile": "default",
    }
    data.update(extra)
    return data


class ResolveEffectiveTest(unittest.TestCase):
    def test_no_override_uses_user_default(self) -> None:
        path = _write_config(_flash_config(reasoning_effort="high"))
        state = SessionModelState()
        self.assertEqual(
            state.effective(path),
            ModelTriple("deepseek", "deepseek-flash", "high"),
        )
        self.assertFalse(state.active)

    def test_override_wins_and_file_bytes_stay(self) -> None:
        path = _write_config(_flash_config(reasoning_effort="high"))
        before = path.read_bytes()
        state = SessionModelState()
        state.set_override(ModelTriple("deepseek", "deepseek-v4-pro", "max"))
        got = state.effective(path)
        self.assertEqual(got.model, "deepseek-v4-pro")
        self.assertEqual(got.effort, "max")
        self.assertEqual(path.read_bytes(), before)
        state.clear()
        self.assertEqual(state.effective(path).model, "deepseek-flash")
        self.assertEqual(path.read_bytes(), before)

    def test_cycle_includes_default_and_wraps(self) -> None:
        self.assertEqual(cycle_effort("deepseek", "deepseek-flash", None), "high")
        self.assertEqual(cycle_effort("deepseek", "deepseek-flash", "high"), "max")
        self.assertIsNone(cycle_effort("deepseek", "deepseek-flash", "max"))
        self.assertEqual(
            cycle_effort("deepseek", "deepseek-flash", None, step=-1),
            "max",
        )

    def test_custom_model_refuses_cycle(self) -> None:
        with self.assertRaises(CustomModelError):
            cycle_effort("deepseek", "my-private-model", None)
        self.assertEqual(
            CUSTOM_EFFORT_NOTICE,
            "该模型未在目录中，思考等级不可调",
        )
        self.assertEqual(
            MAX_EFFORT_WARNING,
            "max 档更费 token、响应更慢、可能想过头，难任务再用。",
        )
        self.assertEqual(session_effort_line("max"), "思考等级：max（仅本次会话）")
        self.assertEqual(session_effort_line(None), "思考等级：默认（仅本次会话）")


class SessionIsolationTest(unittest.TestCase):
    def test_new_app_does_not_keep_another_apps_override(self) -> None:
        one = CifixChatApp(model=FakeChatModel(fallback_reply="x"))
        one._model_state.set_override(
            ModelTriple("deepseek", "deepseek-v4-pro", "max")
        )
        two = CifixChatApp(model=FakeChatModel(fallback_reply="x"))
        self.assertIsNone(two._model_state.override)

    def test_status_marks_session_override(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="x"))
        plain = app.status_line().plain
        self.assertIn("fake-chat-model", plain)
        self.assertNotIn(SESSION_MARKER, plain)
        app._model_state.set_override(
            ModelTriple("deepseek", "deepseek-v4-pro", "max")
        )
        marked = app.status_line().plain
        self.assertIn("deepseek-v4-pro", marked)
        self.assertIn(f"思考 max {SESSION_MARKER}", marked)
        self.assertNotIn("fake-chat-model", marked)


def _config_patches(config_path: Path, user_path: Path):
    return (
        mock.patch.object(model_config, "DEFAULT_CONFIG_PATH", config_path),
        mock.patch.object(model_config, "user_config_path", lambda: user_path),
        mock.patch.object(
            model_config, "user_env_path", lambda: user_path.parent / ".env"
        ),
    )


class _PickerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config_path = self.tmp / "model_config.json"
        self.user_path = self.tmp / "user.json"
        self.config_path.write_text(
            json.dumps(_flash_config()),
            encoding="utf-8",
        )
        self.catalog = mock.Mock()
        self.catalog.active = "default"
        self.catalog.profiles = [
            ModelProfile(
                name="default",
                provider="deepseek",
                model="deepseek-flash",
                api_key_env="TUIM1A_KEY",
            ),
            ModelProfile(
                name="work",
                provider="deepseek",
                model="deepseek-chat",
                api_key_env="TUIM1A_WORK_KEY",
            ),
        ]

    def _patches(self):
        default_patch, user_patch, env_patch = _config_patches(
            self.config_path, self.user_path
        )
        return default_patch, user_patch, env_patch

    async def _open(self, pilot, app) -> ModelPicker:
        picker = ModelPicker(app, self.catalog)
        app.push_screen(picker)
        await pilot.pause()
        return picker

    def _ids(self, picker: ModelPicker) -> list[str]:
        option_list = picker.query_one("#model-list", OptionList)
        return [
            str(option_list.get_option_at_index(i).id)
            for i in range(option_list.option_count)
        ]

    def _highlight(self, picker: ModelPicker, oid: str) -> None:
        option_list = picker.query_one("#model-list", OptionList)
        option_list.highlighted = self._ids(picker).index(oid)


class PickerInteractionTest(_PickerCase):
    async def test_left_right_status_and_max_warning(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch:
            async with app.run_test(size=(110, 36)) as pilot:
                picker = await self._open(pilot, app)
                self._highlight(picker, "model:deepseek-v4-pro")
                await pilot.pause()
                status = picker.query_one("#model-effort-status", Static)
                warning = picker.query_one("#model-max-warning", Static)
                self.assertIn("思考等级：默认", str(status.content))
                self.assertFalse(warning.display)

                await pilot.press("right")
                await pilot.pause()
                self.assertIn("思考等级：high", str(status.content))
                self.assertFalse(warning.display)

                await pilot.press("right")
                await pilot.pause()
                self.assertIn("思考等级：max", str(status.content))
                self.assertTrue(warning.display)
                self.assertEqual(str(warning.content), MAX_EFFORT_WARNING)

                await pilot.press("right")
                await pilot.pause()
                self.assertIn("思考等级：默认", str(status.content))
                self.assertFalse(warning.display)

                await pilot.press("left")
                await pilot.pause()
                self.assertIn("思考等级：max", str(status.content))
                self.assertTrue(warning.display)

    async def test_enter_persists_and_clears_override(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )
        built = FakeChatModel(fallback_reply="switched", model_name="deepseek-v4-pro")
        before = self.config_path.read_bytes()
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "key_is_set", return_value=True
        ), mock.patch.object(
            model_config, "build_model", return_value=built
        ):
            async with app.run_test(size=(110, 36)) as pilot:
                picker = await self._open(pilot, app)
                self._highlight(picker, "model:deepseek-v4-pro")
                await pilot.pause()
                await pilot.press("right")
                await pilot.press("right")
                await pilot.press("enter")
                await pilot.pause()
                self.assertNotEqual(app.screen.id, "model-picker")
        data = json.loads(self.user_path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertEqual(data["reasoning_effort"], "max")
        # 平台配置文件不被会话外的落盘改写；用户文件才是落盘目标。
        self.assertEqual(self.config_path.read_bytes(), before)
        # 落盘路径清掉会话覆盖，否则覆盖会把新默认盖住。
        self.assertIsNone(app._model_state.override)
        self.assertNotIn(SESSION_MARKER, app.status_line().plain)
        self.assertIn("思考 max", app.status_line().plain)

    async def test_s_does_not_touch_config_bytes(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )
        built = FakeChatModel(fallback_reply="session", model_name="deepseek-v4-pro")
        before_config = self.config_path.read_bytes()
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "key_is_set", return_value=True
        ), mock.patch.object(
            model_config, "build_model", return_value=built
        ):
            async with app.run_test(size=(110, 36)) as pilot:
                picker = await self._open(pilot, app)
                self._highlight(picker, "model:deepseek-v4-pro")
                await pilot.pause()
                await pilot.press("right")
                await pilot.press("s")
                await pilot.pause()
        self.assertEqual(self.config_path.read_bytes(), before_config)
        self.assertFalse(self.user_path.exists())
        self.assertEqual(
            app._model_state.override,
            ModelTriple("deepseek", "deepseek-v4-pro", "high"),
        )
        self.assertIn(SESSION_MARKER, app.status_line().plain)
        self.assertIn("思考 high", app.status_line().plain)

    async def test_profile_row_arrows_and_s_are_inert(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )
        before = self.config_path.read_bytes()
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "build_model"
        ) as build_mock:
            async with app.run_test(size=(110, 36)) as pilot:
                picker = await self._open(pilot, app)
                self._highlight(picker, "model:deepseek-flash")
                await pilot.pause()
                await pilot.press("right")
                await pilot.pause()
                self.assertEqual(picker._efforts.get("deepseek-flash"), "high")
                self._highlight(picker, "work")
                await pilot.pause()
                warning = picker.query_one("#model-max-warning", Static)
                self.assertFalse(warning.display)
                await pilot.press("left")
                await pilot.press("right")
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(app.screen.id, "model-picker")
                self.assertEqual(picker._efforts.get("deepseek-flash"), "high")
                self.assertIsNone(app._model_state.override)
                build_mock.assert_not_called()
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.user_path.exists())


class CtrlTTest(_PickerCase):
    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )

    async def test_ctrl_t_cycles_logs_and_wraps_without_writing(self) -> None:
        app = self._app()
        before = self.config_path.read_bytes()
        built = FakeChatModel(fallback_reply="cycled")
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "key_is_set", return_value=True
        ), mock.patch.object(
            model_config, "build_model", return_value=built
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+t")
                await pilot.pause()
                await pilot.press("ctrl+t")
                await pilot.pause()
                await pilot.press("ctrl+t")
                await pilot.pause()
                await pilot.press("ctrl+t")
                await pilot.pause()
        texts = [text for kind, text, _ in app.history if kind == "event"]
        self.assertIn(session_effort_line("high"), texts)
        self.assertIn(session_effort_line("max"), texts)
        self.assertIn(session_effort_line(None), texts)
        # 第四下回到 high，循环而不是停在默认。
        self.assertEqual(
            [text for text in texts if text.startswith("思考等级：")],
            [
                session_effort_line("high"),
                session_effort_line("max"),
                session_effort_line(None),
                session_effort_line("high"),
            ],
        )
        self.assertEqual(
            app._model_state.override,
            ModelTriple("deepseek", "deepseek-flash", "high"),
        )
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.user_path.exists())
        self.assertIn(SESSION_MARKER, app.status_line().plain)

    async def test_ctrl_t_custom_model_changes_nothing(self) -> None:
        self.config_path.write_text(
            json.dumps(_flash_config(model="my-private-model")),
            encoding="utf-8",
        )
        before = self.config_path.read_bytes()
        app = self._app()
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "build_model"
        ) as build_mock, mock.patch.object(
            model_config, "update_active_model"
        ) as update_mock, mock.patch.object(
            model_config, "activate_role_profile"
        ) as activate_mock:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await pilot.press("ctrl+t")
                await pilot.pause()
        texts = [text for _, text, _ in app.history]
        self.assertIn(CUSTOM_EFFORT_NOTICE, texts)
        self.assertIsNone(app._model_state.override)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.user_path.exists())
        build_mock.assert_not_called()
        update_mock.assert_not_called()
        activate_mock.assert_not_called()
        self.assertNotIn(SESSION_MARKER, app.status_line().plain)

    async def test_unwired_provider_does_not_silently_swap(self) -> None:
        app = self._app()
        default_patch, user_patch, env_patch = self._patches()
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "key_is_set", return_value=True
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                old = app._dispatcher
                ok = app._commit_session("not-a-provider", "other-model", None)
                self.assertFalse(ok)
                self.assertIs(app._dispatcher, old)
                self.assertIsNone(app._model_state.override)
                self.assertIsNone(app._active_model)


class ActualCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_history_actual_matches_effective_not_status_label(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        config_path = tmp / "model_config.json"
        user_path = tmp / "user.json"
        config_path.write_text(json.dumps(_flash_config()), encoding="utf-8")
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="假回复", model_name="fake-chat-model"),
            workspace_root=PLATFORM_ROOT,
        )
        default_patch, user_patch, env_patch = _config_patches(config_path, user_path)
        with default_patch, user_patch, env_patch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                # 状态行在无覆盖时仍显示注入替身名；元数据不许跟它走。
                self.assertIn("fake-chat-model", app.status_line().plain)
                prompt = app.screen.query_one(PromptInput)
                prompt.focus()
                prompt.text = "你好"
                await pilot.pause()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                expected = app._model_state.effective().as_tuple()
            assistants = [
                record for record in app.history if record[0] == "assistant"
            ]
            self.assertEqual(len(assistants), 1)
            self.assertEqual(assistants[0].actual, expected)
            self.assertEqual(expected, ("deepseek", "deepseek-flash", None))
            self.assertNotEqual(assistants[0].actual[1], "fake-chat-model")

    async def test_inflight_request_keeps_snapshot_after_switch(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        config_path = tmp / "model_config.json"
        user_path = tmp / "user.json"
        config_path.write_text(
            json.dumps(_flash_config(reasoning_effort="high")),
            encoding="utf-8",
        )
        before = config_path.read_bytes()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gate(index: int) -> None:
            if index == 0:
                entered.set()
            await release.wait()

        first = ChunkedFakeChatModel(["第一轮。"], gate=gate, output_tokens=1)
        second = FakeChatModel(fallback_reply="第二轮", model_name="after-switch")
        app = CifixChatApp(model=first, workspace_root=PLATFORM_ROOT)
        default_patch, user_patch, env_patch = _config_patches(config_path, user_path)
        with default_patch, user_patch, env_patch, mock.patch.object(
            model_config, "key_is_set", return_value=True
        ), mock.patch.object(
            model_config, "build_model", return_value=second
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                prompt = app.screen.query_one(PromptInput)
                prompt.focus()
                prompt.text = "第一条"
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(50):
                    if entered.is_set():
                        break
                    await pilot.pause()
                self.assertTrue(entered.is_set(), "模型调用没有进门控")
                started = app._model_state.effective().as_tuple()
                self.assertTrue(
                    app._commit_session("deepseek", "deepseek-flash", "max")
                )
                self.assertEqual(
                    app._model_state.effective().as_tuple(),
                    ("deepseek", "deepseek-flash", "max"),
                )
                release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()

                # 第一条从首页发出后已经切到会话页。首页那个输入框不再接收按键。
                prompt = app.screen.query_one(PromptInput)
                prompt.focus()
                prompt.text = "第二条"
                await pilot.pause()
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()

        assistants = [
            record for record in app.history if record[0] == "assistant"
        ]
        self.assertEqual([record[1] for record in assistants], ["第一轮。", "第二轮"])
        self.assertEqual(assistants[0].actual, started)
        self.assertEqual(assistants[0].actual[2], "high")
        self.assertEqual(assistants[1].actual, ("deepseek", "deepseek-flash", "max"))
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 1)
        self.assertEqual(config_path.read_bytes(), before)
        self.assertFalse(user_path.exists())


if __name__ == "__main__":
    unittest.main()
