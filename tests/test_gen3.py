"""GEN3 offline tests: dynamic teaming synthesizes specialists on roster gaps.

No real model/API call is made.  The actor probe reuses the WIRE3
``ScriptedToolModel`` test double; the runner probe replaces ``_run_agent``
with an offline coroutine so the real roster/inventory resolution, snapshots
and frozen acceptance still run.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from acceptor.evidence import evidence_from_failed
from core.agentscope_actors import (
    Roster,
    SpecialistProfile,
    agentscope_act,
    roster_from_table,
    synthesize_specialist,
)

DEMO_ROSTER = Roster(
    package="backoffice",
    legacy_prompt=True,
    specialists={
        "billing-specialist": SpecialistProfile(
            files=("backoffice/billing.py",),
            blurb=(
                "你是 billing-specialist（计费专家），负责订单计价、"
                "优惠券、税基与总额。"
            ),
        ),
        "inventory-specialist": SpecialistProfile(
            files=("backoffice/inventory.py",),
            blurb=(
                "你是 inventory-specialist（库存专家），负责库存、预留"
                "与可售数量核算。"
            ),
        ),
        "auth-specialist": SpecialistProfile(
            files=("backoffice/auth.py",),
            blurb="你是 auth-specialist（鉴权专家），负责角色判断与退款权限。",
        ),
        "db-specialist": SpecialistProfile(
            files=("backoffice/db.py",),
            blurb="你是 db-specialist（数据库专家），负责订单持久化与数据访问。",
        ),
        "generalist": SpecialistProfile(
            files=(
                "backoffice/billing.py",
                "backoffice/inventory.py",
                "backoffice/auth.py",
            ),
            blurb=(
                "你是 generalist（通用修复者），可以跨计费、库存、鉴权"
                "三个方向定位根因。"
            ),
            specialist=False,
        ),
    },
)
from core.autoprofile import build_profile
from core.cli import Printer as CliPrinter
from core.cli import _event_printer
from core.runner import load_inventory, run_arm
from kb.lookup import DispatchKB
from tests.test_agentscope_actors import ScriptedToolModel

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO2 = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo2"
COMMITTED_PROFILE2 = PLATFORM_ROOT / "profiles" / "demo_repo2"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

CONFLICT_NODE = (
    "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict"
)
BOOKING_NODE = (
    "tests/test_bookings.py::test_back_to_back_booking_in_same_room_is_allowed"
)
REPO2_FAILED = [
    "tests/test_bookings.py::test_book_and_cancel_releases_slot",
    "tests/test_bookings.py::test_rooms_do_not_share_availability",
    BOOKING_NODE,
    "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability",
    CONFLICT_NODE,
    "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases",
]

BROKEN_OVERLAP = (
    "    return first.start <= second.end and second.start <= first.end"
)
FIXED_OVERLAP = (
    "    return first.start < second.end and second.start < first.end"
)

_FIXTURE: dict = {}


def setUpModule() -> None:
    """Build one real demo_repo2 profile; every test reuses its artifacts."""
    temporary = tempfile.TemporaryDirectory(prefix="gen3-profile-")
    result = build_profile(
        DEMO_REPO2, out_root=Path(temporary.name) / "profiles", name="demo_repo2"
    )
    _FIXTURE.update(
        {
            "temporary": temporary,
            "result": result,
            "table": json.loads(
                result.dispatch_table_path.read_text(encoding="utf-8")
            ),
            "metadata": json.loads(
                result.profile_json_path.read_text(encoding="utf-8")
            ),
        }
    )


def tearDownModule() -> None:
    temporary = _FIXTURE.pop("temporary", None)
    if temporary is not None:
        temporary.cleanup()
    _FIXTURE.clear()


def _inventory() -> dict:
    return _FIXTURE["metadata"]["inventory"]


def _table_without_conflicts_entry() -> dict:
    """Gap fixture A: only the roster entry is missing (KB still names it)."""
    table = json.loads(json.dumps(_FIXTURE["table"]))
    del table["roster"]["specialists"]["conflicts-specialist"]
    return table


def _table_with_fallback_gap() -> dict:
    """Gap fixture B: roster and KB forget the module -> fallback generalist."""
    table = _table_without_conflicts_entry()
    table["module_defaults"].pop("conflicts", None)
    for override in table["node_overrides"].values():
        override["specialists"] = [
            actor
            for actor in override["specialists"]
            if actor != "conflicts-specialist"
        ]
    return table


def _copy_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    shutil.copytree(DEMO_REPO2, workspace, ignore=IGNORE)
    return workspace


def _conflicts_fix_model(workspace: Path) -> ScriptedToolModel:
    target = workspace / "src" / "studyroom" / "conflicts.py"
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
                    "old_string": BROKEN_OVERLAP,
                    "new_string": FIXED_OVERLAP,
                },
            },
            {"type": "text", "text": "fixed half-open overlap"},
        ]
    )


class SynthesizeSpecialistUnitTest(unittest.TestCase):
    def test_inventory_entry_becomes_a_one_file_specialist(self) -> None:
        entry = synthesize_specialist("conflicts", _inventory())

        self.assertIsNotNone(entry)
        actor_id, profile = entry
        self.assertEqual(actor_id, "conflicts-specialist")
        self.assertEqual(profile.files, ("src/studyroom/conflicts.py",))
        self.assertEqual(
            profile.blurb,
            "Conflict detection between a requested slot and existing bookings.",
        )
        self.assertTrue(profile.specialist)

    def test_empty_docstring_gets_the_neutral_sentence(self) -> None:
        entry = synthesize_specialist(
            "conflicts", {"conflicts": {"path": "src/x/conflicts.py"}}
        )
        self.assertEqual(entry[1].blurb, "负责 conflicts 模块的修复。")

    def test_module_outside_inventory_returns_none(self) -> None:
        self.assertIsNone(synthesize_specialist("end_to_end", _inventory()))
        self.assertIsNone(synthesize_specialist("conflicts", None))
        self.assertIsNone(synthesize_specialist("conflicts", {}))

    def test_unsafe_paths_are_rejected(self) -> None:
        for path in ("../escape.py", "/abs/conflicts.py", ""):
            with self.subTest(path=path):
                self.assertIsNone(
                    synthesize_specialist(
                        "conflicts", {"conflicts": {"path": path}}
                    )
                )


class InventoryOutputTest(unittest.TestCase):
    def test_profile_json_carries_the_inventory(self) -> None:
        inventory = _inventory()

        self.assertEqual(
            sorted(inventory),
            ["__init__", "bookings", "conflicts", "members", "slots"],
        )
        self.assertEqual(
            inventory["conflicts"],
            {
                "path": "src/studyroom/conflicts.py",
                "blurb": (
                    "Conflict detection between a requested slot and existing"
                    " bookings."
                ),
            },
        )
        for module, entry in inventory.items():
            with self.subTest(module=module):
                self.assertTrue((DEMO_REPO2 / entry["path"]).is_file())

    def test_dispatch_table_schema_and_bytes_are_unchanged(self) -> None:
        table = _FIXTURE["table"]

        self.assertEqual(
            set(table),
            {
                "version",
                "specialists",
                "node_overrides",
                "module_defaults",
                "fallback",
                "roster",
            },
        )
        self.assertNotIn("inventory", table)
        self.assertEqual(
            _FIXTURE["result"]
            .dispatch_table_path.read_bytes(),
            (COMMITTED_PROFILE2 / "dispatch_table.json").read_bytes(),
        )

    def test_load_inventory_reads_profile_json_and_tolerates_absence(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen3-inventory-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)

        self.assertEqual(
            load_inventory(_FIXTURE["result"].profile_dir), _inventory()
        )
        self.assertIsNone(load_inventory(base))
        (base / "profile.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(load_inventory(base))
        (base / "profile.json").write_text(
            json.dumps({"name": "no inventory"}), encoding="utf-8"
        )
        self.assertIsNone(load_inventory(base))
        self.assertIsNone(load_inventory(None))


class GapSynthesisActorTest(unittest.TestCase):
    """Compare the old behaviour with the synthesized one on real repo2 files."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen3-actor-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = _copy_workspace(self.tmp)
        self.gap_table = _table_without_conflicts_entry()
        self.gap_roster = roster_from_table(self.gap_table)

    def test_baseline_gap_without_inventory_keeps_the_old_keyerror(self) -> None:
        result = agentscope_act(
            "conflicts-specialist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=FakeChatModel(replies=["unused"]),
            timeout_s=30,
            roster=self.gap_roster,
        )

        self.assertFalse(result.applied)
        self.assertIn("error=KeyError", result.note)
        self.assertNotIn("[synthesized]", result.note)

    def test_gap_is_synthesized_scoped_and_applied(self) -> None:
        model = _conflicts_fix_model(self.workspace)

        result = agentscope_act(
            "conflicts-specialist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=model,
            timeout_s=30,
            roster=self.gap_roster,
            inventory=_inventory(),
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.actor_id, "conflicts-specialist")
        self.assertTrue(result.note.startswith("[synthesized] conflicts-specialist"))
        self.assertIn("files=src/studyroom/conflicts.py", result.note)
        self.assertIn("changed=src/studyroom/conflicts.py", result.note)
        prompt = model.invocations[0]["input_text"]
        self.assertIn("你只负责 src/studyroom/conflicts.py。", prompt)
        self.assertIn("只允许修改 src/studyroom/conflicts.py；", prompt)
        self.assertNotIn("src/studyroom/bookings.py", prompt)
        self.assertIn(FIXED_OVERLAP, (self.workspace / "src/studyroom/conflicts.py").read_text(encoding="utf-8"))

    def test_synthesized_scope_denies_out_of_scope_edit(self) -> None:
        target = self.workspace / "src" / "studyroom" / "members.py"
        original = target.read_text(encoding="utf-8")
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Edit",
                    "input": {
                        "file_path": str(target),
                        "old_string": "DAILY_BOOKING_LIMIT = 3",
                        "new_string": "DAILY_BOOKING_LIMIT = 999",
                    },
                },
                {"type": "text", "text": "tried members"},
            ]
        )

        result = agentscope_act(
            "conflicts-specialist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=model,
            timeout_s=30,
            roster=self.gap_roster,
            inventory=_inventory(),
        )

        self.assertFalse(result.applied)
        self.assertTrue(result.note.startswith("[synthesized]"))
        self.assertIn("changed=none", result.note)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_missing_inventory_module_falls_back_to_generalist(self) -> None:
        inventory = {
            key: value
            for key, value in _inventory().items()
            if key != "conflicts"
        }
        result = agentscope_act(
            "conflicts-specialist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=FakeChatModel(replies=["nothing to change"]),
            timeout_s=30,
            roster=self.gap_roster,
            inventory=inventory,
        )

        self.assertFalse(result.applied)
        self.assertTrue(result.note.startswith("[synthesis-miss]"))
        self.assertIn("fell back to generalist", result.note)

    def test_legacy_demo_roster_is_never_synthesized(self) -> None:
        result = agentscope_act(
            "nobody",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=FakeChatModel(replies=["unused"]),
            timeout_s=30,
            roster=DEMO_ROSTER,
            inventory=_inventory(),
        )

        self.assertFalse(result.applied)
        self.assertIn("error=KeyError", result.note)
        self.assertNotIn("[synthesized]", result.note)


class FallbackGeneralistUpgradeTest(unittest.TestCase):
    """The KB-fallback gap: generalist whole package -> synthesized specialist."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen3-fallback-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = _copy_workspace(self.tmp)
        self.gap_table = _table_with_fallback_gap()
        self.gap_roster = roster_from_table(self.gap_table)

    def test_lookup_really_falls_back_to_the_generalist(self) -> None:
        plan = DispatchKB(self.gap_table).lookup(
            evidence_from_failed([CONFLICT_NODE])
        )

        self.assertEqual(plan.assignments, {"generalist": [CONFLICT_NODE]})
        self.assertEqual(plan.unmapped, [CONFLICT_NODE])
        # Old behaviour: the generalist scope is the whole package.
        self.assertEqual(len(self.gap_roster.allowed_files("generalist")), 5)

    def test_fallback_assignment_is_upgraded_to_a_synthesized_specialist(self) -> None:
        model = _conflicts_fix_model(self.workspace)

        result = agentscope_act(
            "generalist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=model,
            timeout_s=30,
            roster=self.gap_roster,
            inventory=_inventory(),
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.actor_id, "generalist")
        self.assertTrue(
            result.note.startswith(
                "[synthesized] conflicts-specialist for module conflicts"
                " (assigned generalist)"
            )
        )
        self.assertIn("changed=src/studyroom/conflicts.py", result.note)
        prompt = model.invocations[0]["input_text"]
        self.assertIn("你只负责 src/studyroom/conflicts.py。", prompt)
        self.assertNotIn("你只负责 src/studyroom/__init__.py", prompt)

    def test_generalist_is_kept_with_a_complete_roster(self) -> None:
        roster = roster_from_table(_FIXTURE["table"])
        model = _conflicts_fix_model(self.workspace)

        result = agentscope_act(
            "generalist",
            self.workspace,
            [CONFLICT_NODE],
            REPO2_FAILED,
            model=model,
            timeout_s=30,
            roster=roster,
            inventory=_inventory(),
        )

        self.assertTrue(result.applied)
        self.assertNotIn("[synthesized]", result.note)
        prompt = model.invocations[0]["input_text"]
        self.assertIn("你是 generalist", prompt)
        self.assertIn("src/studyroom/bookings.py", prompt)

    def test_multi_module_generalist_assignment_is_not_narrowed(self) -> None:
        result = agentscope_act(
            "generalist",
            self.workspace,
            [CONFLICT_NODE, BOOKING_NODE],
            REPO2_FAILED,
            model=FakeChatModel(replies=["nothing to change"]),
            timeout_s=30,
            roster=self.gap_roster,
            inventory=_inventory(),
        )

        self.assertNotIn("[synthesized]", result.note)
        self.assertIn("changed=none", result.note)


class RunnerGapFlowTest(unittest.TestCase):
    """End-to-end offline flow: profile inventory -> roster gap -> synthesis."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen3-runner-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.result = _FIXTURE["result"]

    def _gap_profile_dir(self) -> Path:
        profile = self.tmp / "gap-profile"
        profile.mkdir()
        shutil.copytree(self.result.vault_dir, profile / "vault")
        (profile / "dispatch_table.json").write_text(
            json.dumps(_table_without_conflicts_entry(), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (profile / "profile.json").write_text(
            json.dumps(_FIXTURE["metadata"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return profile

    def _patched_run(self, profile_dir: Path):
        calls: list[dict] = []

        async def fake_run_agent(
            actor_id, system_prompt, workspace, model, allowed_files
        ):
            calls.append(
                {
                    "actor_id": actor_id,
                    "allowed_files": tuple(allowed_files),
                    "prompt": system_prompt,
                }
            )
            if "src/studyroom/conflicts.py" in allowed_files:
                target = Path(workspace) / "src" / "studyroom" / "conflicts.py"
                text = target.read_text(encoding="utf-8")
                if BROKEN_OVERLAP in text:
                    target.write_text(
                        text.replace(BROKEN_OVERLAP, FIXED_OVERLAP, 1),
                        encoding="utf-8",
                    )
            return f"offline {actor_id}"

        events: list[dict] = []
        with mock.patch(
            "core.model_config.build_model", return_value=object()
        ), mock.patch(
            "core.agentscope_actors._run_agent", new=fake_run_agent
        ):
            record = run_arm(
                "C",
                DEMO_REPO2,
                max_rounds=1,
                work_root=self.tmp / "work",
                actor_backend="agentscope",
                on_event=events.append,
                profile_dir=profile_dir,
            )
        return record, calls, events

    def test_gap_profile_synthesizes_and_repairs_through_the_runner(self) -> None:
        profile_dir = self._gap_profile_dir()
        table = json.loads(
            (profile_dir / "dispatch_table.json").read_text(encoding="utf-8")
        )
        plan = DispatchKB(table).lookup(evidence_from_failed(REPO2_FAILED))

        record, calls, events = self._patched_run(profile_dir)

        dispatches = [event for event in events if event["event"] == "dispatch"]
        self.assertEqual(len(dispatches), 1)
        # LOAD1：同一失败多候选时只派一人（累计派单数最少，平局按 lookup
        # 顺序）。单候选节点仍与 lookup 逐条一致；多候选节点只落在运行
        # 记录 load.picks 选中的那一位、且其在 lookup 候选之内。
        holders: dict[str, list[str]] = {}
        for actor, nodes in plan.assignments.items():
            for node in nodes:
                holders.setdefault(node, []).append(actor)
        picks = {
            item["node"]: item
            for item in record.rounds[0]["load"]["picks"]
        }
        self.assertEqual(
            set(picks), {node for node, who in holders.items() if len(who) > 1}
        )
        expected: dict[str, list[str]] = {}
        for actor, nodes in plan.assignments.items():
            kept = [
                node
                for node in nodes
                if len(holders[node]) == 1 or picks[node]["picked"] == actor
            ]
            if kept:
                expected[actor] = kept
        for node, item in picks.items():
            self.assertEqual(item["candidates"], holders[node])
        self.assertEqual(dispatches[0]["assignments"], expected)
        self.assertEqual(record.rounds[0]["assignments"], dispatches[0]["assignments"])

        synthesized = [
            call for call in calls if call["actor_id"] == "conflicts-specialist"
        ]
        self.assertEqual(len(synthesized), 1)
        self.assertEqual(
            synthesized[0]["allowed_files"],
            ("src/studyroom/conflicts.py",),
        )
        self.assertIn(
            "你只负责 src/studyroom/conflicts.py。", synthesized[0]["prompt"]
        )

        actor_done = [
            event
            for event in events
            if event["event"] == "actor_done"
            and event["actor_id"] == "conflicts-specialist"
        ]
        self.assertEqual(len(actor_done), 1)
        self.assertTrue(
            actor_done[0]["note"].startswith("[synthesized] conflicts-specialist")
        )
        self.assertTrue(actor_done[0]["applied"])

        results = {
            item["actor_id"]: item for item in record.rounds[0]["fix_results"]
        }
        self.assertTrue(results["conflicts-specialist"]["applied"])
        self.assertEqual(record.outcome, "CAPPED")
        self.assertNotIn(CONFLICT_NODE, record.final_failed)
        self.assertEqual(len(record.final_failed), 4)

        # The CLI listener must survive the synthesized note unchanged: no new
        # event kind was introduced, only the existing actor_done note grew.
        stream = io.StringIO()
        printer = _event_printer(CliPrinter(stream=stream))
        for event in events:
            printer(event)
        rendered = stream.getvalue()
        self.assertIn(
            "conflicts-specialist 完成 — [synthesized] conflicts-specialist",
            rendered,
        )

    def test_complete_profile_keeps_the_existing_behaviour(self) -> None:
        record, calls, events = self._patched_run(self.result.profile_dir)

        synthesized = [
            call for call in calls if call["actor_id"] == "conflicts-specialist"
        ]
        self.assertTrue(synthesized)
        for call in synthesized:
            self.assertEqual(
                call["allowed_files"], ("src/studyroom/conflicts.py",)
            )
        self.assertFalse(
            any("[synthesized]" in event.get("note", "") for event in events)
        )
        self.assertFalse(
            any(
                "[synthesized]" in item["note"]
                for item in record.rounds[0]["fix_results"]
            )
        )
        self.assertEqual(record.outcome, "CAPPED")
        self.assertNotIn(CONFLICT_NODE, record.final_failed)


if __name__ == "__main__":
    unittest.main()
