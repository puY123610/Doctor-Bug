"""GEN4 offline tests: repro-first path for repositories without tests.

No real model/API call is made.  Triage Agents receive ``FakeChatModel`` (or
the WIRE3 ``ScriptedToolModel`` when tool use must be observed); the
programmatic verification gate runs real pytest subprocesses on private
copies of a temporary synthetic repository.

The synthetic repository has an obvious duration-parsing bug and zero test
files:

    sanity/durations.py   parse_duration("1h30m") returns 60 instead of 90

The fake triage model writes the correct repro test as a fenced code block;
the platform (not the model) judges whether it really fails.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import FrozenAcceptor
from acceptor.evidence import evidence_from_failed
from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher
from core.cli import main as cli_main
from core.runner import run_arm
from core.repro import (
    CANNOT_REPRODUCE,
    FROZEN,
    NO_CANDIDATE,
    REFUSED,
    REPRO_READY,
    FreezeResult,
    ReproResult,
    freeze_repro,
    propose_repro,
)
from kb.lookup import DispatchKB
from tests.test_agentscope_actors import ScriptedToolModel

SYMPTOM = "parse_duration('1h30m') 应返回 90 分钟，但现在返回 60。"
NODE = "tests/test_repro_sanity.py::test_parse_hours_and_minutes"

DURATIONS_SOURCE = '''\
"""Tiny duration helpers used by the GEN4 offline fixture."""


def parse_duration(text: str) -> int:
    """Return the number of minutes encoded as ``<h>h<min>m``."""
    hours = 0
    minutes = 0
    part = ""
    for char in text.strip():
        if char.isdigit():
            part += char
        elif char == "h":
            hours = int(part or 0)
            part = ""
        elif char == "m":
            minutes = int(part or 0)
            part = ""
        else:
            raise ValueError("invalid duration")
    return hours * 60
'''

GOOD_TEST = """\
from sanity.durations import parse_duration


def test_parse_hours_and_minutes():
    assert parse_duration("1h30m") == 90
"""

PASSING_TEST = """\
from sanity.durations import parse_duration


def test_current_behaviour():
    assert parse_duration("1h30m") == 60
"""

BAD_IMPORT_TEST = """\
from sanity.not_a_module import parse_duration


def test_broken_import():
    assert parse_duration("1h") == 60
"""

NON_ASSERTION_TEST = """\
def test_explodes():
    raise ValueError("boom")
"""


def make_repo(root: Path) -> Path:
    """Create a zero-test repository with one clear bug."""
    repo = root / "repo"
    package = repo / "sanity"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '"""GEN4 synthetic fixture package."""\n', encoding="utf-8"
    )
    (package / "durations.py").write_text(
        DURATIONS_SOURCE, encoding="utf-8"
    )
    return repo


class ReproCase(unittest.TestCase):
    """Shared temporary repository plus an isolated profiles root."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen4-repro-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.repo = make_repo(self.tmp)
        self.profiles = self.tmp / "profiles"
        for target in (
            "core.repro.PROFILES_ROOT",
            "core.cli.PROFILES_ROOT",
            "core.dispatcher.PROFILES_ROOT",
        ):
            patcher = mock.patch(target, self.profiles)
            patcher.start()
            self.addCleanup(patcher.stop)

    def propose(
        self,
        replies: list[str],
        *,
        max_attempts: int = 2,
        name: str = "sanity",
    ):
        model = FakeChatModel(replies=replies)
        result = propose_repro(
            self.repo,
            SYMPTOM,
            model=model,
            max_attempts=max_attempts,
            name=name,
        )
        return result, model

    def staging(self, name: str = "sanity") -> Path:
        return self.profiles / name / "repro"


class ProposeTest(ReproCase):
    def test_correct_candidate_is_verified_on_a_private_copy(self) -> None:
        result, model = self.propose([GOOD_TEST])

        self.assertEqual(result.status, REPRO_READY)
        self.assertEqual(result.final_test_name, "test_repro_sanity.py")
        self.assertEqual(
            result.test_path,
            self.staging() / "test_repro.py",
        )
        self.assertEqual(
            result.test_path.read_text(encoding="utf-8"), GOOD_TEST
        )
        self.assertEqual(
            [(item["attempt"], item["verdict"]) for item in result.attempts],
            [(1, "assertion_failed")],
        )
        self.assertEqual(model.call_count, 1)

        # The gate is a real pytest run on the *final* file name.
        self.assertIn(
            "E       AssertionError: assert 60 == 90",
            result.failing_output,
        )
        self.assertIn(NODE, result.failing_output)

        # The target repository was never written to.
        self.assertFalse((self.repo / "tests").exists())
        self.assertEqual(
            (self.repo / "sanity" / "durations.py").read_text(
                encoding="utf-8"
            ),
            DURATIONS_SOURCE,
        )

        candidate = json.loads(
            (self.staging() / "candidate.json").read_text(encoding="utf-8")
        )
        self.assertEqual(candidate["status"], REPRO_READY)
        self.assertEqual(candidate["test_source"], GOOD_TEST)
        attempts = json.loads(
            (self.staging() / "attempts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(attempts), 1)

    def test_triage_agent_only_gets_read_and_glob(self) -> None:
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Glob",
                    "input": {"pattern": "**/*.py"},
                },
                {
                    "type": "tool",
                    "name": "Read",
                    "input": {
                        "file_path": str(
                            self.repo / "sanity" / "durations.py"
                        )
                    },
                },
                {"type": "text", "text": GOOD_TEST},
            ]
        )

        result = propose_repro(
            self.repo, SYMPTOM, model=model, name="sanity"
        )

        self.assertEqual(result.status, REPRO_READY)
        self.assertEqual(len(model.invocations), 3)
        for invocation in model.invocations:
            self.assertEqual(invocation["tools"], ["Read", "Glob"])
            self.assertNotIn("Write", invocation["tools"])
            self.assertNotIn("Edit", invocation["tools"])
            self.assertNotIn("Bash", invocation["tools"])
        self.assertIn(SYMPTOM, model.invocations[0]["input_text"])
        self.assertIn("parse_duration", model.invocations[-1]["input_text"])

    def test_unfenced_valid_python_reply_is_accepted(self) -> None:
        raw = '"""Repro for the duration bug."""\n' + GOOD_TEST

        result, _model = self.propose([raw])

        self.assertEqual(result.status, REPRO_READY)
        self.assertEqual(result.test_source, raw)

    def test_passing_candidate_is_fed_back_and_rewritten(self) -> None:
        result, model = self.propose([PASSING_TEST, GOOD_TEST])

        self.assertEqual(result.status, REPRO_READY)
        self.assertEqual(
            [item["verdict"] for item in result.attempts],
            ["passed", "assertion_failed"],
        )
        self.assertEqual(model.call_count, 2)
        self.assertIn("没有复现", model.calls[1]["input_text"])
        self.assertIn("程序验真", model.calls[1]["input_text"])
        self.assertEqual(
            (self.staging() / "test_repro.py").read_text(encoding="utf-8"),
            GOOD_TEST,
        )

    def test_always_passing_candidate_ends_cannot_reproduce(self) -> None:
        result, model = self.propose([PASSING_TEST])

        self.assertEqual(result.status, CANNOT_REPRODUCE)
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(
            [item["verdict"] for item in result.attempts],
            ["passed", "passed"],
        )
        self.assertEqual(model.call_count, 2)
        self.assertIn("全部尝试记录已保留", result.reason)
        self.assertFalse((self.repo / "tests").exists())

        candidate = json.loads(
            (self.staging() / "candidate.json").read_text(encoding="utf-8")
        )
        self.assertEqual(candidate["status"], CANNOT_REPRODUCE)
        self.assertEqual(len(candidate["attempts"]), 2)
        attempts = json.loads(
            (self.staging() / "attempts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["verdict"] for item in attempts], ["passed", "passed"]
        )

    def test_collection_and_non_assertion_failures_do_not_count(self) -> None:
        result, _model = self.propose(
            [BAD_IMPORT_TEST, NON_ASSERTION_TEST]
        )

        self.assertEqual(result.status, CANNOT_REPRODUCE)
        self.assertEqual(
            [item["verdict"] for item in result.attempts],
            ["collection_error", "not_assertion"],
        )
        self.assertIn("ValueError", result.failing_output)
        self.assertIn("AssertionError", result.attempts[1]["reason"])

    def test_reply_without_a_code_block_is_rejected(self) -> None:
        result, model = self.propose(["我会写测试，但这次忘了代码块。"])

        self.assertEqual(result.status, CANNOT_REPRODUCE)
        self.assertEqual(
            [item["verdict"] for item in result.attempts],
            ["no_test_code", "no_test_code"],
        )
        self.assertEqual(model.call_count, 2)

    def test_empty_symptom_skips_the_model(self) -> None:
        model = FakeChatModel(replies=[GOOD_TEST])

        result = propose_repro(self.repo, "   ", model=model, name="sanity")

        self.assertEqual(result.status, CANNOT_REPRODUCE)
        self.assertEqual(result.attempts, [])
        self.assertIsNone(result.test_path)
        self.assertEqual(model.call_count, 0)
        self.assertIn("症状为空", result.reason)
        self.assertFalse((self.repo / "tests").exists())

    def test_repo_without_readable_source_skips_the_model(self) -> None:
        empty = self.tmp / "empty_repo"
        empty.mkdir()
        (empty / "README.md").write_text("no python here\n", encoding="utf-8")
        model = FakeChatModel(replies=[GOOD_TEST])

        result = propose_repro(empty, "anything", model=model, name="empty")

        self.assertEqual(result.status, CANNOT_REPRODUCE)
        self.assertEqual(result.attempts, [])
        self.assertEqual(model.call_count, 0)
        self.assertIn("可读源码", result.reason)

    def test_max_attempts_must_be_a_positive_integer(self) -> None:
        with self.assertRaises(ValueError):
            propose_repro(self.repo, SYMPTOM, model=None, max_attempts=0)
        with self.assertRaises(ValueError):
            propose_repro(self.repo, SYMPTOM, model=None, max_attempts=True)


class FreezeTest(ReproCase):
    def setUp(self) -> None:
        super().setUp()
        self.result, _model = self.propose([GOOD_TEST])
        self.assertEqual(self.result.status, REPRO_READY)

    def repo_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.repo).as_posix()
            for path in self.repo.rglob("*")
            if path.is_file()
        )

    def test_confirm_false_is_a_hard_refusal_with_evidence(self) -> None:
        before = self.repo_files()

        refused = freeze_repro(self.repo, name="sanity", confirm=False)

        self.assertEqual(refused.status, REFUSED)
        self.assertEqual(refused.test_source, GOOD_TEST)
        self.assertIn("AssertionError", refused.failing_output)
        self.assertIn("明确同意", refused.reason)
        self.assertEqual(self.repo_files(), before)
        self.assertFalse((self.repo / "tests").exists())

    def test_confirm_one_is_not_the_literal_true(self) -> None:
        refused = freeze_repro(self.repo, name="sanity", confirm=1)

        self.assertEqual(refused.status, REFUSED)
        self.assertFalse((self.repo / "tests").exists())

    def test_confirm_true_freezes_builds_profile_and_accepts_fail(self) -> None:
        frozen = freeze_repro(self.repo, name="sanity", confirm=True)

        self.assertEqual(frozen.status, FROZEN)
        destination = self.repo / "tests" / "test_repro_sanity.py"
        self.assertEqual(frozen.repo_test_path, destination)
        self.assertEqual(
            destination.read_text(encoding="utf-8"), GOOD_TEST
        )

        self.assertTrue((frozen.profile_dir / "dispatch_table.json").is_file())
        self.assertTrue(
            (frozen.vault_dir / "tests" / "test_repro_sanity.py").is_file()
        )
        manifest = json.loads(
            (frozen.vault_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("tests/test_repro_sanity.py", manifest["files"])
        self.assertEqual(manifest["package"], "sanity")

        metadata = json.loads(
            (frozen.profile_dir / "profile.json").read_text(encoding="utf-8")
        )
        self.assertIn(NODE, metadata["failed_nodes"])
        self.assertEqual(metadata["failed_count"], 1)

        table = json.loads(
            frozen.dispatch_table_path.read_text(encoding="utf-8")
        )
        plan = DispatchKB(table).lookup(evidence_from_failed([NODE]))
        self.assertEqual(
            plan.assignments,
            {"durations-specialist": [NODE]},
        )

        verdict = FrozenAcceptor(frozen.vault_dir).check(self.repo)
        self.assertEqual(verdict.status, "FAIL")
        self.assertIn(NODE, verdict.failed_tests)
        self.assertEqual(verdict.evidence[0].module, "repro_sanity")

    def test_frozen_profile_enters_the_existing_repair_loop(self) -> None:
        frozen = freeze_repro(self.repo, name="sanity", confirm=True)
        self.assertEqual(frozen.status, FROZEN)

        broken = "    return hours * 60\n"
        fixed = "    return hours * 60 + minutes\n"
        calls: list[tuple[str, tuple[str, ...]]] = []

        async def fake_run_agent(
            actor_id, system_prompt, workspace, model, allowed_files
        ):
            calls.append((actor_id, tuple(allowed_files)))
            target = Path(workspace) / "sanity" / "durations.py"
            text = target.read_text(encoding="utf-8")
            target.write_text(
                text.replace(broken, fixed, 1), encoding="utf-8"
            )
            return "fixed the minutes bug"

        events: list[dict] = []
        with mock.patch(
            "core.model_config.build_model", return_value=object()
        ), mock.patch(
            "core.agentscope_actors._run_agent", new=fake_run_agent
        ):
            record = run_arm(
                "C",
                self.repo,
                max_rounds=1,
                work_root=self.tmp / "work",
                actor_backend="agentscope",
                on_event=events.append,
                profile_dir=frozen.profile_dir,
            )

        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(
            calls, [("durations-specialist", ("sanity/durations.py",))]
        )
        verdicts = [
            event for event in events if event["event"] == "verdict"
        ]
        self.assertEqual(verdicts[0]["status"], "FAIL")
        self.assertEqual(verdicts[-1]["status"], "PASS")
        self.assertEqual(record.final_failed, [])

    def test_freeze_without_a_candidate_does_nothing(self) -> None:
        other = make_repo(self.tmp / "other")

        refused = freeze_repro(other, name="other", confirm=True)

        self.assertEqual(refused.status, NO_CANDIDATE)
        self.assertFalse((other / "tests").exists())

    def test_cannot_reproduce_candidate_is_not_freezable(self) -> None:
        repo2 = make_repo(self.tmp / "cannot")
        result = propose_repro(
            repo2,
            SYMPTOM,
            model=FakeChatModel(replies=[PASSING_TEST]),
            max_attempts=2,
            name="cannot",
        )
        self.assertEqual(result.status, CANNOT_REPRODUCE)

        refused = freeze_repro(repo2, name="cannot", confirm=True)

        self.assertEqual(refused.status, NO_CANDIDATE)
        self.assertFalse((repo2 / "tests").exists())

    def test_cross_repository_freeze_is_refused(self) -> None:
        other = make_repo(self.tmp / "cross")

        refused = freeze_repro(other, name="sanity", confirm=True)

        self.assertEqual(refused.status, NO_CANDIDATE)
        self.assertIn("另一个仓库", refused.reason)
        self.assertFalse((other / "tests").exists())


class CliReproTest(ReproCase):
    def _capture_main(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        # Keep the CLI tests fully offline: never read platform/.env.local.
        with mock.patch(
            "core.model_config.load_local_env"
        ), contextlib.redirect_stdout(buffer):
            code = cli_main(argv)
        return code, buffer.getvalue()

    def test_cli_prints_candidate_verdict_and_confirm_command(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ):
            code, output = self._capture_main(
                ["repro", str(self.repo), "--symptom", SYMPTOM]
            )

        self.assertEqual(code, 0)
        self.assertIn("REPRO_READY", output)
        self.assertIn("test_parse_hours_and_minutes", output)
        self.assertIn("AssertionError", output)
        self.assertIn("--confirm-freeze", output)
        self.assertIn("尚未写入目标仓库", output)
        self.assertFalse((self.repo / "tests").exists())

    def test_cli_confirm_freezes_an_existing_candidate_without_a_model(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ):
            code, _output = self._capture_main(
                ["repro", str(self.repo), "--symptom", SYMPTOM]
            )
        self.assertEqual(code, 0)

        with mock.patch(
            "core.model_config.build_model",
            side_effect=AssertionError("model must not be called"),
        ):
            code, output = self._capture_main(
                [
                    "repro",
                    str(self.repo),
                    "--symptom",
                    SYMPTOM,
                    "--confirm-freeze",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("冻结完成", output)
        self.assertIn("profile 路径", output)
        self.assertTrue(
            (self.repo / "tests" / "test_repro_repo.py").is_file()
        )

    def test_cli_confirm_runs_triage_first_when_no_candidate_exists(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ):
            code, output = self._capture_main(
                [
                    "repro",
                    str(self.repo),
                    "--symptom",
                    SYMPTOM,
                    "--confirm-freeze",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("先运行分诊", output)
        self.assertIn("冻结完成", output)
        self.assertTrue(
            (self.repo / "tests" / "test_repro_repo.py").is_file()
        )

    def test_cli_cannot_reproduce_returns_nonzero(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[PASSING_TEST]),
        ):
            code, output = self._capture_main(
                ["repro", str(self.repo), "--symptom", SYMPTOM]
            )

        self.assertEqual(code, 1)
        self.assertIn("CANNOT_REPRODUCE", output)
        self.assertFalse((self.repo / "tests").exists())


class DispatcherReproTest(ReproCase):
    def test_build_tools_includes_both_repro_tools(self) -> None:
        names = {tool.name for tool in dispatcher.build_tools()}

        self.assertIn("propose_repro", names)
        self.assertIn("freeze_repro", names)

    def test_propose_summary_never_freezes(self) -> None:
        fake = ReproResult(
            status=REPRO_READY,
            repo_path=self.repo,
            name="sanity",
            test_path=self.staging() / "test_repro.py",
            test_source=GOOD_TEST,
            failing_output="E       AssertionError: assert 60 == 90",
            attempts=[{"attempt": 1, "verdict": "assertion_failed"}],
            reason="ok",
            final_test_name="test_repro_sanity.py",
        )
        with mock.patch(
            "core.repro.propose_repro", return_value=fake
        ) as propose_mock, mock.patch(
            "core.repro.freeze_repro"
        ) as freeze_mock:
            output = dispatcher.propose_repro(str(self.repo), SYMPTOM)

        self.assertIn("REPRO_READY", output)
        self.assertIn("还没定下来", output)
        self.assertIn("test_parse_hours_and_minutes", output)
        propose_mock.assert_called_once()
        freeze_mock.assert_not_called()
        self.assertFalse((self.repo / "tests").exists())

    def test_freeze_gate_rejects_non_true_without_touching_disk(self) -> None:
        with mock.patch("core.repro.freeze_repro") as freeze_mock:
            for value in (False, 1, "yes", None):
                with self.subTest(confirm=value):
                    output = dispatcher.freeze_repro(
                        str(self.repo), confirm=value
                    )
                    self.assertIn("拒绝", output)
                    self.assertIn("确认", output)
        freeze_mock.assert_not_called()
        self.assertFalse((self.repo / "tests").exists())

    def test_freeze_confirm_true_summarizes_the_profile(self) -> None:
        fake = FreezeResult(
            status=FROZEN,
            repo_path=self.repo,
            name="sanity",
            reason="ok",
            repo_test_path=self.repo / "tests" / "test_repro_sanity.py",
            profile_dir=self.profiles / "sanity",
            dispatch_table_path=(
                self.profiles / "sanity" / "dispatch_table.json"
            ),
            vault_dir=self.profiles / "sanity" / "vault",
        )
        with mock.patch(
            "core.repro.freeze_repro", return_value=fake
        ) as freeze_mock:
            output = dispatcher.freeze_repro(str(self.repo), confirm=True)

        self.assertIn("已定下来", output)
        self.assertIn("profile", output)
        freeze_mock.assert_called_once_with(
            self.repo, confirm=True, profiles_root=self.profiles
        )


if __name__ == "__main__":
    unittest.main()
