"""MEM1: per-employee IDENTITY.md / MEMORY.md. Fully offline."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, FinishedReason

from agentscope_ext.fake_model import FakeChatModel
from core import api, dispatcher
from core.actors import FixResult

from core.agentscope_actors import roster_from_table as _rft

_DEMO_KB = (
    Path(__file__).resolve().parents[1] / "kb" / "dispatch_table.json"
)
_DEMO_ROSTER = _rft(json.loads(_DEMO_KB.read_text(encoding="utf-8")))
from core.agent_archive import (
    ROLE_GENERALIST,
    ROLE_SPECIALIST,
    append_memory_entries,
    ensure_archives,
    list_archives,
    read_archive,
)
from core.agentscope_actors import (
    Roster,
    SpecialistProfile,
    _MEMORY_INJECT_LIMIT,
    agentscope_act,
)
from core.autoprofile import build_profile, default_profile_name
from core.cli import main as cli_main
from core.dispatcher import build_dispatcher
from core.runner import RunRecord, _write_agent_memory, run_arm
from acceptor.acceptor import Verdict

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_RESPONSE_TIME = "1970-01-01T00:00:00.000000"
_NODES = [
    "tests/test_billing.py::test_a",
    "tests/test_billing.py::test_b",
    "tests/test_billing.py::test_c",
]


def _text_response(index: int, text: str) -> ChatResponse:
    return ChatResponse(
        content=[
            TextBlock(
                text=text,
                id=f"mem1-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"mem1-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


class ScriptedToolModel(FakeChatModel):
    """Issue scripted dispatcher tool calls, then a final text reply."""

    def __init__(self, steps: list[dict]) -> None:
        super().__init__(fallback_reply="script exhausted")
        self.steps = list(steps)
        self.seen_messages: list[list] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        index = len(self.seen_messages)
        self.seen_messages.append(list(messages))
        if not self.steps:
            return _text_response(index, "done")
        step = self.steps.pop(0)
        if step["type"] == "tool":
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"mem1-tool-{index:04d}",
                        name=step["name"],
                        input=json.dumps(step["input"], ensure_ascii=False),
                    )
                ],
                is_last=True,
                id=f"mem1-response-{index:04d}",
                created_at=_RESPONSE_TIME,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
                finished_reason=FinishedReason.COMPLETED,
            )
        return _text_response(index, step["text"])


def _tool_result_texts(messages: list) -> list[str]:
    texts: list[str] = []
    for message in messages:
        for block in message.get_content_blocks():
            if type(block).__name__ != "ToolResultBlock":
                continue
            for output in getattr(block, "output", []) or []:
                text = getattr(output, "text", "")
                if text:
                    texts.append(text)
    return texts


def _roster() -> dict:
    return {
        "billing-specialist": {
            "files": ["backoffice/billing.py"],
            "blurb": "计价",
            "specialist": True,
        },
        "generalist": {
            "files": ["backoffice/billing.py", "backoffice/inventory.py"],
            "blurb": "兜底",
            "specialist": False,
        },
    }


class ArchiveFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mem1-files-")
        self.addCleanup(temporary.cleanup)
        self.profile = Path(temporary.name) / "demo_repo"
        self.profile.mkdir()
        (self.profile / "profile.json").write_text(
            json.dumps({"origin": "tests", "name": "demo_repo"}),
            encoding="utf-8",
        )

    def test_ensure_creates_identity_and_empty_memory(self) -> None:
        names = ensure_archives(self.profile, _roster())

        self.assertEqual(names, ["billing-specialist", "generalist"])
        billing = read_archive(self.profile, "billing-specialist")
        self.assertIsNotNone(billing)
        assert billing is not None
        self.assertIn("员工名: billing-specialist", billing.identity)
        self.assertIn(f"角色: {ROLE_SPECIALIST}", billing.identity)
        self.assertIn("专长模块: billing", billing.identity)
        self.assertIn("backoffice/billing.py", billing.identity)
        self.assertIn("禁区:", billing.identity)
        self.assertIn("一切测试文件", billing.identity)
        self.assertIn("汇报线: 向主治医生汇报", billing.identity)
        self.assertIn(
            f"建档日期: {date.today().isoformat()}", billing.identity
        )
        self.assertIn("profile 来源: demo_repo（origin=tests）", billing.identity)
        self.assertIn("# MEMORY — billing-specialist", billing.memory)
        self.assertIn("## 经验", billing.memory)
        self.assertNotIn("测试 `", billing.memory)

        generalist = read_archive(self.profile, "generalist")
        assert generalist is not None
        self.assertIn(f"角色: {ROLE_GENERALIST}", generalist.identity)
        self.assertIn("专长模块: billing、inventory", generalist.identity)

    def test_ensure_does_not_overwrite_hand_edits(self) -> None:
        ensure_archives(self.profile, _roster())
        identity = self.profile / "agents" / "billing-specialist" / "IDENTITY.md"
        memory = self.profile / "agents" / "billing-specialist" / "MEMORY.md"
        identity.write_text(
            identity.read_text(encoding="utf-8") + "老板手改过身份\n",
            encoding="utf-8",
        )
        memory.write_text(
            memory.read_text(encoding="utf-8") + "老板手改过记忆\n",
            encoding="utf-8",
        )
        edited_identity = identity.read_text(encoding="utf-8")
        edited_memory = memory.read_text(encoding="utf-8")

        ensure_archives(self.profile, _roster())

        self.assertEqual(identity.read_text(encoding="utf-8"), edited_identity)
        self.assertEqual(memory.read_text(encoding="utf-8"), edited_memory)

    def test_append_dedups_test_and_file_and_keeps_identity(self) -> None:
        ensure_archives(self.profile, _roster())
        identity = (
            self.profile / "agents" / "billing-specialist" / "IDENTITY.md"
        ).read_text(encoding="utf-8")
        first = append_memory_entries(
            self.profile,
            "billing-specialist",
            [
                {
                    "test": "tests/test_billing.py::test_tax",
                    "file": "backoffice/billing.py",
                    "cause": "税基未扣优惠",
                },
                {
                    "test": "tests/test_billing.py::test_tax",
                    "file": "backoffice/billing.py",
                    "cause": "这是重复条目",
                },
            ],
        )
        self.assertEqual(len(first), 1)
        memory_path = (
            self.profile / "agents" / "billing-specialist" / "MEMORY.md"
        )
        text = memory_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("test_tax"), 1)
        self.assertIn(f"### {date.today().isoformat()}", text)
        self.assertIn("根因: 税基未扣优惠", text)
        self.assertNotIn("这是重复条目", text)

        second = append_memory_entries(
            self.profile,
            "billing-specialist",
            [
                {
                    "test": "tests/test_billing.py::test_tax",
                    "file": "backoffice/billing.py",
                    "cause": "再写一次也不该落盘",
                },
                {
                    "test": "tests/test_orders.py::test_checkout",
                    "file": "backoffice/billing.py",
                },
            ],
        )
        self.assertEqual(
            second,
            [
                {
                    "test": "tests/test_orders.py::test_checkout",
                    "file": "backoffice/billing.py",
                }
            ],
        )
        updated = memory_path.read_text(encoding="utf-8")
        self.assertEqual(updated.count("test_tax"), 1)
        self.assertIn("test_checkout", updated)
        self.assertNotIn("根因:", updated.split("test_checkout", 1)[1])
        self.assertEqual(
            (
                self.profile / "agents" / "billing-specialist" / "IDENTITY.md"
            ).read_text(encoding="utf-8"),
            identity,
        )

        unchanged = updated
        duplicate = append_memory_entries(
            self.profile,
            "billing-specialist",
            [
                {
                    "test": "tests/test_billing.py::test_tax",
                    "file": "backoffice/billing.py",
                }
            ],
        )
        self.assertEqual(duplicate, [])
        self.assertEqual(memory_path.read_text(encoding="utf-8"), unchanged)

    def test_missing_employee_reads_as_none(self) -> None:
        self.assertIsNone(read_archive(self.profile, "nobody"))
        self.assertEqual(list_archives(self.profile), [])


class BuildProfileArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mem1-profile-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        repo = self.tmp / "tiny"
        (repo / "tiny").mkdir(parents=True)
        (repo / "tiny" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "tiny" / "calc.py").write_text(
            "def add(left, right):\n    return left - right\n",
            encoding="utf-8",
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from tiny.calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 1) == 2\n",
            encoding="utf-8",
        )
        self.repo = repo

    def test_build_profile_files_every_specialist_and_preserves_edits(self) -> None:
        result = build_profile(
            self.repo, out_root=self.tmp / "profiles", name="tiny"
        )
        names = [item["id"] for item in result.dispatch_table["specialists"]]
        self.assertIn("generalist", names)
        self.assertIn("calc-specialist", names)
        for name in names:
            archive = read_archive(result.profile_dir, name)
            self.assertIsNotNone(archive, name)
            assert archive is not None
            self.assertIn(f"员工名: {name}", archive.identity)
            self.assertIn("向主治医生汇报", archive.identity)
            self.assertIn("## 经验", archive.memory)
        calc = read_archive(result.profile_dir, "calc-specialist")
        assert calc is not None
        self.assertIn(ROLE_SPECIALIST, calc.identity)
        self.assertIn("tiny/calc.py", calc.identity)
        generalist = read_archive(result.profile_dir, "generalist")
        assert generalist is not None
        self.assertIn(ROLE_GENERALIST, generalist.identity)

        identity_path = (
            result.profile_dir / "agents" / "calc-specialist" / "IDENTITY.md"
        )
        identity_path.write_text(
            identity_path.read_text(encoding="utf-8") + "手改保留\n",
            encoding="utf-8",
        )
        rebuilt = build_profile(
            self.repo, out_root=self.tmp / "profiles", name="tiny"
        )
        self.assertIn(
            "手改保留",
            (
                rebuilt.profile_dir / "agents" / "calc-specialist" / "IDENTITY.md"
            ).read_text(encoding="utf-8"),
        )


def _fail_verdict() -> Verdict:
    return Verdict(status="FAIL", failed_tests=list(_NODES), failed_count=3)


def _write_profile(profile: Path) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    table = {
        "version": 1,
        "fallback": "generalist",
        "specialists": [],
        "node_overrides": {},
        "module_defaults": {},
        "roster": {
            "package": "backoffice",
            "specialists": _roster(),
        },
    }
    (profile / "dispatch_table.json").write_text(
        json.dumps(table), encoding="utf-8"
    )
    ensure_archives(profile, table)


class MemoryWritebackTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mem1-write-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.profile = self.tmp / "profile"
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "marker.txt").write_text("workspace", encoding="utf-8")
        _write_profile(self.profile)

    def _run(self, verdicts: list[Verdict]) -> RunRecord:
        acceptor = mock.Mock()
        acceptor.check.side_effect = verdicts

        def fake_act(actor_id, workspace, assigned, failed):
            return FixResult(
                actor_id=actor_id, applied=True, note="fixed billing"
            )

        with mock.patch(
            "core.runner.FrozenAcceptor", return_value=acceptor
        ), mock.patch("core.runner.act", side_effect=fake_act):
            return run_arm(
                "A",
                self.src,
                max_rounds=1,
                work_root=self.tmp / "work",
                profile_dir=self.profile,
                vault_dir=self.profile,
                kb_path=self.profile / "dispatch_table.json",
            )

    def _memory(self) -> str:
        archive = read_archive(self.profile, "generalist")
        assert archive is not None
        return archive.memory

    def test_non_pass_run_writes_nothing_even_if_a_fix_was_applied(self) -> None:
        before = self._memory()

        record = self._run([_fail_verdict(), _fail_verdict()])

        self.assertEqual(record.outcome, "CAPPED")
        self.assertTrue(record.rounds[0]["fix_results"][0]["applied"])
        self.assertEqual(self._memory(), before)
        self.assertNotIn("测试 `", self._memory())

    def test_pass_run_appends_at_most_two_facts_and_dedups(self) -> None:
        snapshots: list[tuple[dict, dict]] = []
        original = _write_agent_memory

        def wrapped(record, profile_dir, workspace=None):
            before = json.loads(json.dumps(record.to_dict()))
            original(record, profile_dir, workspace)
            snapshots.append((before, json.loads(json.dumps(record.to_dict()))))

        with mock.patch("core.runner._write_agent_memory", side_effect=wrapped):
            record = self._run(
                [_fail_verdict(), Verdict(status="PASS", passed_count=3)]
            )

        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(snapshots[0][0], snapshots[0][1])
        self.assertEqual(snapshots[0][0]["outcome"], "PASS")
        self.assertEqual(
            set(record.to_dict()),
            {
                "arm",
                "kb_enabled",
                "outcome",
                "rounds",
                "final_failed",
                "scope_denials",
                "baseline_hashes",
                "generalist_scope",
                "review",
            },
        )
        text = self._memory()
        bullets = [
            line for line in text.splitlines() if line.startswith("- 测试 `")
        ]
        self.assertEqual(len(bullets), 2)
        self.assertIn("backoffice/billing.py", text)
        self.assertNotIn("根因:", text)
        self.assertIn(_NODES[0], text)
        self.assertIn(_NODES[1], text)
        self.assertNotIn(_NODES[2], text)

        again = self._run(
            [_fail_verdict(), Verdict(status="PASS", passed_count=3)]
        )
        self.assertEqual(again.outcome, "PASS")
        self.assertEqual(self._memory(), text)

    def test_writeback_failure_does_not_change_the_run(self) -> None:
        def boom(record, profile_dir, workspace=None):
            raise OSError("archive directory is not writable")

        acceptor = mock.Mock()
        acceptor.check.side_effect = [
            _fail_verdict(),
            Verdict(status="PASS", passed_count=3),
        ]
        with mock.patch(
            "core.runner.FrozenAcceptor", return_value=acceptor
        ), mock.patch(
            "core.runner.act",
            return_value=FixResult(
                actor_id="generalist", applied=True, note="fixed billing"
            ),
        ), mock.patch(
            "core.runner._write_agent_memory", side_effect=boom
        ):
            record = run_arm(
                "A",
                self.src,
                max_rounds=1,
                work_root=self.tmp / "work-boom",
                profile_dir=self.profile,
                vault_dir=self.profile,
                kb_path=self.profile / "dispatch_table.json",
            )

        self.assertEqual(record.outcome, "PASS")
        self.assertTrue(record.rounds[0]["fix_results"][0]["applied"])
        self.assertNotIn("测试 `", self._memory())

    def test_direct_hook_skips_non_pass_and_records_reply_cause(self) -> None:
        blocked = self.tmp / "not-a-directory"
        blocked.write_text("x", encoding="utf-8")
        noisy = RunRecord(
            arm="C",
            kb_enabled=True,
            outcome="TAMPERED",
            rounds=[
                {
                    "failed_tests": ["tests/test_billing.py::test_tax"],
                    "assignments": {
                        "billing-specialist": [
                            "tests/test_billing.py::test_tax"
                        ]
                    },
                    "fix_results": [
                        {
                            "actor_id": "billing-specialist",
                            "applied": True,
                            "note": (
                                "completed; changed=backoffice/billing.py; "
                                "reply=税基未扣优惠"
                            ),
                        }
                    ],
                }
            ],
        )
        _write_agent_memory(noisy, blocked)
        self.assertNotIn("测试 `", self._memory())

        passed = RunRecord(
            arm="C",
            kb_enabled=True,
            outcome="PASS",
            rounds=[
                {
                    "failed_tests": [
                        "tests/test_widget.py::test_one",
                    ],
                    "assignments": {
                        "generalist": ["tests/test_widget.py::test_one"]
                    },
                    "fix_results": [
                        {
                            "actor_id": "generalist",
                            "applied": True,
                            "note": (
                                "[synthesized] widget-specialist for module "
                                "widget (assigned generalist); completed; "
                                "changed=pkg/widget.py; reply=边界写成了 <="
                            ),
                        }
                    ],
                }
            ],
        )
        ensure_archives(
            self.profile,
            {
                "widget-specialist": {
                    "files": ["pkg/widget.py"],
                    "specialist": True,
                }
            },
        )
        _write_agent_memory(passed, self.profile)
        widget = read_archive(self.profile, "widget-specialist")
        assert widget is not None
        self.assertIn("tests/test_widget.py::test_one", widget.memory)
        self.assertIn("pkg/widget.py", widget.memory)
        self.assertIn("根因: 边界写成了 <=", widget.memory)
        self.assertNotIn("测试 `", self._memory())


class PromptInjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mem1-prompt-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = self.tmp / "ws"
        (self.workspace / "backoffice").mkdir(parents=True)
        (self.workspace / "backoffice" / "billing.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg" / "a.py").write_text("y = 1\n", encoding="utf-8")
        (self.workspace / "pkg" / "widget.py").write_text(
            "z = 1\n", encoding="utf-8"
        )
        self.profile = self.tmp / "profile"
        self.profile.mkdir()

    def test_archive_is_appended_and_missing_archive_is_a_no_op(self) -> None:
        ensure_archives(
            self.profile,
            {
                "billing-specialist": {
                    "files": ["backoffice/billing.py"],
                    "specialist": True,
                }
            },
        )
        memory = self.profile / "agents" / "billing-specialist" / "MEMORY.md"
        memory.write_text(
            memory.read_text(encoding="utf-8")
            + "\n上次修的是税基未扣优惠\n",
            encoding="utf-8",
        )
        model = FakeChatModel(replies=["no change"])
        agentscope_act(
            "billing-specialist",
            self.workspace,
            ["tests/test_billing.py::test_tax"],
            ["tests/test_billing.py::test_tax"],
            model=model,
            timeout_s=30,
            roster=_DEMO_ROSTER,
            profile_dir=self.profile,
        )
        prompt = model.calls[0]["input_text"]
        self.assertIn("【员工档案】", prompt)
        self.assertIn("员工名: billing-specialist", prompt)
        self.assertIn("上次修的是税基未扣优惠", prompt)

        bare = FakeChatModel(replies=["no change"])
        agentscope_act(
            "billing-specialist",
            self.workspace,
            ["tests/test_billing.py::test_tax"],
            ["tests/test_billing.py::test_tax"],
            model=bare,
            timeout_s=30,
            roster=_DEMO_ROSTER,
            profile_dir=self.tmp / "absent-profile",
        )
        bare_prompt = bare.calls[0]["input_text"]
        self.assertNotIn("【员工档案】", bare_prompt)
        self.assertNotIn("上次修的是税基未扣优惠", bare_prompt)
        self.assertIn("billing-specialist", bare_prompt)

    def test_memory_injection_stops_at_4000_characters(self) -> None:
        ensure_archives(
            self.profile,
            {
                "billing-specialist": {
                    "files": ["backoffice/billing.py"],
                    "specialist": True,
                }
            },
        )
        memory = self.profile / "agents" / "billing-specialist" / "MEMORY.md"
        memory.write_text("甲" * (_MEMORY_INJECT_LIMIT + 500), encoding="utf-8")
        model = FakeChatModel(replies=["no change"])
        agentscope_act(
            "billing-specialist",
            self.workspace,
            ["tests/test_billing.py::test_tax"],
            ["tests/test_billing.py::test_tax"],
            model=model,
            timeout_s=30,
            roster=_DEMO_ROSTER,
            profile_dir=self.profile,
        )
        prompt = model.calls[0]["input_text"]
        self.assertIn("甲" * _MEMORY_INJECT_LIMIT, prompt)
        self.assertNotIn("甲" * (_MEMORY_INJECT_LIMIT + 1), prompt)
        self.assertIn("MEMORY 已截断，仅注入前 4000 字", prompt)

    def test_synthesized_specialist_is_filed_before_the_prompt(self) -> None:
        roster = Roster(
            package="pkg",
            specialists={
                "generalist": SpecialistProfile(
                    files=("pkg/a.py",),
                    blurb="你是 generalist（通用修理工），兜底。",
                    specialist=False,
                )
            },
        )
        inventory = {
            "widget": {"path": "pkg/widget.py", "blurb": "负责 widget 模块。"}
        }
        model = FakeChatModel(replies=["no change"])
        result = agentscope_act(
            "widget-specialist",
            self.workspace,
            ["tests/test_widget.py::test_one"],
            ["tests/test_widget.py::test_one"],
            model=model,
            timeout_s=30,
            roster=roster,
            inventory=inventory,
            profile_dir=self.profile,
        )
        self.assertTrue(result.note.startswith("[synthesized] widget-specialist"))
        identity_path = (
            self.profile / "agents" / "widget-specialist" / "IDENTITY.md"
        )
        identity = identity_path.read_text(encoding="utf-8")
        self.assertIn(ROLE_SPECIALIST, identity)
        self.assertIn("pkg/widget.py", identity)
        self.assertIn("向主治医生汇报", identity)
        self.assertIn("禁区:", identity)
        prompt = model.calls[0]["input_text"]
        self.assertIn("员工名: widget-specialist", prompt)
        self.assertIn("MEMORY — widget-specialist", prompt)

        identity_path.write_text(identity + "老板手改过\n", encoding="utf-8")
        again = FakeChatModel(replies=["no change"])
        agentscope_act(
            "widget-specialist",
            self.workspace,
            ["tests/test_widget.py::test_one"],
            ["tests/test_widget.py::test_one"],
            model=again,
            timeout_s=30,
            roster=roster,
            inventory=inventory,
            profile_dir=self.profile,
        )
        kept = identity_path.read_text(encoding="utf-8")
        self.assertEqual(kept.count("老板手改过"), 1)
        self.assertIn("老板手改过", again.calls[0]["input_text"])


class ArchiveFacadeTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mem1-facade-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "acme"
        self.workspace.mkdir()
        self.profile = (
            self.workspace
            / ".drbug"
            / "profiles"
            / default_profile_name(self.workspace)
        )
        self.profile.mkdir(parents=True)
        ensure_archives(self.profile, _roster())
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.addCleanup(dispatcher.set_event_sink, None)

    def test_api_lists_and_reads_archives_without_creating_storage(self) -> None:
        listed = api.list_agent_archives(str(self.workspace))
        self.assertEqual(
            [item["name"] for item in listed],
            ["billing-specialist", "generalist"],
        )
        billing = api.get_agent_archive(str(self.workspace), "billing-specialist")
        self.assertTrue(billing["found"])
        self.assertIn("员工名: billing-specialist", billing["identity"])
        self.assertIn("## 经验", billing["memory"])
        missing = api.get_agent_archive(str(self.workspace), "nobody")
        self.assertFalse(missing["found"])
        self.assertIn("没有这个员工: nobody", missing["message"])
        self.assertEqual(missing["identity"], "")
        self.assertFalse((self.profile / "agents" / "nobody").exists())

    def test_cli_agents_lists_names(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["agents", str(self.workspace)])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("billing-specialist", output)
        self.assertIn("generalist", output)
        self.assertIn("共 2 名", output)
        self.assertNotIn("DEEPSEEK", output)

        empty = self.workspace.parent / "empty-proj"
        empty.mkdir()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["agents", str(empty)])
        self.assertEqual(code, 0)
        self.assertIn("没有员工档案", buffer.getvalue())

    def test_dispatcher_tool_round_trip(self) -> None:
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "show_agent_archive",
                    "input": {"name": "billing-specialist"},
                },
                {
                    "type": "tool",
                    "name": "show_agent_archive",
                    "input": {"name": "nobody"},
                },
                {"type": "text", "text": "档案已转达"},
            ]
        )
        agent = build_dispatcher(model=model, workspace=str(self.workspace))
        reply = asyncio.run(
            agent.reply(
                Msg(
                    name="用户",
                    role="user",
                    content=[TextBlock(text="看看 billing-specialist 的档案")],
                )
            )
        )
        self.assertEqual(reply.get_text_content(), "档案已转达")
        names = [tool.name for tool in dispatcher.build_tools()]
        self.assertIn("show_agent_archive", names)
        results = [
            text
            for messages in model.seen_messages
            for text in _tool_result_texts(messages)
        ]
        self.assertTrue(
            any("员工名: billing-specialist" in text for text in results),
            results,
        )
        self.assertTrue(
            any("## 经验" in text for text in results),
            results,
        )
        self.assertTrue(
            any("没有这个员工: nobody" in text for text in results),
            results,
        )

    def test_scripted_and_llm_modules_do_not_load_archives(self) -> None:
        for relative in ("core/actors.py", "core/llm_actors.py"):
            text = (PLATFORM_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("agent_archive", text)
            self.assertNotIn("IDENTITY.md", text)
            self.assertNotIn("MEMORY.md", text)


if __name__ == "__main__":
    unittest.main()
