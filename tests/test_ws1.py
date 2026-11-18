# -*- coding: utf-8 -*-
"""WS1：非项目目录待命、/cd 切换、/workspace 会话纳管。

全程离线：FakeChatModel / 直接调工具函数。不读密钥，不调用付费 API。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher
from core.dispatcher import (
    UNBOUND_WORKSPACE,
    UNBOUND_WORKSPACE_REJECTION,
    build_dispatcher,
)
from core.tui import (
    COMMANDS,
    PLATFORM_ROOT,
    CifixChatApp,
    PromptInput,
    _completion_candidates,
    _is_known_command,
    directory_has_project_markers,
    startup_directory_is_project,
)
from tests.test_tui import ScriptedToolModel, _tool_result_texts

DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"


def _history(app: CifixChatApp) -> str:
    return "\n".join(text for _, text, _ in app.history)


def _write_project(path: Path, name: str = "fixture") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        f"[project]\nname = \"{name}\"\n",
        encoding="utf-8",
    )
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return path.resolve()


class MarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ws1-marker-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_each_marker_counts_and_plain_directory_does_not(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi\n", encoding="utf-8")
        self.assertFalse(directory_has_project_markers(plain))
        self.assertFalse(startup_directory_is_project(plain))

        git_dir = self.tmp / "git"
        git_dir.mkdir()
        (git_dir / ".git").mkdir()
        self.assertTrue(directory_has_project_markers(git_dir))

        pyproject = self.tmp / "py"
        pyproject.mkdir()
        (pyproject / "pyproject.toml").write_text("x\n", encoding="utf-8")
        self.assertTrue(startup_directory_is_project(pyproject))

        requirements = self.tmp / "req"
        requirements.mkdir()
        (requirements / "requirements.txt").write_text("x\n", encoding="utf-8")
        self.assertTrue(startup_directory_is_project(requirements))

        bugdoc = self.tmp / "bug"
        bugdoc.mkdir()
        (bugdoc / ".drbug").mkdir()
        self.assertTrue(directory_has_project_markers(bugdoc))
        self.assertTrue(startup_directory_is_project(bugdoc))

        # .drbug 必须是目录；pyproject 必须是文件。
        weird = self.tmp / "weird"
        weird.mkdir()
        (weird / ".drbug").write_text("not a dir\n", encoding="utf-8")
        (weird / "pyproject.toml").mkdir()
        self.assertFalse(directory_has_project_markers(weird))

    def test_home_tool_cache_is_not_a_project_but_profiles_are(self) -> None:
        home = self.tmp / "home"
        (home / ".drbug" / "work").mkdir(parents=True)
        with mock.patch("core.tui.Path.home", return_value=home):
            self.assertTrue(directory_has_project_markers(home))
            self.assertFalse(startup_directory_is_project(home))

            (home / "pyproject.toml").write_text("x\n", encoding="utf-8")
            self.assertTrue(startup_directory_is_project(home))
            (home / "pyproject.toml").unlink()

            (home / ".drbug" / "profiles").mkdir()
            self.assertTrue(startup_directory_is_project(home))

    def test_demo_repos_are_startup_projects(self) -> None:
        for name in ("demo_repo", "demo_repo2", "demo_repo3", "demo_repo4"):
            self.assertTrue(
                startup_directory_is_project(
                PLATFORM_ROOT / "tests" / "fixtures" / name
            ), name
            )


class StandbyAndSwitchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ws1-app-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.addCleanup(dispatcher.set_event_sink, None)

    def _standby_root(self) -> Path:
        root = self.tmp / "standby"
        root.mkdir(exist_ok=True)
        (root / "readme.txt").write_text("not a project\n", encoding="utf-8")
        return root.resolve()

    def _app(self, root: Path, model: object | None = None) -> CifixChatApp:
        return CifixChatApp(
            model=model or FakeChatModel(fallback_reply="我是调度员替身，随时待命"),
            workspace_root=root,
        )

    async def _submit(self, app: CifixChatApp, pilot, text: str) -> None:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = text
        await pilot.pause()
        # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
        if prompt.box is not None and prompt.box.suggestions_visible:
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    def _status(self, app: CifixChatApp) -> str:
        return str(app.screen.query_one(".prompt-status").content)

    async def test_non_project_start_is_standby_and_can_chat(self) -> None:
        root = self._standby_root()
        app = self._app(root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIsNone(app._active_workspace)
            self.assertIn("工作区：未绑定", app.status_line().plain)
            self.assertIn("工作区：未绑定", self._status(app))
            self.assertEqual(dispatcher._BOUND_WORKSPACE, UNBOUND_WORKSPACE)

            await self._submit(app, pilot, "你好，介绍一下自己")
            await app.workers.wait_for_complete()
            await pilot.pause()

            text = _history(app)
            self.assertIn("我是调度员替身，随时待命", text)
            self.assertNotIn("未知命令", text)
            self.assertNotIn("主治医生暂时不可用", text)
            self.assertIsNone(app._active_workspace)
            prompt = app._dispatcher._system_prompt
            self.assertIn("当前绑定工作区: 未绑定", prompt)
            self.assertIn("/cd", prompt)
            self.assertNotIn("你启动我的这个目录就是", prompt)
            self.assertNotIn("当前绑定工作区: demo_repo", prompt)

    async def test_pasted_path_stays_a_message_and_does_not_bind(self) -> None:
        app = self._app(self._standby_root())
        pasted = "/Users/hostuser/Desktop/截屏 2026-11-14 15.26.13.png 这是什么"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, pasted)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertNotIn("未知命令", _history(app))
            self.assertIn("我是调度员替身，随时待命", _history(app))
            self.assertIsNone(app._active_workspace)
            self.assertIn("工作区：未绑定", app.status_line().plain)
            self.assertEqual(app.history[0][:2], ("user", pasted))

    def test_standby_tools_reject_without_touching_demo(self) -> None:
        app = self._app(self._standby_root())
        self.assertEqual(dispatcher._BOUND_WORKSPACE, UNBOUND_WORKSPACE)
        calls = [
            dispatcher.run_repair,
            dispatcher.accept_check,
            dispatcher.propose_repro,
            dispatcher.freeze_repro,
            dispatcher.list_roster,
        ]
        for fn in calls:
            result = fn()
            self.assertIn("workspace_unbound", result, fn.__name__)
            self.assertIn("/cd", result, fn.__name__)
            self.assertNotIn("demo_repo", result, fn.__name__)
            self.assertEqual(result, UNBOUND_WORKSPACE_REJECTION)
        self.assertEqual(
            dispatcher.show_agent_archive("billing-specialist"),
            UNBOUND_WORKSPACE_REJECTION,
        )
        self.assertEqual(
            dispatcher.add_worker("someone", "app.py"),
            UNBOUND_WORKSPACE_REJECTION,
        )
        self.assertEqual(
            dispatcher.remove_worker("someone"),
            UNBOUND_WORKSPACE_REJECTION,
        )
        # 显式路径仍按原工具执行，不走待命拒绝。
        project = _write_project(self.tmp / "explicit")
        explicit = dispatcher.accept_check(str(project))
        self.assertIn("尚无冻结档案", explicit)
        self.assertNotIn("workspace_unbound", explicit)
        self.assertIn(str(project), explicit)

    async def test_model_tool_call_is_rejected_in_standby(self) -> None:
        model = ScriptedToolModel(
            [
                {"type": "tool", "name": "run_repair", "input": {}},
                {"type": "tool", "name": "accept_check", "input": {}},
                {"type": "tool", "name": "propose_repro", "input": {}},
                {"type": "text", "text": "请先绑定工作区。"},
            ]
        )
        app = self._app(self._standby_root(), model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "请把这个目录修一下并验收")
            await app.workers.wait_for_complete()
            await pilot.pause()

        texts = [
            text
            for messages in model.seen_messages
            for text in _tool_result_texts(messages)
        ]
        self.assertGreaterEqual(len(texts), 3, texts)
        for text in texts:
            self.assertIn("workspace_unbound", text)
            self.assertIn("/cd", text)
            self.assertNotIn("demo_repo", text)
        self.assertIn("请先绑定工作区。", _history(app))
        self.assertIsNone(app._active_workspace)

    async def test_cd_binds_project_migrates_state_and_tools_work(self) -> None:
        project = _write_project(self.tmp / "proj")
        app = self._app(self._standby_root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "先打个招呼")
            await app.workers.wait_for_complete()
            await pilot.pause()
            old_agent = app._dispatcher
            old_state = old_agent.state
            self.assertTrue(old_state.context)

            before = {
                path.relative_to(self.tmp).as_posix()
                for path in self.tmp.rglob("*")
            }
            await self._submit(app, pilot, f"/cd {project}")
            await pilot.pause()

            self.assertIsNot(app._dispatcher, old_agent)
            self.assertIs(app._dispatcher.state, old_state)
            self.assertEqual(app._active_workspace, project)
            self.assertIn(str(project), app._dispatcher._system_prompt)
            self.assertIn("你启动我的这个目录就是", app._dispatcher._system_prompt)
            self.assertIn(f"已切换工作区：{project}", _history(app))
            self.assertIn(str(project), app.status_line().plain)
            self.assertIn(str(project), self._status(app))
            self.assertNotIn("工作区：未绑定", app.status_line().plain)
            after = {
                path.relative_to(self.tmp).as_posix()
                for path in self.tmp.rglob("*")
            }
            self.assertEqual(before, after)

        verdict = dispatcher.accept_check()
        self.assertIn("尚无冻结档案", verdict)
        self.assertNotIn("workspace_unbound", verdict)
        roster = dispatcher.list_roster()
        self.assertNotIn("workspace_unbound", roster)

    async def test_cd_missing_or_file_keeps_the_binding(self) -> None:
        project = _write_project(self.tmp / "kept")
        stray = self.tmp / "not-a-dir.txt"
        stray.write_text("x\n", encoding="utf-8")
        app = self._app(project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            bound_before = dispatcher._BOUND_WORKSPACE
            managed_before = list(app._managed_workspaces)

            await self._submit(app, pilot, "/cd /this/path/does/not/exist-ws1")
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            self.assertEqual(dispatcher._BOUND_WORKSPACE, bound_before)
            self.assertEqual(app._managed_workspaces, managed_before)
            self.assertIn("路径不存在", _history(app))
            self.assertIn("工作区绑定保持不变", _history(app))
            self.assertNotIn("已切换工作区：", _history(app))
            self.assertIn(str(project), app.status_line().plain)

            await self._submit(app, pilot, f"/cd {stray}")
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            self.assertEqual(dispatcher._BOUND_WORKSPACE, bound_before)
            self.assertIn("不是目录", _history(app))

            await self._submit(app, pilot, "/cd")
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            self.assertIn("用法: /cd <路径>", _history(app))

    async def test_cd_relative_to_bound_dir_and_tilde_and_unbound_cwd(self) -> None:
        parent = _write_project(self.tmp / "parent")
        child = parent / "nested"
        child.mkdir()
        app = self._app(parent)
        self.assertEqual(app._resolve_session_path("nested"), child.resolve())
        self.assertEqual(
            app._resolve_session_path("~"),
            Path.home().resolve(),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/cd nested")
            await pilot.pause()
            self.assertEqual(app._active_workspace, child.resolve())
            self.assertIn(f"已切换工作区：{child.resolve()}", _history(app))

        standby = self._app(self._standby_root())
        base = self.tmp / "cwd-base"
        (base / "relproj").mkdir(parents=True)
        previous = Path.cwd()
        os.chdir(base)
        try:
            self.assertEqual(
                standby._resolve_session_path("relproj"),
                (base / "relproj").resolve(),
            )
        finally:
            os.chdir(previous)

    async def test_workspace_add_remove_and_fallback(self) -> None:
        project = _write_project(self.tmp / "active")
        extra = self.tmp / "extra"
        extra.mkdir()
        other = self.tmp / "other"
        other.mkdir()
        app = self._app(self._standby_root())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = {
                path.relative_to(self.tmp).as_posix()
                for path in self.tmp.rglob("*")
            }

            await self._submit(app, pilot, "/workspace")
            await pilot.pause()
            self.assertIn("（无）", _history(app))
            self.assertIn("未绑定", _history(app))

            await self._submit(app, pilot, f"/workspace add {extra}")
            await pilot.pause()
            self.assertIn(f"已纳管目录（未切换）: {extra.resolve()}", _history(app))
            self.assertIsNone(app._active_workspace)
            self.assertIn("工作区：未绑定", app.status_line().plain)
            self.assertEqual(
                dispatcher.accept_check(), UNBOUND_WORKSPACE_REJECTION
            )

            await self._submit(app, pilot, f"/cd {project}")
            await pilot.pause()
            await self._submit(app, pilot, f"/workspace add {other}")
            await pilot.pause()
            await self._submit(app, pilot, "/workspace")
            await pilot.pause()
            listing = _history(app)
            self.assertIn(f"* {project} （当前）", listing)
            self.assertIn(str(extra.resolve()), listing)
            self.assertIn(str(other.resolve()), listing)

            await self._submit(app, pilot, f"/workspace remove {other}")
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            self.assertIn("当前工作区绑定保持不变", _history(app))
            self.assertNotIn(other.resolve(), app._managed_workspaces)

            await self._submit(app, pilot, f"/workspace remove {project}")
            await pilot.pause()
            self.assertIsNone(app._active_workspace)
            self.assertIn("回落到未绑定待命态", _history(app))
            self.assertIn("工作区：未绑定", app.status_line().plain)
            self.assertIn("工作区：未绑定", self._status(app))
            self.assertIn(extra.resolve(), app._managed_workspaces)
            self.assertNotIn(project, app._managed_workspaces)
            self.assertEqual(
                dispatcher.accept_check(), UNBOUND_WORKSPACE_REJECTION
            )

            after = {
                path.relative_to(self.tmp).as_posix()
                for path in self.tmp.rglob("*")
            }
            self.assertEqual(before, after)

        fresh = self._app(self._standby_root())
        self.assertEqual(fresh._managed_workspaces, [])
        self.assertIsNone(fresh._active_workspace)

    async def test_project_start_still_binds_and_platform_root_binds_itself(self) -> None:
        project = _write_project(self.tmp / "legacy")
        app = self._app(project)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app._active_workspace, project)
            self.assertIn(str(project), app.status_line().plain)
            self.assertNotIn("工作区：未绑定", app.status_line().plain)
            self.assertIn(project, app._managed_workspaces)

        platform = self._app(PLATFORM_ROOT)
        agent = platform._ensure_dispatcher()
        self.assertEqual(platform._active_workspace, PLATFORM_ROOT)
        self.assertIn(f"当前绑定工作区: {PLATFORM_ROOT}", agent._system_prompt)
        self.assertIn("你启动我的这个目录就是", agent._system_prompt)
        self.assertIn(f"工作区：{PLATFORM_ROOT}", platform.status_line().plain)
        events: list[dict] = []
        dispatcher.set_event_sink(events.append)
        result = dispatcher.accept_check()
        self.assertIn("尚无冻结档案", result)
        self.assertEqual(events[-1]["summary"], f"workspace={PLATFORM_ROOT}")
        self.assertNotIn("workspace_unbound", result)

    async def test_demo_repo_startup_binds(self) -> None:
        root = DEMO_REPO.resolve()
        model = ScriptedToolModel([{"type": "text", "text": "已绑定。"}])
        app = self._app(root, model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app._active_workspace, root)
            self.assertIn(str(root), app.status_line().plain)
            self.assertNotIn("工作区：未绑定", app.status_line().plain)
            self.assertNotIn("工作区：未绑定", self._status(app))
            self.assertIn(root, app._managed_workspaces)
            self.assertNotEqual(
                dispatcher._BOUND_WORKSPACE, UNBOUND_WORKSPACE
            )

    def test_real_home_cache_starts_unbound(self) -> None:
        home = Path.home()
        if (
            (home / ".git").exists()
            or (home / "pyproject.toml").is_file()
            or (home / "requirements.txt").is_file()
            or (home / ".drbug" / "profiles").is_dir()
            or (home / ".drbug" / "runs").is_dir()
        ):
            self.skipTest("当前主目录带有真正的项目特征")
        app = self._app(home)
        self.assertIsNone(app._active_workspace)
        self.assertIn("工作区：未绑定", app.status_line().plain)
        self.assertEqual(dispatcher._BOUND_WORKSPACE, UNBOUND_WORKSPACE)

    async def test_standby_slash_defaults_refuse_but_explicit_workspace_remains(self) -> None:
        app = self._app(self._standby_root())
        with mock.patch.object(
            dispatcher, "run_repair", return_value="修复完成"
        ) as repair:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self._submit(app, pilot, "/run")
                await pilot.pause()
                self.assertIn("/run 未执行", _history(app))
                self.assertIn("/cd", _history(app))
                self.assertNotIn("开始修复", _history(app))
                repair.assert_not_called()

                await self._submit(app, pilot, "/accept")
                await self._submit(app, pilot, "/profile")
                await self._submit(app, pilot, "/repro")
                await pilot.pause()
                text = _history(app)
                self.assertIn("/accept 未执行", text)
                self.assertIn("/profile 未执行", text)
                self.assertIn("/repro 未执行", text)

                await self._submit(app, pilot, f"/run --workspace {DEMO_REPO} --yes")
                await app.workers.wait_for_complete()
                await pilot.pause()
            repair.assert_called_once()
            self.assertEqual(
                Path(repair.call_args.kwargs["workspace"]).resolve(),
                DEMO_REPO.resolve(),
            )
            self.assertIn("修复完成", _history(app))
            self.assertIsNone(app._active_workspace)

    def test_commands_are_in_palette_help_and_completion(self) -> None:
        described = dict(COMMANDS)
        self.assertIn("切换会话工作区", described["/cd"])
        self.assertIn("纳管", described["/workspace"])
        # TUIE2：HELP_TEXT 退役，/help 弹窗正文与 COMMANDS 同源。
        from core.tui import help_view_text

        self.assertIn("/cd", help_view_text())
        self.assertIn("/workspace", help_view_text())
        self.assertIn(described["/workspace"], help_view_text())
        candidates = dict(_completion_candidates("/"))
        self.assertIn("/cd", candidates)
        self.assertIn("/workspace", candidates)
        self.assertEqual(
            [command for command, _ in _completion_candidates("/cd")],
            ["/cd"],
        )
        self.assertEqual(_completion_candidates("/Users/hostuser/x"), [])
        self.assertFalse(_is_known_command("/Users/hostuser/Desktop/proj"))
        self.assertTrue(_is_known_command("/cd /Users/hostuser/Desktop/proj"))
        self.assertTrue(_is_known_command("/workspace add /tmp"))

    def test_shift_tab_keeps_unbound_prompt(self) -> None:
        app = self._app(self._standby_root())
        agent = app._ensure_dispatcher()
        dispatcher.set_dispatcher_default_actors(agent, "llm")
        prompt = agent._system_prompt
        self.assertIn("当前绑定工作区: 未绑定", prompt)
        self.assertIn("默认执行后端 llm", prompt)
        self.assertNotIn("你启动我的这个目录就是", prompt)
        self.assertEqual(dispatcher._BOUND_WORKSPACE, UNBOUND_WORKSPACE)

    def test_build_dispatcher_none_means_standby(self) -> None:
        agent = build_dispatcher(
            model=FakeChatModel(fallback_reply="x"),
            workspace=None,
        )
        self.assertIn("当前绑定工作区: 未绑定", agent._system_prompt)
        self.assertNotIn("你启动我的这个目录就是", agent._system_prompt)


if __name__ == "__main__":
    unittest.main()
