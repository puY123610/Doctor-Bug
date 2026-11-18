"""TUIE1：/status 状态总览 + /mcp 外接工具弹窗，统一五态，密钥与 env 脱敏。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Static

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, keybinds, market, status_view
from core.model_config import load_config
from core.status_view import (
    KEY_UNVERIFIED,
    MCP_NOT_CHECKED,
    STATE_BAD,
    STATE_OK,
    STATE_UNKNOWN,
    STATE_UNSET,
)
from core.tui import COMMANDS, MODE_LABELS, CifixChatApp, InfoViewModal, help_view_text

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
SECRET_KEY = "sk-TUIE1-secret-value-9f8e7d"
ENV_SECRET = "env-TUIE1-hidden-token-1234"


def _write_mcp(tmp: Path, servers) -> Path:
    path = tmp / "mcp.json"
    path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return path


def _servers_with_secrets(tmp: Path) -> list[dict]:
    script = tmp / "server.py"
    script.write_text("print('x')\n", encoding="utf-8")
    return [
        {
            "name": "echo",
            "command": "python3",
            "args": [str(script), "--token", ENV_SECRET],
            "env": {"API_TOKEN": ENV_SECRET},
        },
        {"name": "ghost", "command": "/nonexistent/bin/ghost-server"},
        {
            "name": "remote",
            "url": f"https://user:{ENV_SECRET}@mcp.example.com/v1?key={ENV_SECRET}",
            "headers": {"Authorization": f"Bearer {ENV_SECRET}"},
        },
    ]


class FiveStateMappingTest(unittest.TestCase):
    def test_key_states(self) -> None:
        self.assertEqual(
            status_view.key_row(key_env="K", key_present=False).state, STATE_UNSET
        )
        unverified = status_view.key_row(key_env="K", key_present=True)
        self.assertEqual(unverified.state, STATE_UNKNOWN)  # 已配置 ≠ 正常
        self.assertIn(KEY_UNVERIFIED, unverified.note)
        self.assertEqual(
            status_view.key_row(key_env="K", key_present=True, verified=True).state,
            STATE_OK,
        )
        self.assertEqual(
            status_view.key_row(key_env=None, key_present=False).state, STATE_UNSET
        )
        self.assertEqual(
            status_view.key_row(key_env=None, key_present=False, error="坏了").state,
            STATE_BAD,
        )

    def test_mcp_none_many_broken_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # 无文件：未配置
            rows, error = status_view.mcp_server_rows(tmp / "missing.json")
            self.assertEqual((rows, error), ([], None))
            self.assertEqual(
                status_view.mcp_summary_row(rows, error).state, STATE_UNSET
            )
            self.assertEqual(
                status_view.mcp_summary_row(rows, error).note, "未安装 MCP 服务器"
            )
            self.assertEqual(status_view.mcp_modal_text(rows, error), "未配置 MCP 服务器")
            # 多台：已配置只能是未知，找不到命令才是异常
            rows, error = status_view.mcp_server_rows(
                _write_mcp(tmp, _servers_with_secrets(tmp))
            )
            self.assertIsNone(error)
            states = {row.name: row.state for row in rows}
            self.assertEqual(
                states,
                {"echo": STATE_UNKNOWN, "ghost": STATE_BAD, "remote": STATE_UNKNOWN},
            )
            self.assertNotIn(STATE_OK, states.values())
            self.assertIn(MCP_NOT_CHECKED, rows[0].note)
            summary = status_view.mcp_summary_row(rows, error)
            self.assertEqual(summary.state, STATE_BAD)
            self.assertIn("已装 3 台", summary.note)
            # 配置损坏
            bad = tmp / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            rows, error = status_view.mcp_server_rows(bad)
            self.assertEqual(rows, [])
            self.assertIn("无法读取", error)
            self.assertEqual(status_view.mcp_summary_row(rows, error).state, STATE_BAD)
            self.assertIn(STATE_BAD, status_view.mcp_modal_text(rows, error))
            # 名字重复
            dup = _write_mcp(
                tmp,
                [{"name": "a", "command": "python3"}, {"name": "a", "command": "python3"}],
            )
            rows, error = status_view.mcp_server_rows(dup)
            self.assertIn("重复", error)
            self.assertEqual(status_view.mcp_summary_row(rows, error).state, STATE_BAD)

    def test_other_rows(self) -> None:
        self.assertEqual(status_view.workspace_row(None, []).state, STATE_UNSET)
        with tempfile.TemporaryDirectory() as raw:
            row = status_view.workspace_row(Path(raw), [str(Path(raw).resolve())])
            self.assertEqual(row.state, STATE_OK)
            self.assertIn("本次会话已信任", row.note)
        self.assertEqual(status_view.mode_row("ask", MODE_LABELS).state, STATE_OK)
        self.assertIn("逐次确认", status_view.mode_row("ask", MODE_LABELS).note)
        self.assertEqual(status_view.mode_row("weird", MODE_LABELS).state, STATE_BAD)
        self.assertEqual(
            status_view.model_row(
                provider=None, model=None, effort_label="默认", layer=None
            ).state,
            STATE_UNSET,
        )
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(
                market, "default_skills_dir", return_value=Path(raw)
            ), mock.patch.object(
                market, "default_mcp_config_path", return_value=Path(raw) / "m.json"
            ):
                self.assertEqual(status_view.market_row().state, STATE_UNSET)
                self.assertEqual(status_view.market_row().note, "未安装扩展")
        with mock.patch.object(market, "installed_ids", side_effect=ValueError("坏")):
            self.assertEqual(status_view.market_row().state, STATE_BAD)
        version = status_view.version_row()
        self.assertEqual(version.state, STATE_OK)
        self.assertIn("0.1.0", version.note)
        missing = status_view.version_row(pyproject=Path("/nonexistent/pyproject.toml"))
        self.assertIn(missing.state, (STATE_OK, STATE_UNKNOWN))


class _ModalBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _patch_paths(self, mcp_path: Path):
        stack = mock.patch.multiple(
            market,
            default_mcp_config_path=mock.MagicMock(return_value=mcp_path),
            default_skills_dir=mock.MagicMock(return_value=self.tmp / "skills"),
        )
        return stack

    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )

    @staticmethod
    def _body(app) -> str:
        return str(app.screen.query_one("#info-body", Static).render())

    @staticmethod
    def _all_log(app) -> str:
        return "\n".join(str(text) for _, text, _ in app.history)

    async def _open(self, app, pilot, command: str) -> None:
        prompt = app.screen.query_one("PromptInput")
        prompt.focus()
        prompt.text = command
        await pilot.pause()
        # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
        if prompt.box is not None and prompt.box.suggestions_visible:
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, InfoViewModal):
                return


class StatusModalTest(_ModalBase):
    async def test_status_rows_from_real_backend_and_redacted(self) -> None:
        profile = load_config()
        mcp_path = _write_mcp(self.tmp, _servers_with_secrets(self.tmp))
        with self._patch_paths(mcp_path), mock.patch.dict(
            os.environ, {profile.api_key_env: SECRET_KEY}
        ):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/status")
                self.assertIsInstance(app.screen, InfoViewModal)
                self.assertEqual(app.screen.id, keybinds.SCREEN_STATUS_VIEW)
                body = self._body(app)
                expected = status_view.format_rows(app.collect_status_rows())
                self.assertEqual(body, expected)
                for name in ("版本", "模型", "密钥", "工作区", "权限模式", "外接工具", "市场"):
                    self.assertIn(f"{name}：", body)
                self.assertIn(f"密钥：{STATE_UNKNOWN} · {KEY_UNVERIFIED}", body)
                self.assertIn(profile.api_key_env, body)
                self.assertIn("权限模式：正常 · 逐次确认（ask）", body)
                self.assertIn("外接工具：异常 · 已装 3 台", body)
                self.assertIn("市场：未配置 · 未安装扩展", body)
                for secret in (SECRET_KEY, ENV_SECRET, SECRET_KEY[:12]):
                    self.assertNotIn(secret, body)
                    self.assertNotIn(secret, self._all_log(app))
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, InfoViewModal)

    async def test_status_key_unset_when_env_missing(self) -> None:
        profile = load_config()
        env = {k: v for k, v in os.environ.items() if k != profile.api_key_env}
        with self._patch_paths(self.tmp / "none.json"), mock.patch.dict(
            os.environ, env, clear=True
        ):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/status")
                body = self._body(app)
                self.assertIn(f"密钥：{STATE_UNSET}", body)
                self.assertIn("外接工具：未配置 · 未安装 MCP 服务器", body)


class McpModalTest(_ModalBase):
    async def test_mcp_zero_servers(self) -> None:
        with self._patch_paths(self.tmp / "none.json"):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/mcp")
                self.assertEqual(app.screen.id, keybinds.SCREEN_MCP_VIEW)
                self.assertEqual(self._body(app), "未配置 MCP 服务器")

    async def test_mcp_many_servers_redacted(self) -> None:
        mcp_path = _write_mcp(self.tmp, _servers_with_secrets(self.tmp))
        with self._patch_paths(mcp_path):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/mcp")
                body = self._body(app)
                self.assertIn(f"echo：{STATE_UNKNOWN} · stdio · 命令 python3 · 参数 3 个", body)
                self.assertIn(MCP_NOT_CHECKED, body)
                self.assertIn(f"ghost：{STATE_BAD}", body)
                self.assertIn("remote：未知 · http · https://mcp.example.com", body)
                self.assertNotIn(ENV_SECRET, body)
                self.assertNotIn("server.py", body)
                self.assertNotIn(ENV_SECRET, self._all_log(app))

    async def test_mcp_broken_config(self) -> None:
        bad = self.tmp / "mcp.json"
        bad.write_text("{broken", encoding="utf-8")
        with self._patch_paths(bad):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/mcp")
                body = self._body(app)
                self.assertIn(f"MCP 配置：{STATE_BAD}", body)
                self.assertIn("无法读取", body)


class CommandAndKeyWiringTest(_ModalBase):
    def test_commands_and_help(self) -> None:
        names = [name for name, _ in COMMANDS]
        self.assertEqual(
            names[names.index("/market") + 1 : names.index("/help")],
            ["/status", "/mcp"],
        )
        # TUIE2：HELP_TEXT 退役，顺序改由 /help 弹窗正文（读 COMMANDS）核对。
        help_text = help_view_text()
        self.assertLess(help_text.index("/market"), help_text.index("/status"))
        self.assertLess(help_text.index("/status"), help_text.index("/mcp "))
        self.assertEqual(keybinds.find_conflicts(keybinds.DEFAULTS), ())

    async def test_close_key_comes_from_registry(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"status_view_close": "q", "mcp_view_close": "x"}
        )
        self.assertEqual(diagnostics, ())
        with self._patch_paths(self.tmp / "none.json"), mock.patch.object(
            keybinds, "_ACTIVE", (entries, diagnostics)
        ):
            app = self._app()
            async with app.run_test(size=(160, 50)) as pilot:
                await pilot.pause()
                await self._open(app, pilot, "/status")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, InfoViewModal)  # 旧键不再关
                await pilot.press("q")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, InfoViewModal)
                await self._open(app, pilot, "/mcp")
                await pilot.press("q")
                await pilot.pause()
                self.assertIsInstance(app.screen, InfoViewModal)
                await pilot.press("x")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, InfoViewModal)


if __name__ == "__main__":
    unittest.main()
