# -*- coding: utf-8 -*-
"""TUIK1：键位注册表是唯一来源，覆盖校验拒绝非法项并保留默认键。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.binding import Binding

from core import keybinds
from core.tui import (
    CifixChatApp,
    CommandPalette,
    ConfirmModal,
    ModelPicker,
    SetupScreen,
    WorkspaceTrustScreen,
)


def _by_id(entries):
    return {entry.id: entry for entry in entries}


class KeybindRegistryTests(unittest.TestCase):
    def test_scope_priority_is_annotated_not_dispatched(self) -> None:
        self.assertEqual(
            keybinds.SCOPE_PRIORITY,
            ("modal", "input", "page", "global"),
        )
        self.assertEqual(set(keybinds.SCOPE_PRIORITY), set(keybinds.SCOPES))

    def test_default_table_has_no_same_scope_conflict(self) -> None:
        self.assertEqual(keybinds.find_conflicts(keybinds.DEFAULTS), ())
        entries, diagnostics = keybinds.load_registry(overrides={})
        self.assertEqual(entries, keybinds.DEFAULTS)
        self.assertEqual(diagnostics, ())
        # 五个弹窗都是 modal + escape，但分属不同 screen，不互相抢键。
        escapes = [
            entry
            for entry in keybinds.DEFAULTS
            if entry.scope == "modal" and entry.key == "escape" and entry.bind
        ]
        self.assertEqual(
            {entry.id for entry in escapes},
            {
                "palette_close",
                "setup_quit",
                "trust_deny",
                "picker_close",
                "confirm_deny",
            },
        )
        self.assertEqual(len({entry.screen for entry in escapes}), 5)

    def test_defaults_pin_every_existing_key(self) -> None:
        expected = {
            "quit": ("ctrl+c", "global", "quit", None, True, "退出"),
            # COPY1：复制上一条回复，默认不绑（key 为 None），老板可自绑。
            "copy_last_reply": (
                None,
                "global",
                "copy_last_reply",
                None,
                False,
                "复制回复",
            ),
            "open_commands": (
                "ctrl+p",
                "global",
                "open_commands",
                None,
                True,
                "命令面板",
            ),
            "cycle_effort": (
                "ctrl+t",
                "global",
                "cycle_effort",
                None,
                True,
                "轮换思考等级",
            ),
            "interrupt": ("escape", "global", "interrupt", None, False, "中断"),
            "palette_close": (
                "escape",
                "modal",
                "close_palette",
                "palette",
                False,
                "关闭",
            ),
            "setup_quit": (
                "escape",
                "modal",
                "quit_setup",
                "setup-screen",
                False,
                "退出",
            ),
            "trust_deny": (
                "escape",
                "modal",
                "deny",
                "workspace-trust",
                False,
                "拒绝",
            ),
            "picker_close": (
                "escape",
                "modal",
                "close_picker",
                "model-picker",
                False,
                "关闭",
            ),
            "picker_effort_prev": (
                "left",
                "modal",
                "picker_effort_prev",
                "model-picker",
                True,
                "上一档",
            ),
            "picker_effort_next": (
                "right",
                "modal",
                "picker_effort_next",
                "model-picker",
                True,
                "下一档",
            ),
            "picker_session": (
                "s",
                "modal",
                "picker_session",
                "model-picker",
                True,
                "仅本次会话",
            ),
            "confirm_deny": (
                "escape",
                "modal",
                "deny",
                "confirm-modal",
                False,
                "拒绝",
            ),
            "picker_cursor_up": ("up", "input", "", "model-picker", False, ""),
            "picker_cursor_down": ("down", "input", "", "model-picker", False, ""),
            "prompt_dismiss_suggestions": (
                "escape",
                "input",
                "",
                "prompt-input",
                False,
                "",
            ),
            "prompt_up": ("up", "input", "", "prompt-input", False, ""),
            "prompt_down": ("down", "input", "", "prompt-input", False, ""),
            "prompt_complete": ("tab", "input", "", "prompt-input", False, ""),
            "prompt_submit": ("enter", "input", "", "prompt-input", False, ""),
            "prompt_newline": (
                "shift+enter",
                "input",
                "",
                "prompt-input",
                False,
                "",
            ),
            "confirm_yes": ("y", "modal", "", "confirm-modal", False, ""),
            "confirm_enter": ("enter", "modal", "", "confirm-modal", False, ""),
            "confirm_always": ("a", "modal", "", "confirm-modal", False, ""),
            "confirm_preview": ("p", "modal", "", "confirm-modal", False, ""),
            "confirm_no": ("n", "modal", "", "confirm-modal", False, ""),
            # TUID1：草稿暂存 / 粘贴块查看（on_key 处理，不进 BINDINGS）。
            "stash_draft": ("ctrl+s", "input", "", "prompt-input", False, ""),
            "paste_view": ("ctrl+o", "input", "", "prompt-input", False, ""),
            "paste_view_close": ("escape", "modal", "", "paste-view", False, ""),
            # TUIE1：/status、/mcp 弹窗关闭键（on_key 处理，不进 BINDINGS）。
            "status_view_close": ("escape", "modal", "", "status-view", False, ""),
            "mcp_view_close": ("escape", "modal", "", "mcp-view", False, ""),
            # TUIE2：/help 弹窗关闭键、错误详情（页面作用域装进 BINDINGS）与其关闭键。
            "help_view_close": ("escape", "modal", "", "help-view", False, ""),
            "error_view_close": ("escape", "modal", "", "error-view", False, ""),
            # TUIC2：消息跳转弹窗关闭键。
            "timeline_close": ("escape", "modal", "", "timeline-view", False, ""),
            "error_view": (
                "ctrl+r",
                "page",
                "open_error_view",
                None,
                False,
                "错误详情",
            ),
            # TUIC1：侧边栏开关与思考块展开/收起（页面作用域）。
            "sidebar_toggle": (
                "ctrl+b",
                "page",
                "toggle_sidebar",
                None,
                False,
                "侧边栏",
            ),
            "thought_toggle": (
                "ctrl+n",
                "page",
                "toggle_thoughts",
                None,
                False,
                "思考块",
            ),
            # BLK1：按块展开定位到的那条思考块（页面作用域）。
            "thought_block_toggle": (
                "ctrl+f",
                "page",
                "toggle_thought_block",
                None,
                False,
                "单块思考",
            ),
            # BLK2：按块展开定位到的那条工具块（页面作用域）。
            "tool_block_toggle": (
                "f8",
                "page",
                "toggle_tool_block",
                None,
                False,
                "单块工具",
            ),
            # TUIC2：消息跳转（页面作用域）。
            "timeline": (
                "ctrl+l",
                "page",
                "open_timeline",
                None,
                False,
                "消息跳转",
            ),
        }
        pinned = {
            entry.id: (
                entry.key,
                entry.scope,
                entry.action,
                entry.screen,
                entry.priority,
                entry.binding_description,
            )
            for entry in keybinds.DEFAULTS
        }
        self.assertEqual(pinned, expected)
        self.assertTrue(all(entry.show is False for entry in keybinds.DEFAULTS))
        self.assertEqual(
            keybinds.bindings_for("global", keybinds.DEFAULTS),
            [
                Binding("ctrl+c", "quit", "退出", priority=True, show=False),
                Binding(
                    "ctrl+p",
                    "open_commands",
                    "命令面板",
                    priority=True,
                    show=False,
                ),
                Binding(
                    "ctrl+t",
                    "cycle_effort",
                    "轮换思考等级",
                    priority=True,
                    show=False,
                ),
                Binding("escape", "interrupt", "中断", show=False),
            ],
        )
        self.assertEqual(
            keybinds.bindings_for("palette", keybinds.DEFAULTS),
            [Binding("escape", "close_palette", "关闭", show=False)],
        )
        self.assertEqual(
            keybinds.bindings_for("setup-screen", keybinds.DEFAULTS),
            [Binding("escape", "quit_setup", "退出", show=False)],
        )
        self.assertEqual(
            keybinds.bindings_for("workspace-trust", keybinds.DEFAULTS),
            [Binding("escape", "deny", "拒绝", show=False)],
        )
        self.assertEqual(
            keybinds.bindings_for("model-picker", keybinds.DEFAULTS),
            [
                Binding("escape", "close_picker", "关闭", show=False),
                Binding(
                    "left",
                    "picker_effort_prev",
                    "上一档",
                    priority=True,
                    show=False,
                ),
                Binding(
                    "right",
                    "picker_effort_next",
                    "下一档",
                    priority=True,
                    show=False,
                ),
                Binding(
                    "s",
                    "picker_session",
                    "仅本次会话",
                    priority=True,
                    show=False,
                ),
            ],
        )
        self.assertEqual(
            keybinds.bindings_for("confirm-modal", keybinds.DEFAULTS),
            [Binding("escape", "deny", "拒绝", show=False)],
        )
        # 选择器 ↑/↓ 与输入框键只登记，不装进 BINDINGS。
        self.assertEqual(keybinds.bindings_for("input", keybinds.DEFAULTS), [])
        # TUIE2 + TUIC1 + TUIC2：页面作用域四条，装进 Home/Session 的 BINDINGS。
        self.assertEqual(
            keybinds.bindings_for("page", keybinds.DEFAULTS),
            [
                Binding("ctrl+r", "open_error_view", "错误详情", show=False),
                Binding("ctrl+b", "toggle_sidebar", "侧边栏", show=False),
                Binding("ctrl+n", "toggle_thoughts", "思考块", show=False),
                Binding(
                    "ctrl+f", "toggle_thought_block", "单块思考", show=False
                ),
                Binding("f8", "toggle_tool_block", "单块工具", show=False),
                Binding("ctrl+l", "open_timeline", "消息跳转", show=False),
            ],
        )
        self.assertEqual(
            keybinds.bindings_for("prompt-input", keybinds.DEFAULTS), []
        )

    def test_describe_reads_the_same_registry(self) -> None:
        entries, _ = keybinds.load_registry(
            overrides={"cycle_effort": "f2", "open_commands": None}
        )
        rows = keybinds.describe(entries)
        self.assertEqual(len(rows), len(entries))
        self.assertEqual(
            [(entry.key, entry.description, entry.scope) for entry in entries],
            rows,
        )
        by_description = {description: key for key, description, _scope in rows}
        self.assertEqual(
            by_description["轮换当前模型的思考等级（仅本次会话）"], "f2"
        )
        self.assertIsNone(by_description["打开命令面板"])
        self.assertIn(("ctrl+c", "退出应用", "global"), rows)
        binding_keys = {
            binding.key for binding in keybinds.bindings_for("global", entries)
        }
        self.assertIn("f2", binding_keys)
        self.assertNotIn("ctrl+p", binding_keys)

    def test_rebind_applies_and_keeps_priority(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"cycle_effort": "f2", "quit": "F12"}
        )
        self.assertEqual(diagnostics, ())
        table = _by_id(entries)
        self.assertEqual(table["cycle_effort"].key, "f2")
        self.assertEqual(table["quit"].key, "f12")
        self.assertTrue(table["quit"].priority)
        self.assertFalse(table["quit"].show)
        bindings = keybinds.bindings_for("global", entries)
        quit_binding = next(item for item in bindings if item.action == "quit")
        self.assertEqual(quit_binding.key, "f12")
        self.assertTrue(quit_binding.priority)
        self.assertFalse(quit_binding.show)
        self.assertEqual(quit_binding.description, "退出")

    def test_null_disables_without_dropping_other_defaults(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"open_commands": None}
        )
        self.assertEqual(diagnostics, ())
        table = _by_id(entries)
        self.assertIsNone(table["open_commands"].key)
        self.assertEqual(table["quit"].key, "ctrl+c")
        actions = [
            binding.action
            for binding in keybinds.bindings_for("global", entries)
        ]
        self.assertEqual(
            actions, ["quit", "cycle_effort", "interrupt"]
        )
        self.assertIsNone(keybinds.key_of("open_commands", entries))

    def test_unknown_id_rejected(self) -> None:
        entries, diagnostics = keybinds.load_registry(overrides={"nope": "f1"})
        self.assertEqual(entries, keybinds.DEFAULTS)
        self.assertEqual(diagnostics, ("未知 action_id 'nope'，已拒绝",))

    def test_conflict_override_rejected_and_default_kept(self) -> None:
        entries, diagnostics = keybinds.load_registry(overrides={"quit": "ctrl+p"})
        table = _by_id(entries)
        self.assertEqual(table["quit"].key, "ctrl+c")
        self.assertEqual(table["open_commands"].key, "ctrl+p")
        self.assertTrue(diagnostics)
        self.assertIn("冲突", diagnostics[0])
        self.assertIn("保留默认键", diagnostics[0])
        self.assertIn("ctrl+c", diagnostics[0])
        bindings = keybinds.bindings_for("global", entries)
        self.assertEqual(
            [(item.key, item.action) for item in bindings],
            [
                ("ctrl+c", "quit"),
                ("ctrl+p", "open_commands"),
                ("ctrl+t", "cycle_effort"),
                ("escape", "interrupt"),
            ],
        )

    def test_two_overrides_onto_one_key_both_rejected(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"quit": "f1", "open_commands": "f1"}
        )
        table = _by_id(entries)
        self.assertEqual(table["quit"].key, "ctrl+c")
        self.assertEqual(table["open_commands"].key, "ctrl+p")
        self.assertTrue(any("冲突" in item for item in diagnostics))

    def test_swap_has_no_conflict(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"quit": "ctrl+p", "open_commands": "ctrl+c"}
        )
        table = _by_id(entries)
        self.assertEqual(diagnostics, ())
        self.assertEqual(table["quit"].key, "ctrl+p")
        self.assertEqual(table["open_commands"].key, "ctrl+c")

    def test_cascade_does_not_invalidate_a_default(self) -> None:
        # open_commands 让到 cycle_effort 的键（被拒），quit 再占 ctrl+p 也必须被拒。
        entries, diagnostics = keybinds.load_registry(
            overrides={"quit": "ctrl+p", "open_commands": "ctrl+t"}
        )
        table = _by_id(entries)
        self.assertEqual(table["quit"].key, "ctrl+c")
        self.assertEqual(table["open_commands"].key, "ctrl+p")
        self.assertEqual(table["cycle_effort"].key, "ctrl+t")
        self.assertGreaterEqual(len(diagnostics), 2)

    def test_illegal_key_rejected(self) -> None:
        for bad in ("bananas", "not a key", "esc", "ctrl+", "", "ctrl+ctrl+c"):
            entries, diagnostics = keybinds.load_registry(overrides={"quit": bad})
            self.assertEqual(
                _by_id(entries)["quit"].key,
                "ctrl+c",
                bad,
            )
            self.assertTrue(diagnostics, bad)
            self.assertIn("非法", diagnostics[0])
            self.assertIn("保留默认键", diagnostics[0])
        entries, diagnostics = keybinds.load_registry(overrides={"quit": 1})
        self.assertEqual(_by_id(entries)["quit"].key, "ctrl+c")
        self.assertIn("null", diagnostics[0])
        entries, diagnostics = keybinds.load_registry(
            overrides={"open_commands": False}
        )
        self.assertEqual(_by_id(entries)["open_commands"].key, "ctrl+p")
        self.assertTrue(diagnostics)

    def test_cross_screen_same_key_is_not_a_conflict(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"palette_close": "f1", "setup_quit": "f1"}
        )
        self.assertEqual(diagnostics, ())
        table = _by_id(entries)
        self.assertEqual(table["palette_close"].key, "f1")
        self.assertEqual(table["setup_quit"].key, "f1")
        self.assertEqual(
            [item.key for item in keybinds.bindings_for("palette", entries)],
            ["f1"],
        )
        self.assertEqual(
            [item.key for item in keybinds.bindings_for("setup-screen", entries)],
            ["f1"],
        )

    def test_input_scope_conflict_keeps_picker_default(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"picker_cursor_up": "down"}
        )
        table = _by_id(entries)
        self.assertEqual(table["picker_cursor_up"].key, "up")
        self.assertEqual(table["picker_cursor_down"].key, "down")
        self.assertTrue(any("input:model-picker" in item for item in diagnostics))

    def test_override_file_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keybinds.json"
            path.write_text(
                json.dumps(
                    {
                        "cycle_effort": "f2",
                        "open_commands": None,
                        "nope": "f1",
                        "quit": "bananas",
                    }
                ),
                encoding="utf-8",
            )
            entries, diagnostics = keybinds.load_registry(path)
            missing, missing_diags = keybinds.load_registry(
                Path(tmp) / "missing.json"
            )
            Path(tmp, "bad.json").write_text("{", encoding="utf-8")
            broken, broken_diags = keybinds.load_registry(Path(tmp) / "bad.json")
            Path(tmp, "list.json").write_text("[]", encoding="utf-8")
            listed, listed_diags = keybinds.load_registry(Path(tmp) / "list.json")
        table = _by_id(entries)
        self.assertEqual(table["cycle_effort"].key, "f2")
        self.assertIsNone(table["open_commands"].key)
        self.assertEqual(table["quit"].key, "ctrl+c")
        joined = "\n".join(diagnostics)
        self.assertIn("nope", joined)
        self.assertIn("bananas", joined)
        self.assertEqual(missing, keybinds.DEFAULTS)
        self.assertEqual(missing_diags, ())
        self.assertEqual(broken, keybinds.DEFAULTS)
        self.assertTrue(any("JSON" in item for item in broken_diags))
        self.assertEqual(listed, keybinds.DEFAULTS)
        self.assertTrue(listed_diags)

    def test_default_path_is_user_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "core.keybinds.user_config_dir", return_value=Path(tmp)
            ):
                self.assertEqual(
                    keybinds.keybinds_path(), Path(tmp) / "keybinds.json"
                )
                entries, diagnostics = keybinds.load_registry()
        self.assertEqual(entries, keybinds.DEFAULTS)
        self.assertEqual(diagnostics, ())

    def test_active_registry_loads_once(self) -> None:
        self.assertIs(keybinds.active_registry(), keybinds.active_registry())


class InstalledBindingTests(unittest.TestCase):
    def test_app_and_screens_match_registry(self) -> None:
        host = object()
        instances = {
            "palette": CommandPalette(),
            "setup-screen": SetupScreen(host),
            "workspace-trust": WorkspaceTrustScreen(host, Path("/tmp/tuik1")),
            "model-picker": ModelPicker(host, object()),
            "confirm-modal": ConfirmModal(
                host, lines=["确认"], preview_lines=None
            ),
        }
        classes = {
            "global": CifixChatApp,
            "palette": CommandPalette,
            "setup-screen": SetupScreen,
            "workspace-trust": WorkspaceTrustScreen,
            "model-picker": ModelPicker,
            "confirm-modal": ConfirmModal,
        }
        for screen_id, cls in classes.items():
            expected = keybinds.bindings_for(screen_id)
            self.assertIn("BINDINGS", cls.__dict__, cls.__name__)
            self.assertEqual(list(cls.BINDINGS), expected, cls.__name__)
            self.assertEqual(
                {binding.key for binding in cls.BINDINGS},
                {binding.key for binding in expected},
                cls.__name__,
            )
        for screen_id, instance in instances.items():
            self.assertEqual(instance.id, screen_id)
            self.assertEqual(
                list(instance.BINDINGS),
                keybinds.bindings_for(instance.id),
                screen_id,
            )

    def test_installed_bindings_are_defaults_without_user_file(self) -> None:
        path = keybinds.keybinds_path()
        if path.is_file():
            self.skipTest(f"user keybinds present: {path}")
        self.assertEqual(
            list(CifixChatApp.BINDINGS),
            keybinds.bindings_for("global", keybinds.DEFAULTS),
        )
        self.assertEqual(
            list(CommandPalette.BINDINGS),
            keybinds.bindings_for("palette", keybinds.DEFAULTS),
        )
        self.assertEqual(
            list(ModelPicker.BINDINGS),
            keybinds.bindings_for("model-picker", keybinds.DEFAULTS),
        )


if __name__ == "__main__":
    unittest.main()
