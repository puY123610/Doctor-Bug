"""TEAM1: roster visibility plus boss add/remove. Fully offline."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, FinishedReason

from acceptor.evidence import evidence_from_failed
from agentscope_ext.fake_model import FakeChatModel
from core import api, dispatcher
from core.actors import CAN_FIX
from core.agent_archive import ensure_archives, read_archive
from core.agentscope_actors import (
    _overlay_blocked_modules,
    _resolve_actor,
    agentscope_act,
    roster_from_table,
)
from core.autoprofile import build_profile, default_profile_name, state_root_for
from core.cli import main as cli_main
from core.dispatcher import build_dispatcher
from core.roster import (
    ZERO_EXPERT_WARNING,
    RosterError,
    add_worker,
    list_roster,
    remove_worker,
)
from core.runner import RunRecord
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
_RESPONSE_TIME = "1970-01-01T00:00:00.000000"
_GENERAL_BLURB = (
    "你是 generalist（通用修理工），在没有明确归因或需要跨模块兜底时"
    "做最小修复。"
)
_BILLING_NODE = "tests/test_billing.py::test_tax_on_discounted_total"
_RECORD_KEYS = {
    "arm",
    "kb_enabled",
    "outcome",
    "rounds",
    "final_failed",
    "scope_denials",
    "baseline_hashes",
    "generalist_scope",
    "review",
}


def _text_response(index: int, text: str) -> ChatResponse:
    return ChatResponse(
        content=[
            TextBlock(
                text=text,
                id=f"team1-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"team1-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


class ScriptedToolModel(FakeChatModel):
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
                        id=f"team1-tool-{index:04d}",
                        name=step["name"],
                        input=json.dumps(step["input"], ensure_ascii=False),
                    )
                ],
                is_last=True,
                id=f"team1-response-{index:04d}",
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plant(root: Path) -> tuple[Path, Path]:
    """Tiny package plus a profile whose specialists have no source field."""
    workspace = root / "acme"
    _write(workspace / "pkg" / "__init__.py", "")
    _write(workspace / "pkg" / "kept.py", '"""Kept module."""\n')
    _write(workspace / "pkg" / "extra.py", '"""Extra helper."""\n')
    _write(workspace / "pkg" / "widget.py", '"""Widget module."""\n')
    profile = (
        state_root_for(workspace)
        / "profiles"
        / default_profile_name(workspace)
    )
    profile.mkdir(parents=True)
    table = {
        "version": 1,
        "fallback": "generalist",
        "module_defaults": {"kept": ["kept-specialist"]},
        "node_overrides": {
            "tests/test_kept.py::test_one": {
                "specialists": ["kept-specialist"],
                "rationale": "fixture",
                "source": "unit",
            }
        },
        "specialists": [
            {
                "id": "kept-specialist",
                "name": "kept 专家",
                "scope": "pkg/kept.py：Kept module.",
                "files": ["pkg/kept.py"],
                "blurb": "Kept module.",
            },
            {
                "id": "generalist",
                "name": "通用修理工",
                "scope": "兜底",
                "files": ["pkg/__init__.py", "pkg/kept.py", "pkg/widget.py"],
                "blurb": _GENERAL_BLURB,
            },
        ],
        "roster": {
            "package": "pkg",
            "symptoms": "",
            "specialists": {
                "kept-specialist": {
                    "files": ["pkg/kept.py"],
                    "blurb": "Kept module.",
                    "specialist": True,
                },
                "generalist": {
                    "files": ["pkg/__init__.py", "pkg/kept.py", "pkg/widget.py"],
                    "blurb": _GENERAL_BLURB,
                    "specialist": False,
                },
            },
        },
    }
    _write(
        profile / "dispatch_table.json",
        json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write(
        profile / "profile.json",
        json.dumps(
            {
                "name": default_profile_name(workspace),
                "origin": "tests",
                "specialists": ["kept-specialist", "generalist"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return workspace, profile


def _table(profile: Path) -> dict:
    return json.loads(
        (profile / "dispatch_table.json").read_text(encoding="utf-8")
    )


class RosterReadAndEditTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team1-roster-")
        self.addCleanup(temporary.cleanup)
        self.workspace, self.profile = _plant(Path(temporary.name))

    def test_three_sources_are_marked_from_the_existing_profile(self) -> None:
        roster = roster_from_table(_table(self.profile))
        result = agentscope_act(
            "widget-specialist",
            self.workspace,
            ["tests/test_widget.py::test_one"],
            ["tests/test_widget.py::test_one"],
            model=FakeChatModel(replies=["no change"]),
            timeout_s=30,
            roster=roster,
            inventory={
                "widget": {
                    "path": "pkg/widget.py",
                    "blurb": "Widget module.",
                }
            },
            profile_dir=self.profile,
        )
        self.assertTrue(result.note.startswith("[synthesized] widget-specialist"))
        identity = read_archive(self.profile, "widget-specialist")
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertIn("profile 来源: synthesized", identity.identity)
        self.assertIn("pkg/widget.py", identity.identity)

        added = add_worker(self.workspace, "extra-hand", "pkg/extra.py")
        self.assertTrue(added["ok"])
        self.assertEqual(added["source"], "manual")
        self.assertEqual(added["scope_files"], ["pkg/extra.py"])
        self.assertEqual(added["evidence_module"], "extra")
        archive = read_archive(self.profile, "extra-hand")
        self.assertIsNotNone(archive)
        assert archive is not None
        self.assertIn("profile 来源: manual", archive.identity)
        self.assertIn("pkg/extra.py", archive.identity)

        rows = {row["name"]: row for row in list_roster(self.workspace)}
        self.assertEqual(rows["kept-specialist"]["source"], "auto")
        self.assertEqual(rows["kept-specialist"]["role"], "模块专家")
        self.assertEqual(rows["kept-specialist"]["module"], "kept")
        self.assertEqual(rows["kept-specialist"]["scope_files"], ["pkg/kept.py"])
        self.assertEqual(rows["generalist"]["source"], "auto")
        self.assertEqual(rows["generalist"]["role"], "通用修理工")
        self.assertEqual(rows["widget-specialist"]["source"], "synthesized")
        self.assertTrue(rows["widget-specialist"]["has_archive"])
        self.assertIn("pkg/widget.py", rows["widget-specialist"]["scope_files"])
        self.assertEqual(rows["extra-hand"]["source"], "manual")
        self.assertTrue(rows["extra-hand"]["has_archive"])
        self.assertNotIn("drbug", rows)

        saved = _table(self.profile)
        self.assertNotIn("widget-specialist", saved["roster"]["specialists"])
        self.assertEqual(
            saved["roster"]["specialists"]["extra-hand"]["source"], "manual"
        )
        self.assertEqual(
            saved["roster"]["specialists"]["extra-hand"]["files"],
            ["pkg/extra.py"],
        )
        self.assertIn("extra-hand", saved["module_defaults"]["extra"])
        self.assertIn(
            "pkg/extra.py",
            saved["roster"]["specialists"]["generalist"]["files"],
        )
        self.assertIn("Extra helper.", saved["roster"]["specialists"]["extra-hand"]["blurb"])
        reloaded = roster_from_table(saved)
        self.assertEqual(
            reloaded.allowed_files("extra-hand"), ("pkg/extra.py",)
        )

    def test_add_rejects_missing_module_and_duplicate_name(self) -> None:
        with self.assertRaises(RosterError) as missing:
            add_worker(self.workspace, "nobody", "pkg/missing.py")
        self.assertIn("不存在", str(missing.exception))
        self.assertNotIn("nobody", _table(self.profile)["roster"]["specialists"])

        add_worker(self.workspace, "extra-hand", "pkg/extra.py")
        with self.assertRaises(RosterError) as duplicate:
            add_worker(self.workspace, "extra-hand", "pkg/extra.py")
        self.assertIn("已有员工", str(duplicate.exception))
        self.assertEqual(
            _table(self.profile)["module_defaults"]["extra"].count("extra-hand"),
            1,
        )

    def test_remove_archives_and_evidence_falls_through(self) -> None:
        ensure_archives(
            self.profile,
            {
                "kept-specialist": {
                    "files": ["pkg/kept.py"],
                    "specialist": True,
                    "module": "kept",
                    "source": "auto",
                }
            },
        )
        identity_path = self.profile / "agents" / "kept-specialist" / "IDENTITY.md"
        identity_path.write_text(
            identity_path.read_text(encoding="utf-8") + "手改标记\n",
            encoding="utf-8",
        )

        result = remove_worker(self.workspace, "kept-specialist")

        self.assertTrue(result["ok"])
        self.assertEqual(result["expert_count"], 0)
        self.assertEqual(result["warning"], ZERO_EXPERT_WARNING)
        self.assertEqual(result["released_module_defaults"], ["kept"])
        self.assertEqual(
            result["released_node_overrides"],
            ["tests/test_kept.py::test_one"],
        )
        self.assertTrue(
            result["archived_to"].startswith(
                f"agents/_archived/kept-specialist-{date.today().isoformat()}"
            )
        )
        self.assertFalse(identity_path.exists())
        archived = self.profile / result["archived_to"] / "IDENTITY.md"
        self.assertIn("手改标记", archived.read_text(encoding="utf-8"))
        names = [row["name"] for row in list_roster(self.workspace)]
        self.assertNotIn("kept-specialist", names)
        self.assertIn("generalist", names)

        table = _table(self.profile)
        plan = DispatchKB(table).lookup(
            evidence_from_failed(["tests/test_kept.py::test_one"])
        )
        self.assertEqual(list(plan.assignments), ["generalist"])
        self.assertEqual(plan.unmapped, ["tests/test_kept.py::test_one"])
        self.assertNotIn("kept", table["module_defaults"])
        self.assertNotIn(
            "tests/test_kept.py::test_one", table["node_overrides"]
        )

        with self.assertRaises(RosterError) as missing:
            remove_worker(self.workspace, "kept-specialist")
        self.assertIn("没有这个员工", str(missing.exception))
        self.assertIn("不会静默成功", str(missing.exception))

    def test_remove_missing_generalist_and_secretary_are_errors(self) -> None:
        with self.assertRaises(RosterError) as missing:
            remove_worker(self.workspace, "ghost")
        self.assertIn("没有这个员工", str(missing.exception))
        with self.assertRaises(RosterError) as secretary:
            remove_worker(self.workspace, "主治医生")
        self.assertIn("秘书", str(secretary.exception))
        with self.assertRaises(RosterError) as fallback:
            remove_worker(self.workspace, "generalist")
        self.assertIn("不能删除", str(fallback.exception))
        self.assertIn("generalist", _table(self.profile)["roster"]["specialists"])

    def test_dispatcher_remove_warns_when_no_experts_remain(self) -> None:
        self.addCleanup(dispatcher._bind_default_workspace, None)
        ensure_archives(
            self.profile,
            {
                "kept-specialist": {
                    "files": ["pkg/kept.py"],
                    "specialist": True,
                    "module": "kept",
                }
            },
        )
        text = dispatcher.remove_worker(
            "kept-specialist", workspace=str(self.workspace)
        )
        self.assertIn(ZERO_EXPERT_WARNING, text)
        self.assertIn("已归档", text)
        self.assertIn("未销毁", text)
        self.assertIn("assignments", text)
        self.assertFalse(
            (self.profile / "agents" / "kept-specialist").exists()
        )
        self.assertTrue((self.profile / "agents" / "_archived").is_dir())


class RosterFacadeTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team1-facade-")
        self.addCleanup(temporary.cleanup)
        self.workspace, self.profile = _plant(Path(temporary.name))
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.addCleanup(dispatcher.set_event_sink, None)

    def test_api_round_trip_matches_the_roster_functions(self) -> None:
        self.assertEqual(
            api.list_roster(str(self.workspace)),
            list_roster(str(self.workspace)),
        )
        added = api.add_worker(str(self.workspace), "extra-hand", "pkg/extra.py")
        self.assertEqual(added["source"], "boss")
        self.assertEqual(added["scope_files"], ["pkg/extra.py"])
        self.assertEqual(
            api.list_roster(str(self.workspace)),
            list_roster(str(self.workspace)),
        )
        removed = api.remove_worker(str(self.workspace), "extra-hand")
        self.assertTrue(removed["ok"])
        self.assertTrue(removed["archived_to"].startswith("agents/_archived/extra-hand-"))
        self.assertNotIn(
            "extra-hand",
            [row["name"] for row in api.list_roster(str(self.workspace))],
        )
        self.assertTrue((self.profile / removed["archived_to"]).is_dir())

    def test_cli_lists_adds_and_rejects(self) -> None:
        def run(argv: list[str]) -> tuple[int, str]:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli_main(argv)
            return code, buffer.getvalue()

        code, output = run(["team", str(self.workspace)])
        self.assertEqual(code, 0, output)
        self.assertIn("kept-specialist", output)
        self.assertIn("auto", output)
        self.assertIn("通用修理工", output)

        code, output = run(
            [
                "team",
                str(self.workspace),
                "add",
                "extra-hand",
                "--module",
                "pkg/extra.py",
            ]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("boss", output)
        self.assertIn("pkg/extra.py", output)

        code, output = run(
            [
                "team",
                str(self.workspace),
                "add",
                "nope",
                "--module",
                "pkg/missing.py",
            ]
        )
        self.assertEqual(code, 2, output)
        self.assertIn("不存在", output)

        code, output = run(
            ["team", str(self.workspace), "remove", "ghost"]
        )
        self.assertEqual(code, 2, output)
        self.assertIn("没有这个员工", output)

        code, output = run(
            ["team", str(self.workspace), "remove", "extra-hand"]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("已归档", output)
        self.assertTrue((self.profile / "agents" / "_archived").is_dir())

    def test_dispatcher_tools_persist_and_restate_rule_is_in_the_prompt(self) -> None:
        tools = {tool.name: tool for tool in dispatcher.build_tools()}
        self.assertTrue(tools["list_roster"].is_read_only)
        self.assertFalse(tools["add_worker"].is_read_only)
        self.assertFalse(tools["remove_worker"].is_read_only)
        self.assertIn("会落盘", tools["add_worker"].description)
        self.assertIn("会落盘", tools["remove_worker"].description)
        self.assertIn("复述", tools["add_worker"].description)
        self.assertIn("复述", tools["remove_worker"].description)

        agent = build_dispatcher(
            model=FakeChatModel(fallback_reply="x"),
            workspace=str(self.workspace),
        )
        prompt = agent._system_prompt
        self.assertIn("团队管理", prompt)
        self.assertIn("复述", prompt)
        self.assertIn("活怎么派永远按事先定好的规则表来", prompt)
        self.assertIn("并保存", prompt)
        self.assertIn("不能临时", prompt)

        refused = dispatcher.add_worker(
            "nobody", "pkg/missing.py", workspace=str(self.workspace)
        )
        self.assertIn("拒绝", refused)
        self.assertIn("不存在", refused)

        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "list_roster",
                    "input": {"workspace": str(self.workspace)},
                },
                {"type": "text", "text": "名册已转达"},
            ]
        )
        agent = build_dispatcher(model=model, workspace=str(self.workspace))
        reply = asyncio.run(
            agent.reply(
                Msg(
                    name="用户",
                    role="user",
                    content=[TextBlock(text="看看名册")],
                )
            )
        )
        self.assertEqual(reply.get_text_content(), "名册已转达")
        results = [
            text
            for messages in model.seen_messages
            for text in _tool_result_texts(messages)
        ]
        self.assertTrue(any("kept-specialist" in text for text in results), results)
        self.assertTrue(any("[auto]" in text or "| auto |" in text for text in results), results)

    def test_run_repair_report_names_the_team_without_changing_dispatch(self) -> None:
        (self.profile / "vault").mkdir()
        record = RunRecord(
            arm="C",
            kb_enabled=True,
            outcome="PASS",
            rounds=[
                {
                    "round": 1,
                    "status": "FAIL",
                    "failed_tests": ["tests/test_kept.py::test_one"],
                    "assignments": {
                        "generalist": ["tests/test_kept.py::test_one"]
                    },
                    "fix_results": [],
                }
            ],
        )
        with mock.patch.object(dispatcher, "run_arm", return_value=record) as run_mock:
            text = dispatcher.run_repair(
                workspace=str(self.workspace),
                actors="scripted",
                max_rounds=1,
            )
        self.assertIn("本次队伍构成: 专家 1 人（kept-specialist[auto]）", text)
        self.assertIn("generalist[auto]", text)
        self.assertIn("第 1 轮派单: generalist×1", text)
        self.assertNotIn("vault_dir", run_mock.call_args.kwargs)
        self.assertNotIn("kb_path", run_mock.call_args.kwargs)
        self.assertIsNotNone(run_mock.call_args.kwargs["profile_dir"])
        self.assertEqual(run_mock.call_args.kwargs["profile_dir"], self.profile)


class FallbackRunTest(unittest.TestCase):
    """A real scripted run: removed billing evidence lands on generalist."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team1-run-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = self.tmp / "shop"
        shutil.copytree(
            DEMO_REPO,
            self.workspace,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "*.pyc", ".drbug"
            ),
        )
        self.profile = (
            state_root_for(self.workspace)
            / "profiles"
            / default_profile_name(self.workspace)
        )
        self.profile.mkdir(parents=True)
        table = json.loads(
            (PLATFORM_ROOT / "kb" / "dispatch_table.json").read_text(
                encoding="utf-8"
            )
        )
        profile_table = json.loads(
            (
                PLATFORM_ROOT / "profiles" / "demo_repo" / "dispatch_table.json"
            ).read_text(encoding="utf-8")
        )
        table["roster"] = profile_table["roster"]
        _write(
            self.profile / "dispatch_table.json",
            json.dumps(table, ensure_ascii=False, indent=2) + "\n",
        )
        shutil.copytree(
            PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault", self.profile / "vault"
        )
        _write(
            self.profile / "profile.json",
            json.dumps(
                {
                    "name": default_profile_name(self.workspace),
                    "origin": "tests",
                    "specialists": [
                        item["id"] for item in table["specialists"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)

    def test_removed_billing_expert_falls_back_in_the_run_record(self) -> None:
        removed = remove_worker(self.workspace, "billing-specialist")
        self.assertIn("billing", removed["released_module_defaults"])
        self.assertNotEqual(removed["warning"], ZERO_EXPERT_WARNING)

        out = self.tmp / "record.json"
        text = dispatcher.run_repair(
            workspace=str(self.workspace),
            actors="scripted",
            max_rounds=1,
            out=str(out),
        )

        self.assertIn("本次队伍构成:", text)
        self.assertIn("[auto]", text)
        self.assertNotIn("billing-specialist[auto]", text)
        self.assertIn("派单:", text)
        self.assertIn("generalist", text)
        self.assertTrue(out.is_file(), text)
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(set(record), _RECORD_KEYS)
        self.assertTrue(record["rounds"], text)
        assignments = record["rounds"][0]["assignments"]
        self.assertNotIn("billing-specialist", assignments)
        self.assertIn(_BILLING_NODE, assignments.get("generalist", []))
        for actor_id in assignments:
            self.assertIn(actor_id, CAN_FIX)
        self.assertIn(f"generalist×{len(assignments['generalist'])}", text)


if __name__ == "__main__":
    unittest.main()


class RosterOverlayTest(unittest.TestCase):
    """TEAM1-fix1 c1: 覆盖层（增删两向）+ 来源渠道与标签。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team1-overlay-")
        self.addCleanup(temporary.cleanup)
        self.workspace, self.profile = _plant(Path(temporary.name))

    def _overlay(self) -> dict:
        path = self.profile / "roster_overlay.json"
        if not path.is_file():
            return {"added": [], "removed": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_apply_overlay_without_file_leaves_table_untouched(self) -> None:
        from core.roster import apply_overlay

        table = _table(self.profile)
        before = copy.deepcopy(table)
        self.assertEqual(apply_overlay(self.profile, table), [])
        self.assertEqual(table, before)

    def test_apply_overlay_removed_strips_worker(self) -> None:
        from core.roster import apply_overlay, save_overlay

        save_overlay(
            self.profile,
            {
                "added": [],
                "removed": [
                    {
                        "id": "kept-specialist",
                        "module": "kept",
                        "time": "2026-11-15T00:00:00",
                    }
                ],
            },
        )
        table = _table(self.profile)
        notes = apply_overlay(self.profile, table)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "roster_overlay")
        self.assertNotIn("kept-specialist", table["roster"]["specialists"])
        self.assertNotIn("kept", table["module_defaults"])
        listed = [item["id"] for item in table["specialists"]]
        self.assertNotIn("kept-specialist", listed)
        self.assertEqual(listed[-1], "generalist")

    def test_apply_overlay_added_reinserts_worker(self) -> None:
        from core.roster import apply_overlay, save_overlay

        save_overlay(
            self.profile,
            {
                "added": [
                    {
                        "id": "widget-pro",
                        "module": "widget",
                        "path": "pkg/widget.py",
                        "blurb": "Widget module.",
                        "source": "boss",
                        "time": "2026-11-15T00:00:00",
                    }
                ],
                "removed": [],
            },
        )
        table = _table(self.profile)
        notes = apply_overlay(self.profile, table)
        self.assertEqual(len(notes), 1)
        entry = table["roster"]["specialists"]["widget-pro"]
        self.assertEqual(entry["source"], "boss")
        self.assertEqual(entry["files"], ["pkg/widget.py"])
        self.assertEqual(table["module_defaults"]["widget"], ["widget-pro"])
        listed = [item["id"] for item in table["specialists"]]
        self.assertIn("widget-pro", listed)
        self.assertEqual(listed[-1], "generalist")

    def test_apply_overlay_conflict_removed_wins(self) -> None:
        from core.roster import apply_overlay, save_overlay

        save_overlay(
            self.profile,
            {
                "added": [
                    {
                        "id": "kept-specialist",
                        "module": "kept",
                        "path": "pkg/kept.py",
                        "blurb": "Kept module.",
                        "source": "boss",
                        "time": "2026-11-15T00:00:00",
                    }
                ],
                "removed": [
                    {
                        "id": "kept-specialist",
                        "module": "kept",
                        "time": "2026-11-15T01:00:00",
                    }
                ],
            },
        )
        table = _table(self.profile)
        notes = apply_overlay(self.profile, table)
        self.assertEqual(len(notes), 2)
        self.assertNotIn("kept-specialist", table["roster"]["specialists"])

    def test_add_worker_records_overlay_with_channel(self) -> None:
        result = add_worker(
            self.workspace, "widget-pro", "pkg/widget.py", channel="boss"
        )
        self.assertEqual(result["source"], "boss")
        overlay = self._overlay()
        self.assertEqual(len(overlay["added"]), 1)
        entry = overlay["added"][0]
        self.assertEqual(entry["id"], "widget-pro")
        self.assertEqual(entry["source"], "boss")
        self.assertEqual(entry["path"], "pkg/widget.py")
        self.assertEqual(entry["module"], "widget")
        rows = {row["name"]: row for row in list_roster(self.workspace)}
        self.assertEqual(rows["widget-pro"]["source"], "boss")
        self.assertEqual(rows["widget-pro"]["source_label"], "老板添加")

    def test_add_worker_secretary_channel_label(self) -> None:
        add_worker(
            self.workspace, "extra-pro", "pkg/extra.py", channel="secretary"
        )
        rows = {row["name"]: row for row in list_roster(self.workspace)}
        self.assertEqual(rows["extra-pro"]["source"], "secretary")
        self.assertEqual(rows["extra-pro"]["source_label"], "秘书提议")

    def test_add_worker_default_channel_stays_manual(self) -> None:
        result = add_worker(self.workspace, "widget-pro", "pkg/widget.py")
        self.assertEqual(result["source"], "manual")
        rows = {row["name"]: row for row in list_roster(self.workspace)}
        self.assertEqual(rows["widget-pro"]["source_label"], "手动添加")
        self.assertEqual(rows["kept-specialist"]["source_label"], "自动建档")

    def test_add_worker_rejects_unknown_channel(self) -> None:
        with self.assertRaises(RosterError):
            add_worker(
                self.workspace,
                "widget-pro",
                "pkg/widget.py",
                channel="random",
            )

    def test_remove_worker_records_overlay_and_clears_added(self) -> None:
        add_worker(
            self.workspace, "widget-pro", "pkg/widget.py", channel="boss"
        )
        remove_worker(self.workspace, "widget-pro")
        overlay = self._overlay()
        self.assertEqual(overlay["added"], [])
        self.assertEqual(len(overlay["removed"]), 1)
        self.assertEqual(overlay["removed"][0]["id"], "widget-pro")
        self.assertEqual(overlay["removed"][0]["module"], "widget")

    def test_add_back_removed_worker_clears_tombstone(self) -> None:
        remove_worker(self.workspace, "kept-specialist")
        self.assertEqual(len(self._overlay()["removed"]), 1)
        result = add_worker(
            self.workspace, "kept-specialist", "pkg/kept.py", channel="boss"
        )
        self.assertTrue(result["ok"])
        overlay = self._overlay()
        self.assertEqual(overlay["removed"], [])
        self.assertEqual(len(overlay["added"]), 1)
        names = {row["name"] for row in list_roster(self.workspace)}
        self.assertIn("kept-specialist", names)

    def test_add_back_archive_only_removed_worker(self) -> None:
        from core.roster import save_overlay

        _write(
            self.profile / "agents" / "widget-pro" / "IDENTITY.md",
            "# widget-pro\n",
        )
        save_overlay(
            self.profile,
            {
                "added": [],
                "removed": [
                    {
                        "id": "widget-pro",
                        "module": "widget",
                        "time": "2026-11-15T00:00:00",
                    }
                ],
            },
        )
        result = add_worker(
            self.workspace, "widget-pro", "pkg/widget.py", channel="boss"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self._overlay()["removed"], [])

    def test_list_roster_filters_overlay_removed(self) -> None:
        from core.roster import save_overlay

        save_overlay(
            self.profile,
            {
                "added": [],
                "removed": [
                    {
                        "id": "kept-specialist",
                        "module": "kept",
                        "time": "2026-11-15T00:00:00",
                    }
                ],
            },
        )
        names = {row["name"] for row in list_roster(self.workspace)}
        self.assertNotIn("kept-specialist", names)
        self.assertIn("generalist", names)


class RosterOverlayRebuildTest(unittest.TestCase):
    """TEAM1-fix1 c2: 建档挂钩——重建档案时覆盖层照样生效。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team1-rebuild-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        repo = self.tmp / "tiny"
        (repo / "tiny").mkdir(parents=True)
        (repo / "tiny" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "tiny" / "calc.py").write_text(
            "def add(left, right):\n    return left - right\n",
            encoding="utf-8",
        )
        (repo / "tiny" / "util.py").write_text(
            "def shout(text):\n    return text.upper()\n", encoding="utf-8"
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from tiny.calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 1) == 2\n",
            encoding="utf-8",
        )
        self.repo = repo
        self.profiles_root = self.tmp / "profiles"

    def _build(self):
        return build_profile(
            self.repo, out_root=self.profiles_root, name="tiny"
        )

    def test_removed_worker_stays_removed_after_rebuild(self) -> None:
        first = self._build()
        names = [item["id"] for item in first.dispatch_table["specialists"]]
        self.assertIn("calc-specialist", names)
        removed = remove_worker(
            self.repo, "calc-specialist", profiles_root=self.profiles_root
        )
        self.assertTrue(removed["ok"])
        rebuilt = self._build()
        names = [item["id"] for item in rebuilt.dispatch_table["specialists"]]
        self.assertNotIn("calc-specialist", names)
        self.assertIn("generalist", names)
        self.assertTrue(
            any(
                item.get("kind") == "roster_overlay"
                for item in rebuilt.ambiguities
            )
        )
        rows = list_roster(self.repo, profiles_root=self.profiles_root)
        self.assertNotIn("calc-specialist", [row["name"] for row in rows])

    def test_added_worker_survives_rebuild(self) -> None:
        self._build()
        added = add_worker(
            self.repo,
            "util-pro",
            "tiny/util.py",
            profiles_root=self.profiles_root,
            channel="boss",
        )
        self.assertEqual(added["source"], "boss")
        rebuilt = self._build()
        names = [item["id"] for item in rebuilt.dispatch_table["specialists"]]
        self.assertIn("util-pro", names)
        self.assertIsNotNone(read_archive(rebuilt.profile_dir, "util-pro"))
        rows = {
            row["name"]: row
            for row in list_roster(self.repo, profiles_root=self.profiles_root)
        }
        self.assertEqual(rows["util-pro"]["source_label"], "老板添加")

    def test_rebuild_without_overlay_is_byte_identical(self) -> None:
        first = self._build()
        snapshot = {
            name: (first.profile_dir / name).read_bytes()
            for name in ("dispatch_table.json", "profile.json", "PROFILE.md")
        }
        second = self._build()
        for name, data in snapshot.items():
            self.assertEqual(
                (second.profile_dir / name).read_bytes(), data, name
            )


class SynthesisOverlayGateTest(unittest.TestCase):
    """TEAM1-fix1 c2: 覆盖层删掉的模块，运行时不许再合成专家。"""

    def _roster(self):
        return roster_from_table(
            {
                "roster": {
                    "package": "tiny",
                    "specialists": {
                        "generalist": {
                            "files": ["tiny/calc.py", "tiny/util.py"],
                            "blurb": "兜底员工。",
                            "specialist": False,
                        }
                    },
                }
            }
        )

    def _inventory(self):
        return {"calc": {"path": "tiny/calc.py", "blurb": "算账。"}}

    def test_synthesis_proceeds_without_block(self) -> None:
        actor_id, _profile, marker = _resolve_actor(
            "generalist",
            ["tests/test_calc.py::test_add"],
            self._roster(),
            self._inventory(),
        )
        self.assertEqual(actor_id, "calc-specialist")
        self.assertTrue((marker or "").startswith("[synthesized]"))

    def test_blocked_module_keeps_generalist(self) -> None:
        actor_id, _profile, marker = _resolve_actor(
            "generalist",
            ["tests/test_calc.py::test_add"],
            self._roster(),
            self._inventory(),
            blocked_modules=frozenset({"calc"}),
        )
        self.assertEqual(actor_id, "generalist")
        self.assertTrue((marker or "").startswith("[synthesis-blocked]"))

    def test_blocked_module_unknown_actor_falls_back(self) -> None:
        actor_id, _profile, marker = _resolve_actor(
            "calc-specialist",
            ["tests/test_calc.py::test_add"],
            self._roster(),
            self._inventory(),
            blocked_modules=frozenset({"calc"}),
        )
        self.assertEqual(actor_id, "generalist")
        self.assertTrue((marker or "").startswith("[synthesis-blocked]"))

    def test_overlay_blocked_modules_reads_removed_entries(self) -> None:
        from core.roster import save_overlay

        temporary = tempfile.TemporaryDirectory(prefix="team1-gate-")
        self.addCleanup(temporary.cleanup)
        profile = Path(temporary.name) / "profile"
        profile.mkdir(parents=True)
        self.assertEqual(_overlay_blocked_modules(profile), frozenset())
        self.assertEqual(_overlay_blocked_modules(None), frozenset())
        save_overlay(
            profile,
            {
                "added": [],
                "removed": [
                    {
                        "id": "calc-specialist",
                        "module": "calc",
                        "time": "2026-11-15T23:00:00",
                    }
                ],
            },
        )
        self.assertEqual(
            _overlay_blocked_modules(profile), frozenset({"calc"})
        )
