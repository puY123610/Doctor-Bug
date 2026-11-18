# -*- coding: utf-8 -*-
"""Pilot-drive the stage-1 gates the way a fresh install hits them:
no config -> SetupScreen -> save -> TrustScreen -> agree -> home.
Second pass: decline the trust question -> app exits."""
import asyncio
import os
import tempfile
from pathlib import Path

import core.model_config as mc


def isolate(root: Path) -> None:
    mc.DEFAULT_CONFIG_PATH = root / "no_cfg.json"
    mc.DEFAULT_ENV_PATH = root / "no_env.local"
    os.environ["XDG_CONFIG_HOME"] = str(root / "xdg")
    os.environ.pop("DEEPSEEK_API_KEY", None)


async def main() -> None:
    from textual.widgets import Input, OptionList

    from core.tui import CifixChatApp, SetupScreen, WorkspaceTrustScreen

    root = Path(tempfile.mkdtemp())
    isolate(root)
    proj = root / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "calc.py").write_text("X = 1\n", encoding="utf-8")
    (proj / "pyproject.toml").write_text("[project]\nname = 'probe'\n", encoding="utf-8")
    (proj / "tests" / "test_calc.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )

    app = CifixChatApp(model=None, workspace_root=proj, interactive_gates=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        names = [type(s).__name__ for s in app.screen_stack]
        print("STACK1:", names)
        assert any(isinstance(s, SetupScreen) for s in app.screen_stack), names
        screen = next(s for s in app.screen_stack if isinstance(s, SetupScreen))
        key_input = screen.query_one("#setup-key", Input)
        key_input.value = "sk-pilot-1"
        key_input.focus()
        await pilot.press("enter")
        await pilot.pause()
        names = [type(s).__name__ for s in app.screen_stack]
        print("STACK2:", names)
        assert any(
            isinstance(s, WorkspaceTrustScreen) for s in app.screen_stack
        ), names
        trust = next(
            s for s in app.screen_stack if isinstance(s, WorkspaceTrustScreen)
        )
        option_list = trust.query_one("#trust-list", OptionList)
        option_list.focus()
        await pilot.press("enter")
        await pilot.pause()
        names = [type(s).__name__ for s in app.screen_stack]
        print("STACK3:", names)
        assert not any(
            isinstance(s, (SetupScreen, WorkspaceTrustScreen))
            for s in app.screen_stack
        ), names
    print("AGREE_PATH_OK")

    # New spec (boss): agreeing counts for this session only.  A fresh
    # launch at the same project must ask again.
    app3 = CifixChatApp(model=None, workspace_root=proj, interactive_gates=True)
    async with app3.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert any(
            isinstance(s, WorkspaceTrustScreen) for s in app3.screen_stack
        ), [type(s).__name__ for s in app3.screen_stack]
        await pilot.press("escape")
        await pilot.pause()
        assert not app3.is_running
    print("REASK_OK")

    # Decline path: a different, untrusted project -> Esc at trust -> exit.
    proj2 = root / "proj2"
    proj2.mkdir()
    (proj2 / "pyproject.toml").write_text("[project]\nname = 'probe2'\n", encoding="utf-8")
    app2 = CifixChatApp(model=None, workspace_root=proj2, interactive_gates=True)
    async with app2.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # config now exists (saved above) -> straight to the trust gate
        assert any(
            isinstance(s, WorkspaceTrustScreen) for s in app2.screen_stack
        ), [type(s).__name__ for s in app2.screen_stack]
        await pilot.press("escape")
        await pilot.pause()
        print("RUNNING_AFTER_DECLINE:", app2.is_running)
        assert not app2.is_running
    print("DECLINE_PATH_OK")


asyncio.run(main())
