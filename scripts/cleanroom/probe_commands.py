# -*- coding: utf-8 -*-
"""Stage-2 acceptance: drive every slash command in the shipped app.

Part A uses the real product path (gates on, no injected model, dummy
key configured, scratch project trusted).  Part B injects the fake
model for the chat + /run paths.  A command passes when it produces
its effect (new history records or its screen) with no failure text.
"""
import asyncio
import os
import tempfile
import time
from pathlib import Path

FAIL_MARKERS = ("暂时不可用", "出问题了", "Traceback", "读取失败", "不可用")
RESULTS: list[tuple[str, bool, str]] = []


def note(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")


def build_project(root: Path) -> Path:
    proj = root / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        "[project]\nname = 'cmdprobe'\n", encoding="utf-8"
    )
    pkg = proj / "calc"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ops.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (proj / "tests" / "test_calc.py").write_text(
        "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return proj


async def submit(app, pilot, text: str) -> None:
    from core.tui import PromptInput

    prompt = app.screen.query_one(PromptInput)
    prompt.focus()
    prompt.text = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def wait_new(app, n0: int, timeout: float = 30.0) -> int:
    start = time.time()
    while time.time() - start < timeout:
        if len(app.history) > n0:
            return len(app.history)
        await asyncio.sleep(0.25)
    return len(app.history)


async def wait_reply(app, n0: int, timeout: float = 30.0) -> int:
    """Wait until a non-user record lands (the command's own output)."""
    start = time.time()
    while time.time() - start < timeout:
        if any(kind != "user" for kind, _, _ in app.history[n0:]):
            await asyncio.sleep(0.4)
            return len(app.history)
        await asyncio.sleep(0.25)
    return len(app.history)


def check_records(app, label: str, n0: int, expect_any: tuple = ()) -> None:
    new = app.history[n0:]
    joined = "\n".join(t for _, t, _ in new)
    bad = [m for m in FAIL_MARKERS if m in joined]
    extra = {"/profile": ("自动建档失败", "建档失败"), "/run": ("未执行",)}
    bad += [m for m in extra.get(label, ()) if m in joined]
    ok = bool(new) and not bad
    if expect_any:
        ok = ok and any(k in joined for k in expect_any)
    note(label, ok, f"new={len(new)} bad={bad}")


async def part_a(root: Path, proj: Path) -> None:
    from core.setup_wizard import run_setup
    from core.tui import CifixChatApp, ModelPicker, WorkspaceTrustScreen

    run_setup("deepseek", "deepseek-flash", "sk-cmdprobe")
    proj2 = root / "proj2"
    proj2.mkdir()
    (proj2 / "pyproject.toml").write_text(
        "[project]\nname = 'p2'\n", encoding="utf-8"
    )

    app = CifixChatApp(model=None, workspace_root=proj, interactive_gates=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # Trust is per-session now: agree the startup prompt first.
        trust0 = next(
            (s for s in app.screen_stack if isinstance(s, WorkspaceTrustScreen)),
            None,
        )
        if trust0 is not None:
            from textual.widgets import OptionList as _OL

            _list = trust0.query_one("#trust-list", _OL)
            _list.focus()
            await pilot.press("enter")
            await pilot.pause()
        commands = [
            ("/help", ("/run", "命令")),
            ("/check", ("体检",)),
            ("/profile", ("建档", "档案", "profile")),
            ("/accept", ("验收", "FAIL", "PASS")),
            ("/workspace", ("已纳管", "目录")),
            ("/team", ("名册", "员工", "generalist")),
            ("/brief", ("简报",)),
            ("/permissions", ("规则", "权限")),
            ("/mode", ("模式",)),
            ("/pending", ("待批准",)),
            ("/runs", ("运行", "记录")),
            ("/repro", ("复现",)),
        ]
        for cmd, expect in commands:
            n0 = len(app.history)
            depth0 = len(app.screen_stack)
            await submit(app, pilot, cmd)
            await wait_reply(app, n0, timeout=60)
            await pilot.pause()
            # G：提交时用户输入先落一条记录，再弹窗；只看是否多了一层屏幕，
            # 并要求新增记录里只有用户那一条（弹窗命令不该另写输出）。
            if len(app.screen_stack) > depth0 and all(
                kind == "user" for kind, _, _ in app.history[n0:]
            ):
                note(cmd, True, "opened a screen")
                await pilot.press("escape")
                await pilot.pause()
            else:
                check_records(app, cmd, n0, expect)
        # /market may open its own page
        n0 = len(app.history)
        depth0 = len(app.screen_stack)
        await submit(app, pilot, "/market")
        await wait_new(app, n0, timeout=15)
        await pilot.pause()
        if len(app.screen_stack) > depth0:
            note("/market", True, "opened market page")
            await pilot.press("escape")
            await pilot.pause()
        else:
            check_records(app, "/market", n0, ("市场", "扩展", "目录", "fs-tools"))
        # /model opens the picker (config exists)
        await submit(app, pilot, "/model")
        await pilot.pause()
        is_picker = any(isinstance(s, ModelPicker) for s in app.screen_stack)
        note("/model", is_picker, f"picker={is_picker}")
        if is_picker:
            await pilot.press("escape")
            await pilot.pause()
        # /cd to an untrusted dir -> trust screen -> agree -> switched
        n0 = len(app.history)
        await submit(app, pilot, f"/cd {proj2}")
        await pilot.pause()
        trust = next(
            (s for s in app.screen_stack if isinstance(s, WorkspaceTrustScreen)),
            None,
        )
        note("/cd trust prompt", trust is not None)
        if trust is not None:
            from textual.widgets import OptionList

            option_list = trust.query_one("#trust-list", OptionList)
            option_list.focus()
            await pilot.press("enter")
            await wait_new(app, n0, timeout=15)
            check_records(app, "/cd switch", n0, ("已切换工作区",))
        # /quit exits the app
        await submit(app, pilot, "/quit")
        await pilot.pause()
        note("/quit", not app.is_running)


def _load_fake_model_class():
    """从仓库检出里直接加载替身模型类（产品包不含替身，见清理定版）。

    本探针是开发期工具、总在仓库检出内运行；fake_model.py 只依赖
    agentscope 本身，不需要包上下文，用文件位置加载即可。被测的
    core 仍全部来自安装包，不受影响。
    """
    import importlib.util

    fake_path = (
        Path(__file__).resolve().parents[2] / "agentscope_ext" / "fake_model.py"
    )
    spec = importlib.util.spec_from_file_location("probe_fake_model", fake_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeChatModel


async def part_b(proj: Path) -> None:
    FakeChatModel = _load_fake_model_class()

    from core.tui import CifixChatApp

    app = CifixChatApp(
        model=FakeChatModel(fallback_reply="替身秘书报到，随时待命"),
        workspace_root=proj,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        n0 = len(app.history)
        await submit(app, pilot, "你好")
        await wait_new(app, n0, timeout=30)
        joined = "\n".join(t for _, t, _ in app.history[n0:])
        note("chat reply", "替身秘书报到" in joined, f"new={len(app.history) - n0}")
        n0 = len(app.history)
        await submit(app, pilot, "/run --yes")
        await wait_reply(app, n0, timeout=120)
        # a run streams many records; wait for an outcome line
        start = time.time()
        outcome = ""
        while time.time() - start < 120:
            joined = "\n".join(t for _, t, _ in app.history[n0:])
            if any(k in joined for k in ("PASS", "CAPPED", "FAIL", "验收")):
                outcome = joined
                break
            await asyncio.sleep(0.5)
        bad = [m for m in FAIL_MARKERS if m in outcome]
        note("/run", bool(outcome) and not bad, f"bad={bad}")


async def main() -> None:
    root = Path(tempfile.mkdtemp())
    os.environ["XDG_CONFIG_HOME"] = str(root / "xdg")
    proj = build_project(root)
    await part_a(root, proj)
    await part_b(proj)
    failed = [label for label, ok, _ in RESULTS if not ok]
    print("TOTAL:", len(RESULTS), "FAILED:", failed)
    print("COMMAND_ACCEPTANCE:", "PASS" if not failed else "FAIL")


asyncio.run(main())
