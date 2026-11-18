"""GEN5 offline tests: narrow evidence dispatch + repro-origin scope guard.

No real model/API call is made.  The native actor probes reuse the WIRE3
``ScriptedToolModel`` test double; every acceptance/profile check runs real
pytest on private temporary copies of synthetic repositories.

Fixture shapes:

* ``_write_reexport_shape``: a package whose ``__init__`` re-exports three
  submodules, plus one failing repro-style test that imports the public name
  from the package root.  Before GEN5 this fanned out to every module.
* ``_write_init_relay_shape``: the traceback goes
  ``test -> pkg/__init__.py (wrapper) -> pkg/impl.py``.  The package root is
  only a relay, so rule A1 must discard its frame.
* ``_write_dynamic_attr_shape``: ``from pkg import LAZY`` works at runtime
  through PEP 562 ``__getattr__`` but is not statically re-exported.  The
  import channel must record an ambiguity and assign no module at all.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.evidence import evidence_from_failed
from agentscope_ext.fake_model import FakeChatModel
from core.agentscope_actors import (
    agentscope_act,
    evidence_scope_files,
    roster_from_table,
)
from core.actors import FixResult
from core.autoprofile import build_profile
from core.repro import FROZEN, REPRO_READY, freeze_repro, propose_repro
from core.runner import (
    RunRecord,
    _table_scope_files,
    load_origin,
    run_arm,
)
from kb.lookup import DispatchKB
from tests.test_agentscope_actors import ScriptedToolModel

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

REPRO_FILE = "tests/test_repro_mslib.py"
REPRO_NODE = f"{REPRO_FILE}::test_format_span_includes_hours"
BROKEN_FORMAT_LINE = "    return f\"{minutes}m{seconds}s\""
FIXED_FORMAT_LINE = "    return \"1h30m\""


def _assigned(table: dict, node_id: str) -> list[str]:
    plan = DispatchKB(table).lookup(evidence_from_failed([node_id]))
    return sorted(
        actor
        for actor, nodes in plan.assignments.items()
        if node_id in nodes
    )


def _write_reexport_shape(root: Path) -> Path:
    """Package root re-exporting three submodules + one failing repro test."""
    package = root / "mslib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""Synthetic duration library; the root only re-exports."""\n\n'
        "from .convert import to_seconds\n"
        "from .format import format_span\n"
        "from .parse import parse_span\n",
        encoding="utf-8",
    )
    (package / "convert.py").write_text(
        '"""Conversions."""\n\n'
        'UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}\n\n\n'
        "def to_seconds(hours=0, minutes=0, seconds=0):\n"
        "    return hours * 3600 + minutes * 60 + seconds\n",
        encoding="utf-8",
    )
    (package / "format.py").write_text(
        '"""Formatting."""\n\n\n'
        "def format_span(total_seconds):\n"
        "    hours, remainder = divmod(total_seconds, 3600)\n"
        "    minutes, seconds = divmod(remainder, 60)\n"
        + BROKEN_FORMAT_LINE
        + "\n",
        encoding="utf-8",
    )
    (package / "parse.py").write_text(
        '"""Parsing."""\n\n\n'
        "def parse_span(text):\n"
        "    return text\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (root / REPRO_FILE).write_text(
        "from mslib import format_span\n\n\n"
        "def test_format_span_includes_hours():\n"
        '    assert format_span(5400) == "1h30m"\n'
        '    assert format_span(3600) == "1h"\n',
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n", encoding="utf-8"
    )
    return root


def _write_init_relay_shape(root: Path) -> Path:
    """Failure raised below a package-root wrapper function."""
    package = root / "weblib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .impl import compute\n\n\n"
        "def run(value):\n"
        "    return compute(value)\n",
        encoding="utf-8",
    )
    (package / "impl.py").write_text(
        '"""Real implementation."""\n\n\n'
        "def compute(value):\n"
        "    return 6 // (value - 3)\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_api.py").write_text(
        "from weblib import run\n\n\n"
        "def test_run():\n"
        "    assert run(3) == 6\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n", encoding="utf-8"
    )
    return root


def _write_dynamic_attr_shape(root: Path) -> Path:
    """``from pkg import LAZY`` works at runtime via module __getattr__."""
    package = root / "vaultlib"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .alpha import ALPHA\n\n\n"
        "def __getattr__(name):\n"
        "    if name == 'LAZY':\n"
        "        return 42\n"
        "    raise AttributeError(name)\n",
        encoding="utf-8",
    )
    (package / "alpha.py").write_text(
        '"""Alpha."""\n\nALPHA = 1\n', encoding="utf-8"
    )
    (package / "beta.py").write_text(
        '"""Beta."""\n\nBETA = 2\n', encoding="utf-8"
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_dyn.py").write_text(
        "from vaultlib import LAZY\n\n\n"
        "def test_dyn():\n"
        "    assert LAZY == 1\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n", encoding="utf-8"
    )
    return root


class _BuiltProfile(unittest.TestCase):
    """Shared machinery: build one profile and copy the repo per test."""

    origin = "repro"

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="gen5-profile-")
        cls.tmp = Path(cls._temporary.name)
        cls.repo = cls.make_repo(cls.tmp / "repo")
        cls.result = build_profile(
            cls.repo,
            out_root=cls.tmp / "profiles",
            name=cls.repo.name,
            origin=cls.origin,
        )
        cls.table = json.loads(
            cls.result.dispatch_table_path.read_text(encoding="utf-8")
        )
        cls.metadata = json.loads(
            cls.result.profile_json_path.read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        raise NotImplementedError


class Repo4ShapeProfileTest(_BuiltProfile):
    """A re-export package must dispatch a root-import failure narrowly."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def test_single_failure_is_assigned_to_the_defining_module_only(self) -> None:
        self.assertEqual(self.metadata["failed_nodes"], [REPRO_NODE])
        self.assertEqual(_assigned(self.table, REPRO_NODE), ["format-specialist"])

    def test_module_defaults_point_at_the_defining_module(self) -> None:
        self.assertEqual(
            self.table["module_defaults"]["repro_mslib"], ["format-specialist"]
        )

    def test_no_whole_package_override_survives(self) -> None:
        override = self.table["node_overrides"].get(REPRO_NODE)
        self.assertIsNone(override)

    def test_origin_is_recorded_for_repro_profiles(self) -> None:
        self.assertEqual(self.metadata["origin"], "repro")

    def test_exported_name_resolves_without_an_ambiguity(self) -> None:
        kinds = {item["kind"] for item in self.metadata["ambiguities"]}
        self.assertNotIn("unresolved_reexport", kinds)


class TracebackInitRelayTest(_BuiltProfile):
    """Rule A1: a package-root relay frame is not a root cause."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_init_relay_shape(root)

    def test_only_the_real_module_is_dispatched(self) -> None:
        node = self.metadata["failed_nodes"][0]
        self.assertEqual(_assigned(self.table, node), ["impl-specialist"])

    def test_rationale_keeps_a_non_init_package_frame(self) -> None:
        node = self.metadata["failed_nodes"][0]
        override = self.table["node_overrides"].get(node)
        # The single candidate equals the file default, so no override is
        # written; the rendered PROFILE.md line is the public evidence.
        markdown = self.result.markdown_path.read_text(encoding="utf-8")
        self.assertIn("`weblib/impl.py:", markdown)
        self.assertNotIn("`weblib/__init__.py:", markdown)
        self.assertIsNone(override)


class UnresolvedReexportTest(_BuiltProfile):
    """An unknown root-level name must never fan out to the whole package."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_dynamic_attr_shape(root)

    def test_unresolved_name_is_recorded_as_an_ambiguity(self) -> None:
        items = [
            item
            for item in self.metadata["ambiguities"]
            if item["kind"] == "unresolved_reexport"
        ]
        self.assertEqual(len(items), 1)
        self.assertIn("from vaultlib import LAZY", items[0]["detail"])
        markdown = self.result.markdown_path.read_text(encoding="utf-8")
        self.assertIn("`unresolved_reexport`", markdown)

    def test_unknown_name_does_not_assign_any_submodule_specialist(self) -> None:
        node = self.metadata["failed_nodes"][0]
        self.assertEqual(_assigned(self.table, node), ["generalist"])
        specialists = {
            item["id"] for item in self.table["specialists"]
        }
        self.assertNotIn("alpha-specialist", set(_assigned(self.table, node)))
        self.assertIn("alpha-specialist", specialists)


class ScopeGuardTest(_BuiltProfile):
    """GEN5 rule B: repro-origin profiles hard-limit every actor's writes."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen5-scope-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "workspace"
        shutil.copytree(self.repo, self.workspace, ignore=IGNORE)
        self.roster = roster_from_table(self.table)
        self.node_files, self.module_files = _table_scope_files(self.table)
        self.denials: list[dict] = []

    def _act(self, target: Path, old: str, new: str, *, strict: bool = True):
        model = ScriptedToolModel(
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
        result = agentscope_act(
            "generalist",
            self.workspace,
            [REPRO_NODE],
            [REPRO_NODE],
            model=model,
            timeout_s=30,
            roster=self.roster,
            inventory=self.metadata["inventory"],
            strict_scope=strict,
            node_files=self.node_files,
            module_files=self.module_files,
            scope_denials=self.denials,
        )
        return result, model

    def test_generalist_is_narrowed_to_the_evidence_module(self) -> None:
        self.assertGreater(
            len(self.roster.allowed_files("generalist")), 3
        )

        result, model = self._act(
            self.workspace / "mslib" / "format.py",
            BROKEN_FORMAT_LINE,
            FIXED_FORMAT_LINE,
        )

        self.assertTrue(result.applied)
        self.assertTrue(result.note.startswith("[scope-narrowed]"))
        self.assertIn("changed=mslib/format.py", result.note)
        prompt = model.invocations[0]["input_text"]
        self.assertIn("你只负责 mslib/format.py。", prompt)
        self.assertNotIn("mslib/convert.py", prompt)
        self.assertEqual(self.denials, [])

    def test_out_of_scope_edit_is_denied_recorded_and_file_unchanged(self) -> None:
        target = self.workspace / "mslib" / "convert.py"
        original = target.read_text(encoding="utf-8")

        result, _model = self._act(
            target,
            'UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}',
            'UNIT_SECONDS = {"h": 1, "m": 60, "s": 1}',
        )

        self.assertFalse(result.applied)
        self.assertIn("changed=none", result.note)
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(len(self.denials), 1)
        denial = self.denials[0]
        self.assertEqual(denial["actor_id"], "generalist")
        self.assertEqual(denial["tool"], "Edit")
        self.assertTrue(denial["path"].endswith("mslib/convert.py"))
        self.assertIn("roster files scope", denial["reason"])

    def test_every_actor_is_denied_outside_its_roster_files(self) -> None:
        target = self.workspace / "mslib" / "parse.py"
        original = target.read_text(encoding="utf-8")
        model = ScriptedToolModel(
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
                        "old_string": "    return text",
                        "new_string": "    return text.strip()",
                    },
                },
                {"type": "text", "text": "done"},
            ]
        )

        result = agentscope_act(
            "format-specialist",
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

        self.assertFalse(result.applied)
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(len(self.denials), 1)
        self.assertEqual(self.denials[0]["actor_id"], "format-specialist")
        self.assertNotIn("[scope-narrowed]", result.note)

    def test_tests_origin_keeps_the_old_unrestricted_generalist(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen5-tests-origin-")
        self.addCleanup(temporary.cleanup)
        tests_profile = build_profile(
            self.repo,
            out_root=Path(temporary.name) / "profiles",
            name="tests_origin",
        )
        metadata = json.loads(
            tests_profile.profile_json_path.read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["origin"], "tests")
        table = json.loads(
            tests_profile.dispatch_table_path.read_text(encoding="utf-8")
        )
        node_files, module_files = _table_scope_files(table)

        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Read",
                    "input": {
                        "file_path": str(self.workspace / "mslib" / "convert.py")
                    },
                },
                {
                    "type": "tool",
                    "name": "Edit",
                    "input": {
                        "file_path": str(self.workspace / "mslib" / "convert.py"),
                        "old_string": 'UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}',
                        "new_string": 'UNIT_SECONDS = {"h": 1, "m": 60, "s": 1}',
                    },
                },
                {"type": "text", "text": "done"},
            ]
        )
        result = agentscope_act(
            "generalist",
            self.workspace,
            [REPRO_NODE],
            [REPRO_NODE],
            model=model,
            timeout_s=30,
            roster=roster_from_table(table),
            inventory=metadata["inventory"],
            strict_scope=False,
            node_files=node_files,
            module_files=module_files,
            scope_denials=self.denials,
        )

        self.assertTrue(result.applied)
        self.assertIn("changed=mslib/convert.py", result.note)
        self.assertNotIn("[scope-narrowed]", result.note)
        self.assertEqual(self.denials, [])
        self.assertIn(
            '"h": 1',
            (self.workspace / "mslib" / "convert.py").read_text(
                encoding="utf-8"
            ),
        )


class ScopeEvidenceFilesTest(unittest.TestCase):
    def test_node_override_files_win_and_module_defaults_fill_the_rest(self) -> None:
        node_files = {"tests/test_a.py::test_x": ("pkg/a.py",)}
        module_files = {"b": ("pkg/b.py",)}
        files = evidence_scope_files(
            ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
            node_files,
            module_files,
        )
        self.assertEqual(files, ("pkg/a.py", "pkg/b.py"))

    def test_unknown_node_yields_no_scope(self) -> None:
        self.assertEqual(
            evidence_scope_files(["tests/test_x.py::test_y"], {}, {}), ()
        )


class RunnerScopeWiringTest(_BuiltProfile):
    """SCOPE1: the runner switches the guard on for every native run."""

    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def _run_with_fake_backend(self, profile_dir: Path):
        calls: list[dict] = []

        def fake_act(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
            calls.append({"actor_id": actor_id, **kwargs})
            sink = kwargs.get("scope_denials")
            if sink is not None:
                sink.append(
                    {
                        "actor_id": actor_id,
                        "tool": "Edit",
                        "path": "mslib/convert.py",
                        "reason": "path outside the roster files scope",
                    }
                )
            return FixResult(
                actor_id=actor_id, applied=False, note="offline denial"
            )

        with mock.patch(
            "core.agentscope_actors.agentscope_act", new=fake_act
        ):
            record = run_arm(
                "C",
                self.repo,
                max_rounds=1,
                work_root=self.tmp / f"work-{profile_dir.name}",
                actor_backend="agentscope",
                profile_dir=profile_dir,
            )
        return record, calls

    def test_repro_profile_turns_the_guard_on_and_records_denials(self) -> None:
        record, calls = self._run_with_fake_backend(self.result.profile_dir)

        self.assertEqual(record.outcome, "CAPPED")
        self.assertTrue(calls)
        self.assertTrue(all(call["strict_scope"] for call in calls))
        self.assertEqual(calls[0]["node_files"], {})
        self.assertEqual(
            calls[0]["module_files"]["repro_mslib"], ("mslib/format.py",)
        )
        self.assertEqual(len(record.scope_denials), len(calls))
        self.assertTrue(
            all(item["round"] == 1 for item in record.scope_denials)
        )
        payload = record.to_dict()
        self.assertEqual(payload["scope_denials"], record.scope_denials)

    def test_tests_profile_turns_the_guard_on_too(self) -> None:
        # SCOPE1: the guard is no longer gated on origin="repro"; a
        # tests-origin profile gets the same fence and denial sink.
        temporary = tempfile.TemporaryDirectory(prefix="gen5-tests-origin-")
        self.addCleanup(temporary.cleanup)
        tests_profile = build_profile(
            self.repo,
            out_root=Path(temporary.name) / "profiles",
            name="tests_origin",
        )

        record, calls = self._run_with_fake_backend(tests_profile.profile_dir)

        self.assertTrue(calls)
        self.assertTrue(all(call["strict_scope"] for call in calls))
        self.assertIsNotNone(calls[0]["scope_denials"])
        self.assertEqual(len(record.scope_denials), len(calls))
        self.assertEqual(record.to_dict()["scope_denials"], record.scope_denials)


class OriginFieldTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen5-origin-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def _profile(self, metadata: dict) -> Path:
        profile = self.tmp / "profile"
        profile.mkdir()
        (profile / "profile.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        return profile

    def test_missing_profile_means_tests(self) -> None:
        self.assertEqual(load_origin(None), "tests")
        self.assertEqual(load_origin(self.tmp / "absent"), "tests")

    def test_legacy_profile_without_the_field_means_tests(self) -> None:
        self.assertEqual(load_origin(self._profile({})), "tests")

    def test_repro_profile_is_read_back(self) -> None:
        self.assertEqual(
            load_origin(self._profile({"origin": "repro"})), "repro"
        )

    def test_old_run_records_still_get_an_empty_denial_list(self) -> None:
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        self.assertEqual(record.scope_denials, [])
        self.assertEqual(record.to_dict()["scope_denials"], [])


class FreezeReproOriginTest(unittest.TestCase):
    """B1: freeze_repro must stamp the generated profile as origin=repro."""

    SPEED_SOURCE = (
        '"""Tiny speed helpers used by the GEN5 fixture."""\n\n\n'
        "def format_speed(meters_per_second):\n"
        '    return f"{meters_per_second * 36}km/h"\n'
    )
    SPEED_TEST = (
        "from speedlib.speed import format_speed\n\n\n"
        "def test_format_speed():\n"
        '    assert format_speed(10) == "36km/h"\n'
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen5-freeze-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.profiles = self.tmp / "profiles"
        self.repo = self.tmp / "repo"
        (self.repo / "speedlib").mkdir(parents=True)
        (self.repo / "speedlib" / "__init__.py").write_text(
            '"""Speed package."""\n', encoding="utf-8"
        )
        (self.repo / "speedlib" / "speed.py").write_text(
            self.SPEED_SOURCE, encoding="utf-8"
        )

    def test_freezing_a_repro_marks_the_profile_as_repro(self) -> None:
        proposal = propose_repro(
            self.repo,
            "format_speed(10) 应返回 36km/h，但现在返回 360km/h。",
            model=FakeChatModel(replies=[self.SPEED_TEST]),
            name="speedlib",
            profiles_root=self.profiles,
        )
        self.assertEqual(proposal.status, REPRO_READY)

        frozen = freeze_repro(
            self.repo,
            name="speedlib",
            confirm=True,
            profiles_root=self.profiles,
        )

        self.assertEqual(frozen.status, FROZEN)
        metadata = json.loads(
            (frozen.profile_dir / "profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["origin"], "repro")


if __name__ == "__main__":
    unittest.main()
