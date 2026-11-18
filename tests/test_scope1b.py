"""SCOPE1b offline tests: per-assignment grant, widening, summary.

Runner-level tests reuse the GEN5 re-export fixture with a doctored
profile whose only worker is the generalist (the forced-fallback
shape), plus a fake native backend that mimics the real act's
scope_state contract. Act-level tests use ScriptedToolModel.
No real model/API call is made.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.actors import FixResult
from core.agentscope_actors import agentscope_act, roster_from_table
from core.runner import _table_scope_files, run_arm
from tests.test_agentscope_actors import ScriptedToolModel
from tests.test_gen5 import (
    BROKEN_FORMAT_LINE,
    FIXED_FORMAT_LINE,
    IGNORE,
    REPRO_NODE,
    _BuiltProfile,
    _write_reexport_shape,
)


class _GeneralistOnlyProfile(_BuiltProfile):
    """Repro profile doctored so every failure lands on the generalist."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def _doctored_profile(self) -> Path:
        temporary = tempfile.TemporaryDirectory(prefix="scope1b-profile-")
        self.addCleanup(temporary.cleanup)
        profile = Path(temporary.name) / "profile"
        shutil.copytree(self.result.profile_dir, profile)
        table_path = profile / "dispatch_table.json"
        table = json.loads(table_path.read_text(encoding="utf-8"))
        table["roster"]["specialists"] = {
            name: config
            for name, config in table["roster"]["specialists"].items()
            if name == "generalist"
        }
        table["specialists"] = [
            item for item in table["specialists"] if item["id"] == "generalist"
        ]
        table["module_defaults"] = {
            module: ["generalist"] for module in table["module_defaults"]
        }
        table_path.write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return profile

    def _run_with_fake_backend(
        self,
        profile_dir: Path,
        *,
        notes: list[str],
        deny_rounds: set[int],
        max_rounds: int,
        generalist_grant: str = "per_assignment",
    ):
        calls: list[dict] = []

        def fake_act(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
            state = kwargs.get("scope_state") or {}
            calls.append(
                {
                    "actor_id": actor_id,
                    "grant": kwargs.get("generalist_grant"),
                    "widened_seen": bool(state.get("widened", False)),
                }
            )
            if actor_id == "generalist":
                state.setdefault("initial_files", ["mslib/format.py"])
                state.setdefault("narrowed", True)
                round_no = len(calls)
                sink = kwargs.get("scope_denials")
                if sink is not None and round_no in deny_rounds:
                    sink.append(
                        {
                            "actor_id": actor_id,
                            "tool": "Edit",
                            "path": "mslib/convert.py",
                            "reason": "path outside the roster files scope",
                        }
                    )
                note = notes[min(round_no - 1, len(notes) - 1)] if notes else "done"
                return FixResult(actor_id=actor_id, applied=False, note=note)

        with mock.patch(
            "core.agentscope_actors.agentscope_act", new=fake_act
        ):
            record = run_arm(
                "C",
                self.repo,
                max_rounds=max_rounds,
                work_root=self.tmp / f"work-{len(calls)}-{max_rounds}",
                actor_backend="agentscope",
                profile_dir=profile_dir,
                generalist_grant=generalist_grant,
            )
        return record, calls


class WideningRunnerTest(_GeneralistOnlyProfile):
    def test_denial_widens_next_round(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=["done"],
            deny_rounds={1},
            max_rounds=2,
        )
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0]["widened_seen"])
        self.assertTrue(calls[1]["widened_seen"])
        scope = record.generalist_scope
        self.assertTrue(scope["widened"])
        self.assertEqual(len(scope["widen_events"]), 1)
        self.assertEqual(scope["widen_events"][0]["trigger"], "denial")
        self.assertEqual(scope["widen_events"][0]["round"], 2)

    def test_valid_application_widens(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=[
                "reply=根因在格式化模块 WIDEN-APPLY: mslib/format.py | 需要改格式化实现"
            ],
            deny_rounds=set(),
            max_rounds=2,
        )
        scope = record.generalist_scope
        self.assertTrue(scope["widened"])
        self.assertEqual(scope["widen_events"][0]["trigger"], "widen_apply")
        self.assertEqual(
            scope["widen_events"][0]["path"], "mslib/format.py"
        )
        self.assertTrue(scope["widen_applications"][0]["valid"])

    def test_no_trigger_no_widen(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=["done"],
            deny_rounds=set(),
            max_rounds=2,
        )
        scope = record.generalist_scope
        self.assertFalse(scope["widened"])
        self.assertEqual(scope["widen_events"], [])

    def test_invalid_application_recorded_not_granted(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=[
                "reply=想改换算 WIDEN-APPLY: mslib/convert.py | 也许在换算表",
                "reply=本轮无申请",
            ],
            deny_rounds=set(),
            max_rounds=2,
        )
        scope = record.generalist_scope
        self.assertFalse(scope["widened"])
        self.assertEqual(len(scope["widen_applications"]), 1)
        self.assertFalse(scope["widen_applications"][0]["valid"])

    def test_widens_at_most_once(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=["done"],
            deny_rounds={1, 2, 3},
            max_rounds=3,
        )
        self.assertEqual(len(record.generalist_scope["widen_events"]), 1)

    def test_whole_package_mode_records_no_summary(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=["done"],
            deny_rounds={1},
            max_rounds=2,
            generalist_grant="whole_package",
        )
        self.assertEqual(calls[0]["grant"], "whole_package")
        self.assertEqual(record.generalist_scope, {})
        self.assertEqual(record.to_dict()["generalist_scope"], {})

    def test_summary_changed_beyond_initial(self) -> None:
        record, calls = self._run_with_fake_backend(
            self._doctored_profile(),
            notes=[
                "changed=none",
                "changed=mslib/format.py, mslib/convert.py",
            ],
            deny_rounds={1},
            max_rounds=2,
        )
        scope = record.generalist_scope
        self.assertEqual(scope["initial_files"], ["mslib/format.py"])
        self.assertTrue(scope["narrowed"])
        self.assertEqual(
            scope["changed_beyond_initial"], ["mslib/convert.py"]
        )
        payload = record.to_dict()
        self.assertEqual(payload["generalist_scope"], scope)


class GrantActTest(_BuiltProfile):
    """Act-level: prompt states the grant; widened grant acts package-wide."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="scope1b-act-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        shutil.copytree(self.repo, self.workspace, ignore=IGNORE)
        self.roster = roster_from_table(self.table)
        self.node_files, self.module_files = _table_scope_files(self.table)
        self.denials: list[dict] = []

    def _edit_model(self, target: Path, old: str, new: str) -> ScriptedToolModel:
        return ScriptedToolModel(
            [
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
                        "old_string": old,
                        "new_string": new,
                    },
                },
                {"type": "text", "text": "done"},
            ]
        )

    def test_narrowed_prompt_offers_widen_apply(self) -> None:
        target = self.workspace / "mslib" / "format.py"
        model = self._edit_model(target, BROKEN_FORMAT_LINE, FIXED_FORMAT_LINE)
        state: dict = {}
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
            scope_state=state,
        )
        self.assertTrue(result.applied)
        self.assertEqual(state["initial_files"], ["mslib/format.py"])
        self.assertTrue(state["narrowed"])
        prompt = model.invocations[0]["input_text"]
        self.assertIn("本次授权", prompt)
        self.assertIn("WIDEN-APPLY", prompt)

    def test_widened_generalist_acts_package_wide(self) -> None:
        target = self.workspace / "mslib" / "convert.py"
        model = self._edit_model(
            target,
            'UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}',
            'UNIT_SECONDS = {"h": 1, "m": 60, "s": 1}',
        )
        state: dict = {"widened": True}
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
            scope_state=state,
        )
        self.assertTrue(result.applied)
        self.assertNotIn("[scope-narrowed]", result.note)
        self.assertFalse(state["narrowed"])
        self.assertIn("mslib/convert.py", state["initial_files"])
        prompt = model.invocations[0]["input_text"]
        self.assertIn("已放宽到整个项目包", prompt)

    def test_whole_package_grant_never_narrows(self) -> None:
        target = self.workspace / "mslib" / "convert.py"
        model = self._edit_model(
            target,
            'UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}',
            'UNIT_SECONDS = {"h": 1, "m": 60, "s": 1}',
        )
        state: dict = {}
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
            generalist_grant="whole_package",
            scope_state=state,
        )
        self.assertTrue(result.applied)
        self.assertNotIn("[scope-narrowed]", result.note)
        self.assertNotIn("initial_files", state)


if __name__ == "__main__":
    unittest.main()
