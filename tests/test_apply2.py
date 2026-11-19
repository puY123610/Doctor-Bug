# -*- coding: utf-8 -*-
"""APPLY2：主治医生的 apply_result 工具——走现有确认回路，复用 apply_bundle。

全部离线：脚本模型发 apply_result 工具调用，真实 ChatSession（core/api.py 的
确认驱动）把程序拼的确认问题交给应答者；结果包用 results.build_result_bundle
真造，放在工作区自己的运行目录（state_root_for → <ws>/.drbug/runs/bundles），
apply_result 缺省就能找到它。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core import dispatcher
from core.api import ChatSession
from core.results import build_result_bundle
from tests.test_policy_secretary_confirm import (
    _ScriptedSecretaryModel,
    _collecting_sink,
    _event_text,
)

MISLEADING = "模型自写描述：只是改个注释，不用细看"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".drbug" not in p.relative_to(root).parts
    }


class _ApplyCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.ws = self.tmp / "proj"
        _write(self.ws, "pkg/a.py", "alpha\nbeta\n")
        _write(self.ws, "old.py", "gone\n")
        source = self.tmp / "source"
        shutil.copytree(self.ws, source)
        final = self.tmp / "final"
        _write(final, "pkg/a.py", "alpha\nBETA\n")
        _write(final, "pkg/c.py", "new file\n")
        baseline = {
            str(p.relative_to(source)): _sha(p)
            for p in sorted(source.rglob("*"))
            if p.is_file()
        }
        record = {
            "arm": "C",
            "kb_enabled": True,
            "outcome": "PASS",
            "rounds": [],
            "final_failed": [],
            "scope_denials": [],
            "baseline_hashes": baseline,
        }
        self.bundle = build_result_bundle(
            record,
            source,
            final,
            bundle_root=self.ws / ".drbug" / "runs" / "bundles",
            run_id="apply2-run",
        )
        self.before = _tree(self.ws)
        self.asked: list[str] = []

    def _responder(self, answer: str):
        def respond(line: str) -> str:
            self.asked.append(line)
            return answer

        return respond

    def _adoption(self) -> dict:
        return json.loads((self.bundle / "adoption.json").read_text(encoding="utf-8"))

    def _audit(self) -> str:
        path = self.ws / ".drbug" / "audit.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    async def _run(self, steps: list[dict], responder=None, marker: str = "已把结果包"):
        events: list[object] = []
        model = _ScriptedSecretaryModel(steps, marker=marker)
        kwargs = {}
        if responder is not None:
            kwargs["confirm_responder"] = responder
        session = ChatSession(
            model=model,
            actors="scripted",
            event_sink=_collecting_sink(events),
            workspace=self.ws,
            **kwargs,
        )
        reply = await session.reply("把改动正式应用到原项目。" + MISLEADING)
        self.session = session
        self.model = model
        return reply, "\n".join(_event_text(e) for e in events)


_CALL = {"type": "tool", "name": "apply_result", "input": {}}
_ECHO = {"type": "text", "text": "__echo_marker__"}


class ApplyResultConfirmTests(_ApplyCase):
    # 成功路：老板答 Y → 原文件按补丁改好，adoption.json 与命令行同字段
    async def test_yes_applies_and_records_adoption(self) -> None:
        reply, blob = await self._run([_CALL, _ECHO], self._responder("Y"))
        self.assertEqual(len(self.asked), 1)
        question = self.asked[0]
        # 确认文案来自真实结果包与 dry_run 文件清单，不是模型写的
        self.assertIn("apply2-run", question)
        self.assertIn(str(self.ws), question)
        self.assertIn("将改动 3 个文件", question)
        for item in ("修改 pkg/a.py", "新建 pkg/c.py", "删除 old.py"):
            self.assertIn(item, question)
        self.assertNotIn(MISLEADING, question)
        self.assertIn(question, blob)
        self.assertEqual(
            (self.ws / "pkg/a.py").read_text(encoding="utf-8"), "alpha\nBETA\n"
        )
        self.assertEqual(
            (self.ws / "pkg/c.py").read_text(encoding="utf-8"), "new file\n"
        )
        self.assertFalse((self.ws / "old.py").exists())
        adoption = self._adoption()
        # 与命令行 adopt 同字段（adopted/decided_at/note），apply 再加 applied 块
        self.assertTrue({"adopted", "decided_at", "note"} <= set(adoption))
        self.assertIs(adoption["adopted"], True)
        self.assertTrue(adoption["decided_at"])
        self.assertEqual(
            sorted(adoption["applied"]["files"]),
            ["old.py", "pkg/a.py", "pkg/c.py"],
        )
        self.assertIn("已把结果包", reply)
        tool = await self.session.agent.toolkit.get_tool("apply_result")
        self.assertEqual(tool.executions, 1)
        log = self._audit()
        self.assertIn('"action": "apply_result"', log)
        self.assertIn("答复 Y", log)
        self.assertIn("已执行", log)
        self.assertIn("apply2-run", log)

    # 没确认路：答 N → 原文件不变、adoption.json 仍未决定；同轮换路径再调不再问
    async def test_no_keeps_files_and_does_not_reask(self) -> None:
        second = {
            "type": "tool",
            "name": "apply_result",
            "input": {"bundle": str(self.bundle)},
        }
        await self._run([_CALL, second, _ECHO], self._responder("N"))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(_tree(self.ws), self.before)
        self.assertIsNone(self._adoption()["adopted"])
        self.assertNotIn("applied", self._adoption())
        tool = await self.session.agent.toolkit.get_tool("apply_result")
        self.assertEqual(tool.executions, 0)
        log = self._audit()
        self.assertIn('"action": "apply_result"', log)
        self.assertIn("答复 N", log)
        self.assertIn("未执行", log)

    # 无人可答：没挂应答者 → 按拒绝落地，一个字不写
    async def test_no_responder_is_denial(self) -> None:
        reply, _ = await self._run([_CALL, _ECHO], None, marker="按拒绝落地")
        self.assertEqual(_tree(self.ws), self.before)
        self.assertIsNone(self._adoption()["adopted"])
        self.assertIn("按拒绝落地", reply)

    # 无结果包路：明确拒绝、不打扰老板、原文件不变
    async def test_no_bundle_refuses(self) -> None:
        shutil.rmtree(self.ws / ".drbug" / "runs" / "bundles")
        self.before = _tree(self.ws)
        reply, _ = await self._run(
            [_CALL, _ECHO], self._responder("Y"), marker="还没有可应用的结果包"
        )
        self.assertEqual(self.asked, [])
        self.assertIn("还没有可应用的结果包", reply)
        self.assertEqual(_tree(self.ws), self.before)

    # 指纹对不上：原文件运行后被改 → 整包拒绝，被改后的样子一个字不被覆盖
    async def test_fingerprint_mismatch_refuses_whole_bundle(self) -> None:
        _write(self.ws, "pkg/a.py", "boss edited\n")
        self.before = _tree(self.ws)
        reply, _ = await self._run(
            [_CALL, _ECHO], self._responder("Y"), marker="原件在运行之后被改动"
        )
        self.assertEqual(self.asked, [])
        self.assertIn("原件在运行之后被改动", reply)
        self.assertEqual(
            (self.ws / "pkg/a.py").read_text(encoding="utf-8"), "boss edited\n"
        )
        self.assertEqual(_tree(self.ws), self.before)
        self.assertFalse((self.ws / "pkg/c.py").exists())
        self.assertIsNone(self._adoption()["adopted"])


class ApplyResultDirectTests(_ApplyCase):
    # 工具本体直接调（绕过确认回路）拿不到放行条 → 一个字不写
    def test_direct_call_without_release_writes_nothing(self) -> None:
        text = dispatcher.apply_result(workspace=str(self.ws))
        self.assertEqual(text, dispatcher.APPLY_NEEDS_CONFIRM)
        self.assertEqual(_tree(self.ws), self.before)
        self.assertIsNone(self._adoption()["adopted"])

    def test_registered_with_run_repair_and_accept_check(self) -> None:
        names = [tool.name for tool in dispatcher.build_tools()]
        self.assertIn("apply_result", names)
        self.assertIn("run_repair", names)
        self.assertIn("accept_check", names)


class ApplyResultTuiTests(_ApplyCase):
    """TUI 自己那份确认驱动：同样按工具名分流，弹程序拼的问题，Y 才写。"""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(dispatcher._bind_default_workspace, None)

    def _app(self):
        from core.tui import CifixChatApp
        from tests.test_tui import ScriptedToolModel

        model = ScriptedToolModel(
            [
                {"type": "tool", "name": "apply_result", "input": {}},
                {"type": "text", "text": "办完了"},
            ]
        )
        return CifixChatApp(model=model, workspace_root=self.ws)

    async def _ask_and_answer(self, key: str):
        from tests.test_tui import _log_text, _submit_text, _wait_until

        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "把改动正式应用到原项目。" + MISLEADING)
            await _wait_until(pilot, lambda: app._awaiting_confirm, "进入等待确认")
            await pilot.press(key)
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            return _log_text(app)

    async def test_tui_yes_applies(self) -> None:
        # 日志按宽度折行：去掉换行再比对
        log_text = (await self._ask_and_answer("y")).replace("\n", "")
        self.assertIn("确认把结果包 apply2-run 正式应用到原项目", log_text)
        self.assertIn("修改 pkg/a.py", log_text)
        self.assertNotIn("确认 secretary", log_text)
        self.assertEqual(
            (self.ws / "pkg/a.py").read_text(encoding="utf-8"), "alpha\nBETA\n"
        )
        self.assertIs(self._adoption()["adopted"], True)
        log = self._audit()
        self.assertIn('"action": "apply_result"', log)
        self.assertIn("答复 Y", log)
        self.assertIn("已执行", log)

    async def test_tui_no_writes_nothing(self) -> None:
        await self._ask_and_answer("n")
        self.assertEqual(_tree(self.ws), self.before)
        self.assertIsNone(self._adoption()["adopted"])
        log = self._audit()
        self.assertIn("答复 N", log)
        self.assertIn("未执行", log)


if __name__ == "__main__":
    unittest.main()
