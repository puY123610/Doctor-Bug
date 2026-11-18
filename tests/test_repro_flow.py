"""FLOW1 offline tests: symptom confirmation, freeze receipt, reject path.

No real model/API call is made.  Triage uses ``FakeChatModel``; the programmatic
verification gate and the post-repair re-check run real pytest subprocesses on
private copies of a temporary synthetic repository (the GEN4 fixture).

The user-facing contract under test:

1. before freezing, the platform shows "your symptom -> what actually happened
   on the original code -> what you expected", not the candidate test code;
2. freezing needs the user's explicit "yes, that is my problem" (terminal
   interaction defaults to refusal on EOF/anything else);
3. a frozen candidate leaves a receipt with path + sha256 + origin=repro and
   the TAMPERED warning, persisted next to the staging candidate;
4. "that is not my problem" voids the candidate and never freezes;
5. after a repair run, the same repro test is re-run and the before/after
   comparison is shown with "your own real use is the final judge".
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher
from core.cli import main as cli_main
from core.repro import (
    FROZEN,
    NO_CANDIDATE,
    REJECTED,
    REPRO_READY,
    ReproResult,
    comparison_lines,
    freeze_repro,
    load_candidate,
    load_receipt,
    propose_repro,
    reject_repro,
    repair_comparison_lines,
    verify_fix,
)
from tests.test_gen4 import GOOD_TEST, SYMPTOM, make_repo

SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")
BROKEN_LINE = "    return hours * 60\n"
FIXED_LINE = "    return hours * 60 + minutes\n"


def fix_duration_bug(repo: Path) -> None:
    target = repo / "sanity" / "durations.py"
    text = target.read_text(encoding="utf-8")
    if BROKEN_LINE not in text:
        raise AssertionError("fixture is not in the expected broken state")
    target.write_text(text.replace(BROKEN_LINE, FIXED_LINE, 1), encoding="utf-8")


class FlowCase(unittest.TestCase):
    """Shared synthetic repository plus an isolated profiles root."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="flow1-repro-")
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

    def propose(self, replies: list[str], *, name: str = "sanity"):
        return propose_repro(
            self.repo,
            SYMPTOM,
            model=FakeChatModel(replies=replies),
            name=name,
        )

    def freeze(self, *, name: str = "sanity"):
        result = self.propose([GOOD_TEST], name=name)
        self.assertEqual(result.status, REPRO_READY)
        frozen = freeze_repro(self.repo, name=name, confirm=True)
        self.assertEqual(frozen.status, FROZEN)
        return frozen

    def repaired_copy(self) -> Path:
        repaired = Path(tempfile.mkdtemp(dir=self.tmp)) / "repaired"
        shutil.copytree(self.repo, repaired)
        fix_duration_bug(repaired)
        return repaired


class ComparisonTest(FlowCase):
    def test_comparison_is_symptom_plus_verification_not_test_code(self) -> None:
        result = self.propose([GOOD_TEST])
        self.assertEqual(result.status, REPRO_READY)

        comparison = result.comparison

        self.assertEqual(comparison.symptom, SYMPTOM)
        self.assertIn("parse_duration('1h30m')", comparison.input_expr)
        self.assertIn("60", comparison.observed)
        self.assertIn("90", comparison.observed)
        self.assertIn("90", comparison.expected)

        lines = comparison_lines(comparison)
        self.assertIn("症状复现对照", lines[0])
        self.assertIn(SYMPTOM, lines[1])
        self.assertIn("实际得到 60", lines[2])
        self.assertIn("结果应当是 90", lines[3])
        rendered = "\n".join(lines)
        self.assertNotIn("def test_", rendered)
        self.assertIn("不作为确认依据", rendered)

    def test_comparison_without_assertion_evidence_stays_plain_and_safe(self) -> None:
        from core.repro import build_comparison

        comparison = build_comparison(SYMPTOM, GOOD_TEST, "")

        self.assertEqual(comparison.symptom, SYMPTOM)
        self.assertIn("断言失败", comparison.observed)
        self.assertIn("按你上面的描述", comparison.expected)
        self.assertNotIn("def test_", "\n".join(comparison_lines(comparison)))


class CliFlowTest(FlowCase):
    def _capture(self, argv: list[str], stdin_text: str | None = None):
        buffer = io.StringIO()
        patches = [
            mock.patch("core.model_config.load_local_env"),
            mock.patch.dict(os.environ),
        ]
        if stdin_text is not None:
            patches.append(mock.patch("core.cli.sys.stdin", io.StringIO(stdin_text)))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(contextlib.redirect_stdout(buffer))
            code = cli_main(argv)
        return code, buffer.getvalue()

    def test_unconfirmed_triage_shows_comparison_and_writes_no_repo_files(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ):
            code, output = self._capture(
                ["repro", str(self.repo), "--symptom", SYMPTOM]
            )

        self.assertEqual(code, 0)
        self.assertLess(
            output.index("症状复现对照"), output.index("技术细节：候选测试全文")
        )
        self.assertIn("实际得到 60", output)
        self.assertIn("结果应当是 90", output)
        self.assertIn("这复现的是不是你遇到的那个问题", output)
        self.assertIn("尚未写入目标仓库", output)
        self.assertIn("--confirm-freeze", output)
        self.assertFalse((self.repo / "tests").exists())
        self.assertFalse((self.profiles / "repo" / "vault").exists())
        self.assertIsNone(
            load_receipt(self.repo, name="repo", profiles_root=self.profiles)
        )

    def test_redirected_stdout_never_prompts_even_with_a_terminal_stdin(self) -> None:
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        fake_stdin.readline.side_effect = AssertionError(
            "redirected output must not prompt"
        )
        buffer = io.StringIO()
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ), mock.patch(
            "core.model_config.load_local_env"
        ), mock.patch(
            "core.cli.sys.stdin", fake_stdin
        ), contextlib.redirect_stdout(buffer):
            code = cli_main(["repro", str(self.repo), "--symptom", SYMPTOM])

        self.assertEqual(code, 0)
        fake_stdin.readline.assert_not_called()
        self.assertFalse((self.repo / "tests").exists())

    def test_interactive_yes_freezes_and_prints_the_receipt(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ), mock.patch("core.cli._stdin_is_interactive", return_value=True):
            code, output = self._capture(
                ["repro", str(self.repo), "--symptom", SYMPTOM],
                stdin_text="y\n",
            )

        self.assertEqual(code, 0)
        self.assertIn("冻结完成", output)
        self.assertIn("冻结回执", output)
        self.assertRegex(output, SHA256_RE)
        self.assertIn("origin=repro", output)
        self.assertIn("TAMPERED", output)
        self.assertIn("回执留档", output)

        destination = self.repo / "tests" / "test_repro_repo.py"
        self.assertEqual(destination.read_text(encoding="utf-8"), GOOD_TEST)
        sha = hashlib.sha256(destination.read_bytes()).hexdigest()
        self.assertIn(sha, output)
        receipt = load_receipt(
            self.repo, name="repo", profiles_root=self.profiles
        )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["sha256"], sha)
        self.assertEqual(receipt["origin"], "repro")
        self.assertIn("TAMPERED", receipt["note"])
        self.assertEqual(receipt["before"]["observed"].count("60"), 1)

    def test_interactive_eof_refuses_and_voids_the_candidate(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ), mock.patch("core.cli._stdin_is_interactive", return_value=True):
            code, output = self._capture(
                ["repro", str(self.repo), "--symptom", SYMPTOM],
                stdin_text="",
            )

        self.assertEqual(code, 1)
        self.assertIn("候选已作废", output)
        self.assertIn("未冻结", output)
        self.assertFalse((self.repo / "tests").exists())
        candidate = load_candidate(
            self.repo, name="repo", profiles_root=self.profiles
        )
        self.assertEqual(candidate["status"], REJECTED)

        refused = freeze_repro(
            self.repo, name="repo", confirm=True, profiles_root=self.profiles
        )
        self.assertEqual(refused.status, NO_CANDIDATE)
        self.assertFalse((self.repo / "tests").exists())

    def test_interactive_no_rejects_instead_of_freezing(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ), mock.patch("core.cli._stdin_is_interactive", return_value=True):
            code, output = self._capture(
                ["repro", str(self.repo), "--symptom", SYMPTOM],
                stdin_text="n\n",
            )

        self.assertEqual(code, 1)
        self.assertIn("补充", output)
        self.assertNotIn("冻结完成", output)
        self.assertFalse((self.repo / "tests").exists())
        self.assertEqual(
            load_candidate(
                self.repo, name="repo", profiles_root=self.profiles
            )["status"],
            REJECTED,
        )

    def test_confirm_freeze_still_freezes_and_shows_receipt(self) -> None:
        with mock.patch(
            "core.model_config.build_model",
            return_value=FakeChatModel(replies=[GOOD_TEST]),
        ):
            code, _ = self._capture(
                ["repro", str(self.repo), "--symptom", SYMPTOM]
            )
        self.assertEqual(code, 0)

        with mock.patch(
            "core.model_config.build_model",
            side_effect=AssertionError("model must not be called"),
        ):
            code, output = self._capture(
                [
                    "repro",
                    str(self.repo),
                    "--symptom",
                    SYMPTOM,
                    "--confirm-freeze",
                ]
            )

        self.assertEqual(code, 0)
        self.assertIn("症状复现对照", output)
        self.assertIn("冻结回执", output)
        self.assertRegex(output, SHA256_RE)
        self.assertTrue(
            (self.repo / "tests" / "test_repro_repo.py").is_file()
        )


class RejectTest(FlowCase):
    def test_reject_marks_the_candidate_and_blocks_freezing(self) -> None:
        result = self.propose([GOOD_TEST])
        self.assertEqual(result.status, REPRO_READY)

        rejected = reject_repro(
            self.repo, name="sanity", reason="用户说这不是他的问题"
        )

        self.assertEqual(rejected.status, REJECTED)
        self.assertIn("作废", rejected.reason)
        candidate = load_candidate(
            self.repo, name="sanity", profiles_root=self.profiles
        )
        self.assertEqual(candidate["status"], REJECTED)
        self.assertIn("这不是他的问题", candidate["rejection_reason"])

        refused = freeze_repro(
            self.repo, name="sanity", confirm=True, profiles_root=self.profiles
        )
        self.assertEqual(refused.status, NO_CANDIDATE)
        self.assertFalse((self.repo / "tests").exists())
        self.assertFalse((self.profiles / "sanity" / "vault").exists())

    def test_retriage_after_rejection_starts_from_a_fresh_candidate(self) -> None:
        self.propose([GOOD_TEST])
        reject_repro(self.repo, name="sanity", reason="not my problem")

        retried = propose_repro(
            self.repo,
            "parse_duration('1h30m') 返回的分钟数少了 30。",
            model=FakeChatModel(replies=[GOOD_TEST]),
            name="sanity",
        )

        self.assertEqual(retried.status, REPRO_READY)
        candidate = load_candidate(
            self.repo, name="sanity", profiles_root=self.profiles
        )
        self.assertEqual(candidate["status"], REPRO_READY)
        self.assertNotIn("rejection_reason", candidate)
        self.assertIn("少了 30", candidate["symptom"])


class DispatcherFlowTest(FlowCase):
    def test_propose_summary_presents_comparison_then_asks_about_symptom(self) -> None:
        fake = ReproResult(
            status=REPRO_READY,
            repo_path=self.repo,
            name="sanity",
            test_path=self.profiles / "sanity" / "repro" / "test_repro.py",
            test_source=GOOD_TEST,
            failing_output=(
                "E       AssertionError: assert 60 == 90\n"
                "E        +  where 60 = parse_duration('1h30m')"
            ),
            attempts=[{"attempt": 1, "verdict": "assertion_failed"}],
            reason="ok",
            final_test_name="test_repro_sanity.py",
            symptom=SYMPTOM,
        )
        with mock.patch(
            "core.repro.propose_repro", return_value=fake
        ), mock.patch("core.repro.freeze_repro") as freeze_mock:
            output = dispatcher.propose_repro(str(self.repo), SYMPTOM)

        self.assertIn("① 你原话的症状", output)
        self.assertIn("实际得到 60", output)
        self.assertIn("结果应当是 90", output)
        self.assertIn("这复现的是不是你遇到的那个问题", output)
        self.assertIn("freeze_repro(confirm=True)", output)
        self.assertLess(
            output.index("症状复现对照"), output.index("技术细节：候选测试全文")
        )
        self.assertIn("还没定下来", output)
        freeze_mock.assert_not_called()
        self.assertFalse((self.repo / "tests").exists())

    def test_freeze_refusal_asks_for_symptom_confirmation(self) -> None:
        with mock.patch("core.repro.freeze_repro") as freeze_mock:
            output = dispatcher.freeze_repro(str(self.repo), confirm=False)

        self.assertIn("拒绝", output)
        self.assertIn("症状复现对照", output)
        freeze_mock.assert_not_called()

    def test_freeze_summary_contains_receipt_fields(self) -> None:
        self.freeze(name="repo")

        output = dispatcher.freeze_repro(str(self.repo), confirm=True)

        self.assertIn("已定下来", output)
        self.assertIn("sha256:", output)
        self.assertRegex(output, SHA256_RE)
        self.assertIn("origin=repro", output)
        self.assertIn("回执留档", output)
        self.assertIn("TAMPERED", output)


class AfterRepairTest(FlowCase):
    def test_verify_fix_reruns_the_same_repro_test(self) -> None:
        self.freeze(name="sanity")
        broken = self.tmp / "still_broken"
        shutil.copytree(self.repo, broken)

        before = verify_fix(broken, name="sanity")
        self.assertIsNotNone(before)
        self.assertFalse(before.ok)
        self.assertIn("60", before.reason)

        after = verify_fix(self.repaired_copy(), name="sanity")
        self.assertIsNotNone(after)
        self.assertTrue(after.ok)
        self.assertEqual(after.test_name, "test_repro_sanity.py")

        lines = repair_comparison_lines(
            self.repo, self.repaired_copy(), name="sanity"
        )
        text = "\n".join(lines)
        self.assertIn("修复后复验对照", text)
        self.assertIn("修复前:", text)
        self.assertIn("实际得到 60", text)
        self.assertIn("修复后:", text)
        self.assertIn("通过", text)
        self.assertIn("最终以你自己的实际使用为准", text)

    def test_verify_fix_refuses_a_modified_frozen_test(self) -> None:
        frozen = self.freeze(name="sanity")
        copy = Path(tempfile.mkdtemp(dir=self.tmp)) / "tampered"
        shutil.copytree(self.repo, copy)
        target = copy / "tests" / "test_repro_sanity.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# edited after freeze\n",
            encoding="utf-8",
        )

        result = verify_fix(
            copy, name="sanity", expected_sha256=frozen.sha256
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "tampered")
        self.assertIn("TAMPERED", result.reason)

    def test_repair_lines_are_empty_for_non_repro_profiles(self) -> None:
        self.assertEqual(
            repair_comparison_lines(
                self.repo, self.repaired_copy(), name="sanity"
            ),
            [],
        )

    def _fake_record(self):
        record = mock.Mock()
        record.outcome = "PASS"
        record.rounds = [{"round": 1}]
        record.final_failed = []
        record.to_dict.return_value = {
            "arm": "C",
            "kb_enabled": True,
            "outcome": "PASS",
            "rounds": [],
            "final_failed": [],
        }
        return record

    def test_dispatcher_run_repair_summary_shows_before_after(self) -> None:
        self.freeze(name="repo")
        repaired = self.repaired_copy()
        record = self._fake_record()

        def fake_run_arm(**kwargs):
            kwargs["on_event"](
                {"event": "run_started", "workspace": str(repaired)}
            )
            return record

        with mock.patch(
            "core.dispatcher.run_arm", side_effect=fake_run_arm
        ):
            output = dispatcher.run_repair(
                arm="C",
                actors="scripted",
                workspace=str(self.repo),
                max_rounds=1,
                out=str(self.tmp / "tui_run.json"),
            )

        self.assertIn("修复运行完成", output)
        self.assertIn("修复后复验对照", output)
        self.assertIn("修复前:", output)
        self.assertIn("修复后:", output)
        self.assertIn("最终以你自己的实际使用为准", output)

    def test_cli_run_output_shows_before_after(self) -> None:
        self.freeze(name="repo")
        repaired = self.repaired_copy()
        record = self._fake_record()

        def fake_run_arm(**kwargs):
            kwargs["on_event"](
                {"event": "run_started", "workspace": str(repaired)}
            )
            return record

        out_path = self.tmp / "cli_run.json"
        buffer = io.StringIO()
        with mock.patch(
            "core.model_config.load_local_env"
        ), mock.patch(
            "core.runner.run_arm", side_effect=fake_run_arm
        ), mock.patch.dict(os.environ), contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "run",
                    "--arm",
                    "C",
                    "--workspace",
                    str(self.repo),
                    "--profile",
                    "auto",
                    "--max-rounds",
                    "1",
                    "--out",
                    str(out_path),
                ]
            )

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("终局汇总", output)
        self.assertIn("修复后复验对照", output)
        self.assertIn("修复前:", output)
        self.assertIn("修复后:", output)
        self.assertIn("最终以你自己的实际使用为准", output)
        self.assertTrue(out_path.is_file())


class PromptNamingTest(unittest.TestCase):
    """Model-visible prompts call the product Doctor Bug, not an old name (NAME1: was BugDoc)."""

    def test_dispatcher_and_triage_prompts_use_doctor_bug(self) -> None:
        from core.dispatcher import DISPATCHER_SYSTEM_PROMPT
        from core.repro import TRIAGE_SYSTEM_PROMPT

        combined = DISPATCHER_SYSTEM_PROMPT + TRIAGE_SYSTEM_PROMPT
        self.assertNotIn("cifix", combined.lower())
        self.assertIn("Doctor Bug", DISPATCHER_SYSTEM_PROMPT)
        self.assertIn("Doctor Bug", TRIAGE_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
