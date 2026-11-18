"""SCOPE1a offline tests: fence on every profile + hash backstop.

The fixture repo/profile machinery is reused from tests.test_gen5 (the
re-export shape whose repro node is assigned to the generalist).  No
real model/API call is made.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.agentscope_actors import (
    _rollback_out_of_scope,
    _snapshot_tree,
    agentscope_act,
    roster_from_table,
)
from core.runner import _table_scope_files
from tests.test_agentscope_actors import ScriptedToolModel
from tests.test_gen5 import (
    BROKEN_FORMAT_LINE,
    FIXED_FORMAT_LINE,
    IGNORE,
    REPRO_NODE,
    _BuiltProfile,
    _write_reexport_shape,
)


class _RawWriteModel(ScriptedToolModel):
    """Scripted model that also writes one file directly, bypassing tools."""

    def __init__(self, steps: list[dict], raw_target: Path, raw_text: str) -> None:
        super().__init__(steps)
        self._raw_target = raw_target
        self._raw_text = raw_text
        self._raw_done = False

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        if not self._raw_done:
            self._raw_done = True
            self._raw_target.parent.mkdir(parents=True, exist_ok=True)
            self._raw_target.write_text(self._raw_text, encoding="utf-8")
        return await super()._call_api(
            model_name, messages, tools=tools, tool_choice=tool_choice, **kwargs
        )


class BackstopUnitTest(unittest.TestCase):
    def test_rollback_restores_out_of_scope_only(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="scope1a-unit-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        workspace = root / "ws"
        (workspace / "pkg").mkdir(parents=True)
        (workspace / "pkg" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (workspace / "pkg" / "b.py").write_text("B = 1\n", encoding="utf-8")
        (workspace / "pkg" / "gone.py").write_text("G = 1\n", encoding="utf-8")
        backup = root / "backup"
        shutil.copytree(workspace, backup)
        before = _snapshot_tree(workspace)

        (workspace / "pkg" / "a.py").write_text("A = 2\n", encoding="utf-8")
        (workspace / "pkg" / "b.py").write_text("B = 2\n", encoding="utf-8")
        (workspace / "pkg" / "gone.py").unlink()
        (workspace / "pkg" / "new.py").write_text("N = 1\n", encoding="utf-8")

        rolled = _rollback_out_of_scope(
            workspace, backup, before, ("pkg/a.py",)
        )

        self.assertEqual(
            sorted(rolled), ["pkg/b.py", "pkg/gone.py", "pkg/new.py"]
        )
        self.assertEqual(
            (workspace / "pkg" / "a.py").read_text(encoding="utf-8"), "A = 2\n"
        )
        self.assertEqual(
            (workspace / "pkg" / "b.py").read_text(encoding="utf-8"), "B = 1\n"
        )
        self.assertEqual(
            (workspace / "pkg" / "gone.py").read_text(encoding="utf-8"),
            "G = 1\n",
        )
        self.assertFalse((workspace / "pkg" / "new.py").exists())


class BackstopActTest(_BuiltProfile):
    """A raw write that bypasses the tool layer is rolled back + recorded."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="scope1a-act-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        shutil.copytree(self.repo, self.workspace, ignore=IGNORE)
        self.roster = roster_from_table(self.table)
        self.node_files, self.module_files = _table_scope_files(self.table)
        self.denials: list[dict] = []

    def _steps_edit_format(self) -> list[dict]:
        target = self.workspace / "mslib" / "format.py"
        return [
            {
                "type": "tool",
                "name": "Read",
                "input": {"file_path": str(target)},
            },
            {
                "type": "tool",
                "name": "Edit",
                "input": {
                    "file_path": str(target),
                    "old_string": BROKEN_FORMAT_LINE,
                    "new_string": FIXED_FORMAT_LINE,
                },
            },
            {"type": "text", "text": "done"},
        ]

    def test_raw_out_of_scope_change_is_rolled_back(self) -> None:
        convert = self.workspace / "mslib" / "convert.py"
        original = convert.read_text(encoding="utf-8")
        model = _RawWriteModel(
            self._steps_edit_format(),
            convert,
            original.replace('"h": 3600', '"h": 1'),
        )
        result = agentscope_act(
            "generalist",
            self.workspace,
            [REPRO_NODE],
            [REPRO_NODE],
            model=model,
            timeout_s=30,
            roster=self.roster,
            inventory=self.metadata["inventory"],
            strict_scope=True,
            node_files=self.node_files,
            module_files=self.module_files,
            scope_denials=self.denials,
        )
        # The in-scope Edit went through; the raw out-of-scope write did not.
        self.assertTrue(result.applied)
        self.assertIn("changed=mslib/format.py", result.note)
        self.assertNotIn("convert", result.note.split("changed=")[1].split(";")[0])
        self.assertEqual(convert.read_text(encoding="utf-8"), original)
        backstop = [
            item for item in self.denials if item["tool"] == "hash_backstop"
        ]
        self.assertEqual(len(backstop), 1)
        self.assertEqual(backstop[0]["path"], "mslib/convert.py")
        self.assertEqual(backstop[0]["actor_id"], "generalist")

    def test_raw_new_file_out_of_scope_is_deleted(self) -> None:
        evil = self.workspace / "mslib" / "evil.py"
        model = _RawWriteModel(
            self._steps_edit_format(), evil, "EVIL = 1\n"
        )
        result = agentscope_act(
            "generalist",
            self.workspace,
            [REPRO_NODE],
            [REPRO_NODE],
            model=model,
            timeout_s=30,
            roster=self.roster,
            inventory=self.metadata["inventory"],
            strict_scope=True,
            node_files=self.node_files,
            module_files=self.module_files,
            scope_denials=self.denials,
        )
        self.assertTrue(result.applied)
        self.assertFalse(evil.exists())
        backstop = [
            item for item in self.denials if item["tool"] == "hash_backstop"
        ]
        self.assertEqual(len(backstop), 1)
        self.assertEqual(backstop[0]["path"], "mslib/evil.py")


if __name__ == "__main__":
    unittest.main()
