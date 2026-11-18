# -*- coding: utf-8 -*-
"""API1：双 UI 门面 ``core/api.py`` 的离线测试。

全程不实例化真实 provider 模型、不读真实密钥、不联网：

* 体检只跑项目自带 ``tools/env_check.py``；
* 修复运行一律 ``actors="scripted"``；
* ChatSession 用仓库现成的 ``FakeChatModel`` 及其两个测试子类
  （循环敏感替身参照 ``tests/test_tui.py`` 的 TUI1-fix2 回归写法）；
* 涉及写入的用例全部把 ``api.RUNS_DIR`` / ``api.PROFILES_ROOT`` 指到
  临时目录，或在 demo_repo 的临时副本上操作。

安全断言：``get_model_config`` / ``get_status`` 只报密钥设置状态。真实
``platform/.env.local`` 永不读取；测试自建一份临时 ``.env.local``，并断言
其中任何内容都不会出现在门面输出里。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, FinishedReason

from agentscope_ext.fake_model import FakeChatModel
from core import api, dispatcher
from core.actors import apply_fix
from core.model_config import load_local_env

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
_RESPONSE_TIME = "1970-01-01T00:00:00.000000"

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
                id=f"api1-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"api1-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


class ScriptedToolModel(FakeChatModel):
    """按脚本先发一次 AgentScope 工具调用，再发最终文本。"""

    def __init__(self, steps: list[dict]) -> None:
        super().__init__(fallback_reply="script exhausted")
        self.steps = list(steps)

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        index = len(self.calls)
        self.calls.append({"index": index})
        if not self.steps:
            return _text_response(index, "done")
        step = self.steps.pop(0)
        if step["type"] == "tool":
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"api1-tool-{index:04d}",
                        name=step["name"],
                        input=json.dumps(step["input"], ensure_ascii=False),
                    )
                ],
                is_last=True,
                id=f"api1-response-{index:04d}",
                created_at=_RESPONSE_TIME,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
                finished_reason=FinishedReason.COMPLETED,
            )
        return _text_response(index, step["text"])


class LoopBoundModel(FakeChatModel):
    """仿真实 httpx 异步客户端：模型绑定首次调用时的事件循环。

    真实 ``DeepSeekChatModel`` 的 HTTP 客户端在首次请求时绑定当前循环；
    循环关闭后复用会抛 ``RuntimeError('Event loop is closed')``。本替身
    每次 ``_call_api`` 都复刻这一约束，用来抓「逐条消息新建事件循环」。
    """

    def __init__(self, fallback_reply: str) -> None:
        super().__init__(fallback_reply=fallback_reply)
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.call_loops: list[asyncio.AbstractEventLoop] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        loop = asyncio.get_running_loop()
        if self.bound_loop is None:
            self.bound_loop = loop
        elif self.bound_loop.is_closed() or loop is not self.bound_loop:
            raise RuntimeError("Event loop is closed")
        self.call_loops.append(loop)
        return await super()._call_api(
            model_name,
            messages,
            tools,
            tool_choice,
            **kwargs,
        )


class TempDirTest(unittest.TestCase):
    """每个用例一个临时目录。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="api1-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def patch_runs_dir(self) -> Path:
        runs_dir = self.tmp / "runs"
        patcher = mock.patch.object(api, "RUNS_DIR", runs_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runs_dir

    def patch_profiles_root(self) -> Path:
        profiles_root = self.tmp / "profiles"
        patcher = mock.patch.object(api, "PROFILES_ROOT", profiles_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        return profiles_root

    def copy_demo(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(DEMO_REPO, target, ignore=_COPY_IGNORE)
        return target


def _assert_event_matches_schema(testcase: unittest.TestCase, event: dict) -> None:
    """事件必须落在事件模式表内；必需字段一个不能少。"""
    name = event.get("event")
    testcase.assertIn(name, api.EVENT_FIELD_SCHEMA)
    fields = set(api.EVENT_FIELD_SCHEMA[name])
    optional = set(api.EVENT_OPTIONAL_FIELDS.get(name, ()))
    testcase.assertLessEqual(set(event), fields)
    testcase.assertLessEqual(fields - optional, set(event))


# ---------------------------------------------------------------------------
# 事件模式表（两通道）
# ---------------------------------------------------------------------------
class EventSchemaTest(unittest.TestCase):
    def test_two_channels_and_names_are_locked(self) -> None:
        self.assertEqual(
            api.RUNNER_EVENT_NAMES,
            (
                "run_started",
                "verdict",
                "dispatch",
                "actor_done",
                "run_finished",
            ),
        )
        self.assertEqual(api.DISPATCHER_EVENT_NAMES, ("tool_call",))
        self.assertEqual(
            set(api.EVENT_FIELD_SCHEMA),
            set(api.RUNNER_EVENT_NAMES) | set(api.DISPATCHER_EVENT_NAMES),
        )
        self.assertEqual(set(api.EVENT_CHANNELS), set(api.EVENT_FIELD_SCHEMA))

    def test_channel_partition_and_field_shape(self) -> None:
        for name in api.RUNNER_EVENT_NAMES:
            self.assertEqual(api.EVENT_CHANNELS[name], api.CHANNEL_RUNNER)
        for name in api.DISPATCHER_EVENT_NAMES:
            self.assertEqual(api.EVENT_CHANNELS[name], api.CHANNEL_DISPATCHER)
        for name, fields in api.EVENT_FIELD_SCHEMA.items():
            self.assertEqual(fields[0], "event", name)
            self.assertEqual(len(fields), len(set(fields)), name)
        for name, optional in api.EVENT_OPTIONAL_FIELDS.items():
            self.assertLessEqual(set(optional), set(api.EVENT_FIELD_SCHEMA[name]))

    def test_key_fields_of_each_event(self) -> None:
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["run_started"],
            ("event", "arm", "actors", "kb_enabled", "workspace"),
        )
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["verdict"],
            ("event", "round", "status", "failed_count"),
        )
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["dispatch"],
            ("event", "round", "assignments"),
        )
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["actor_done"],
            ("event", "round", "actor_id", "applied", "note"),
        )
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["run_finished"],
            ("event", "outcome", "rounds", "final_failed"),
        )
        self.assertEqual(
            api.EVENT_FIELD_SCHEMA["tool_call"], ("event", "tool", "summary")
        )
        self.assertEqual(api.EVENT_OPTIONAL_FIELDS, {"tool_call": ("summary",)})


# ---------------------------------------------------------------------------
# 模型配置（绝不泄露密钥）
# ---------------------------------------------------------------------------
class ModelConfigTest(TempDirTest):
    def test_three_legacy_keys_plus_profiles_and_never_the_secret(self) -> None:
        sentinel = "sk-api1-secret-should-never-leak"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": sentinel}):
            config = api.get_model_config()
        self.assertEqual(
            set(config),
            {"provider", "model", "api_key_set", "profiles", "roles"},
        )
        self.assertTrue(config["api_key_set"])
        self.assertIsInstance(config["api_key_set"], bool)
        self.assertNotIn(sentinel, json.dumps(config, ensure_ascii=False))
        self.assertEqual(
            config["profiles"],
            [
                {
                    "name": "default",
                    "provider": config["provider"],
                    "model": config["model"],
                    "api_key_set": True,
                    "active": True,
                    "auto_generated": False,
                }
            ],
        )
        self.assertEqual(
            config["roles"],
            {
                "mapped": False,
                "secretary": "default",
                "worker": "default",
            },
        )

    def test_profiles_summary_marks_active_and_key_state_without_secret(
        self,
    ) -> None:
        from core import model_config

        sentinel = "sk-api1-profiles-secret"
        config_path = self.tmp / "model_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "work",
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_key_env": "API1_WORK_KEY",
                        },
                        {
                            "name": "claude",
                            "provider": "anthropic",
                            "model": "claude-test",
                            "api_key_env": "API1_CLAUDE_KEY",
                        },
                    ],
                    "active_profile": "claude",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            model_config, "DEFAULT_CONFIG_PATH", config_path
        ), mock.patch.object(
            model_config, "user_config_path", lambda: self.tmp / "user.json"
        ), mock.patch.dict(
            os.environ,
            {
                "API1_WORK_KEY": sentinel,
                "API1_CLAUDE_KEY": "",
                "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
            },
            clear=False,
        ):
            config = api.get_model_config()

        self.assertEqual(config["provider"], "anthropic")
        self.assertEqual(config["model"], "claude-test")
        self.assertFalse(config["api_key_set"])
        self.assertEqual(
            [
                (item["name"], item["active"], item["api_key_set"])
                for item in config["profiles"]
            ],
            [("work", False, True), ("claude", True, False)],
        )
        serialized = json.dumps(config, ensure_ascii=False)
        self.assertNotIn(sentinel, serialized)

    def test_env_local_contents_never_reach_the_output(self) -> None:
        # 不读取真实的 platform/.env.local：测试自建一份，内容等价地敏感。
        sentinel = "sk-api1-env-file-secret"
        env_file = self.tmp / ".env.local"
        env_file.write_text(
            f"API1_FAKE_KEY={sentinel}\n", encoding="utf-8"
        )
        with mock.patch.dict(
            os.environ,
            {
                "PLATFORM_MODEL_API_KEY_ENV": "API1_FAKE_KEY",
                "PLATFORM_MODEL": "api1-fake-model",
            },
        ):
            load_local_env(env_file)
            config = api.get_model_config()

        self.assertEqual(config["model"], "api1-fake-model")
        self.assertTrue(config["api_key_set"])
        serialized = json.dumps(config, ensure_ascii=False)
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn(env_file.read_text(encoding="utf-8").strip(), serialized)


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------
class StatusTest(unittest.TestCase):
    def test_status_has_environment_vault_and_model_without_secret(self) -> None:
        sentinel = "sk-api1-status-secret"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": sentinel}):
            status = api.get_status()

        self.assertEqual(
            set(status), {"ok", "environment", "acceptor", "model"}
        )
        self.assertTrue(status["ok"])
        self.assertTrue(status["environment"]["ok"])
        self.assertEqual(status["environment"]["exit_code"], 0)
        self.assertIn("ENV_CHECK_PASS", status["environment"]["lines"])
        self.assertEqual(
            set(status["environment"]),
            {"ok", "exit_code", "python", "agentscope", "lines"},
        )
        self.assertTrue(status["acceptor"]["ok"])
        self.assertIn("pytest", status["acceptor"]["parsers"])
        self.assertEqual(set(status["acceptor"]), {"ok", "parsers"})
        self.assertEqual(
            set(status["model"]),
            {"configured", "provider", "model", "api_key_env", "api_key_set"},
        )
        self.assertTrue(status["model"]["configured"])
        self.assertTrue(status["model"]["api_key_set"])
        self.assertNotIn(sentinel, json.dumps(status, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 修复运行与付费闸
# ---------------------------------------------------------------------------
class RunRepairTest(TempDirTest):
    def test_paid_gate_refuses_before_touching_anything(self) -> None:
        runs_dir = self.patch_runs_dir()
        events: list[dict] = []
        result = api.run_repair(
            arm="Z",
            actors="agentscope",
            workspace=str(self.tmp / "does-not-exist"),
            confirm_paid=0,
            on_event=events.append,
        )

        self.assertEqual(result["outcome"], "REFUSED")
        self.assertIn("API 额度", result["reason"])
        self.assertEqual(result["rounds"], [])
        self.assertEqual(result["final_failed"], [])
        self.assertIsNone(result["artifacts"]["record_path"])
        self.assertIsNone(result["artifacts"]["bundle_path"])
        self.assertEqual(events, [])
        self.assertFalse(runs_dir.exists())

    def test_paid_gate_accepts_only_literal_true(self) -> None:
        runs_dir = self.patch_runs_dir()
        result = api.run_repair(
            actors="agentscope",
            workspace=str(DEMO_REPO),
            confirm_paid=1,
        )

        self.assertEqual(result["outcome"], "REFUSED")
        self.assertFalse(runs_dir.exists())

    def test_scripted_run_returns_record_events_and_bundle(self) -> None:
        runs_dir = self.patch_runs_dir()
        events: list[dict] = []
        result = api.run_repair(
            workspace=str(DEMO_REPO), max_rounds=3, on_event=events.append
        )

        self.assertTrue(_RECORD_KEYS <= set(result))
        self.assertEqual(result["arm"], "C")
        self.assertTrue(result["kb_enabled"])
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["final_failed"], [])
        self.assertEqual(len(result["rounds"]), 1)
        self.assertEqual(result["rounds"][0]["status"], "FAIL")

        record_path = Path(result["artifacts"]["record_path"])
        self.assertTrue(record_path.is_file())
        self.assertEqual(record_path.parent, runs_dir)
        saved = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved,
            {key: result[key] for key in _RECORD_KEYS},
        )

        bundle_path = Path(result["artifacts"]["bundle_path"])
        self.assertEqual(result["artifacts"]["bundle_error"], "")
        self.assertEqual(
            sorted(item.name for item in bundle_path.iterdir()),
            ["adoption.json", "patch.diff", "record.json", "report.md"],
        )
        bundle_record = json.loads(
            (bundle_path / "record.json").read_text(encoding="utf-8")
        )
        self.assertEqual(bundle_record["outcome"], "PASS")

        names = [event["event"] for event in events]
        self.assertEqual(names[0], "run_started")
        self.assertEqual(names[-1], "run_finished")
        verdicts = [event for event in events if event["event"] == "verdict"]
        self.assertEqual(verdicts[0]["status"], "FAIL")
        self.assertEqual(verdicts[0]["round"], 0)
        self.assertEqual(verdicts[-1]["status"], "PASS")
        self.assertIn("dispatch", names)
        self.assertIn("actor_done", names)
        for event in events:
            _assert_event_matches_schema(self, event)
        self.assertEqual(events[-1]["outcome"], "PASS")
        self.assertEqual(events[-1]["rounds"], 1)

        summaries = api.list_runs()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(Path(summaries[0]["path"]), record_path)

    def test_invalid_arguments_raise(self) -> None:
        with self.assertRaises(ValueError):
            api.run_repair(arm="Z")
        with self.assertRaises(ValueError):
            api.run_repair(actors="robot", confirm_paid=True)
        with self.assertRaises(ValueError):
            api.run_repair(max_rounds=-1)
        with self.assertRaises(ValueError):
            api.run_repair(max_rounds="many")
        with self.assertRaises(FileNotFoundError):
            api.run_repair(workspace=str(self.tmp / "missing"))


# ---------------------------------------------------------------------------
# 冻结验收
# ---------------------------------------------------------------------------
class AcceptCheckTest(TempDirTest):
    def test_verdict_dict_shape_on_failing_workspace(self) -> None:
        verdict = api.accept_check(str(DEMO_REPO))

        self.assertEqual(
            set(verdict),
            {
                "status",
                "failed_tests",
                "tampered_files",
                "passed_count",
                "failed_count",
                "evidence",
                "detail",
            },
        )
        self.assertEqual(verdict["status"], "FAIL")
        self.assertGreater(len(verdict["failed_tests"]), 0)
        self.assertEqual(verdict["tampered_files"], [])
        self.assertGreater(verdict["failed_count"], 0)
        for item in verdict["evidence"]:
            self.assertEqual(set(item), {"node_id", "module"})

    def _give_profile(self, target: Path) -> None:
        source = Path(__file__).resolve().parents[1] / "profiles" / "demo_repo"
        dest = target / ".drbug" / "profiles" / target.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, ignore=_COPY_IGNORE)

    def test_pass_branch_after_scripted_fixes(self) -> None:
        fixed = self.copy_demo("fixed")
        self._give_profile(fixed)
        self.assertTrue(apply_fix("billing-specialist", fixed, "billing"))
        self.assertTrue(apply_fix("inventory-specialist", fixed, "inventory"))
        self.assertTrue(apply_fix("auth-specialist", fixed, "auth"))

        verdict = api.accept_check(str(fixed))

        self.assertEqual(verdict["status"], "PASS")
        self.assertEqual(verdict["failed_count"], 0)
        self.assertEqual(verdict["failed_tests"], [])
        self.assertGreater(verdict["passed_count"], 0)

    def test_tampered_branch_is_reported(self) -> None:
        tampered = self.copy_demo("tampered")
        self._give_profile(tampered)
        target = tampered / "tests" / "test_billing.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )

        verdict = api.accept_check(str(tampered))

        self.assertEqual(verdict["status"], "TAMPERED")
        self.assertIn("tests/test_billing.py", verdict["tampered_files"])
        self.assertEqual(verdict["failed_tests"], [])
        self.assertEqual(verdict["evidence"], [])

    def test_missing_workspace_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            api.accept_check(str(self.tmp / "missing"))


# ---------------------------------------------------------------------------
# 派单解释
# ---------------------------------------------------------------------------
class ExplainDispatchTest(TempDirTest):
    def _workspace_with_fixture_table(self) -> str:
        from core.autoprofile import default_profile_name

        workspace = self.tmp / "ws"
        workspace.mkdir()
        profile_dir = (
            workspace / ".drbug" / "profiles" / default_profile_name(workspace)
        )
        profile_dir.mkdir(parents=True)
        fixture = (
            Path(__file__).resolve().parents[1]
            / "kb"
            / "dispatch_table.json"
        )
        shutil.copyfile(fixture, profile_dir / "dispatch_table.json")
        return str(workspace)

    def test_dispatch_plan_dict_with_override_and_fallback(self) -> None:
        known = "tests/test_orders.py::test_checkout_total_with_coupon"
        unknown = "tests/test_mystery.py::test_unknown_node"

        plan = api.explain_dispatch(
            f"{known}, {unknown}", self._workspace_with_fixture_table()
        )

        self.assertEqual(set(plan), {"assignments", "unmapped"})
        self.assertEqual(plan["assignments"]["billing-specialist"], [known])
        self.assertEqual(plan["assignments"]["generalist"], [unknown])
        self.assertEqual(plan["unmapped"], [unknown])

    def test_accepts_iterable_and_empty_input(self) -> None:
        workspace = self._workspace_with_fixture_table()
        node = "tests/test_orders.py::test_checkout_total_with_coupon"
        from_list = api.explain_dispatch([node, node], workspace)

        self.assertEqual(from_list["assignments"]["billing-specialist"], [node])
        self.assertEqual(
            api.explain_dispatch("", workspace),
            {"assignments": {}, "unmapped": []},
        )


# ---------------------------------------------------------------------------
# 自动建档
# ---------------------------------------------------------------------------
class BuildProfileTest(TempDirTest):
    def test_profile_result_is_returned_as_dict(self) -> None:
        repo = self.copy_demo("demo_repo")

        result = api.build_profile(str(repo))

        self.assertEqual(result["name"], "demo_repo")
        profile_dir = Path(result["profile_dir"])
        self.assertEqual(
            profile_dir,
            (repo / ".drbug" / "profiles" / "demo_repo").resolve(),
        )
        self.assertTrue((profile_dir / "vault" / "manifest.json").is_file())
        self.assertTrue((profile_dir / "dispatch_table.json").is_file())
        self.assertTrue(Path(result["profile_json_path"]).is_file())
        self.assertGreater(result["test_total"], 0)
        self.assertGreater(result["failed_count"], 0)
        self.assertIn("packages", result)
        self.assertNotIn("dispatch_table", result)


# ---------------------------------------------------------------------------
# 复现测试分诊 / 冻结硬闸
# ---------------------------------------------------------------------------
class ReproFacadeTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.patch_profiles_root()

    def test_propose_without_symptom_never_builds_a_model(self) -> None:
        repo = self.copy_demo("demo_repo")
        with mock.patch(
            "core.model_config.build_model",
            side_effect=AssertionError("model must not be called"),
        ):
            result = api.propose_repro(str(repo))

        self.assertEqual(result["status"], "CANNOT_REPRODUCE")
        self.assertIsNone(result["test_path"])
        self.assertEqual(result["attempts"], [])
        self.assertIsNone(result["comparison"])
        self.assertIn("症状", result["reason"])
        self.assertFalse(
            (DEMO_REPO / "tests" / "test_repro_demo_repo.py").exists()
        )
        staging = repo / ".drbug" / "profiles" / "demo_repo" / "repro"
        self.assertTrue((staging / "candidate.json").is_file())

    def test_freeze_gate_accepts_only_literal_true_and_writes_nothing(self) -> None:
        repo = self.tmp / "repo"
        repo.mkdir()

        refused = api.freeze_repro(str(repo), confirm=False)
        truthy = api.freeze_repro(str(repo), confirm="yes")

        self.assertEqual(refused["status"], "REFUSED")
        self.assertIn("确认", refused["reason"])
        self.assertEqual(truthy["status"], "REFUSED")
        self.assertFalse((repo / "tests").exists())
        self.assertFalse((self.tmp / "profiles").exists())

        no_candidate = api.freeze_repro(str(repo), confirm=True)

        self.assertEqual(no_candidate["status"], "NO_CANDIDATE")
        self.assertFalse((repo / "tests").exists())
        self.assertFalse((self.tmp / "profiles").exists())


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------
class RunRecordTest(TempDirTest):
    def test_get_run_record_accepts_file_and_bundle_directory(self) -> None:
        record = {
            "arm": "C",
            "kb_enabled": True,
            "outcome": "PASS",
            "rounds": [],
            "final_failed": [],
            "scope_denials": [],
        }
        record_file = self.tmp / "record.json"
        record_file.write_text(json.dumps(record), encoding="utf-8")
        bundle = self.tmp / "bundle"
        bundle.mkdir()
        (bundle / "record.json").write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual(api.get_run_record(record_file), record)
        self.assertEqual(api.get_run_record(bundle), record)
        self.assertEqual(api.get_run_record(str(record_file)), record)

    def test_missing_or_invalid_record_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            api.get_run_record(self.tmp / "missing.json")

        bad = self.tmp / "bad.json"
        bad.write_text("not a record", encoding="utf-8")
        with self.assertRaises(ValueError):
            api.get_run_record(bad)

        array = self.tmp / "array.json"
        array.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            api.get_run_record(array)

    def test_list_runs_summarizes_skips_noise_and_sorts_newest_first(self) -> None:
        runs_dir = self.patch_runs_dir()
        runs_dir.mkdir()

        older = runs_dir / "a_older.json"
        older.write_text(
            json.dumps(
                {
                    "arm": "C",
                    "kb_enabled": True,
                    "outcome": "PASS",
                    "rounds": [{"round": 1}],
                    "final_failed": [],
                    "scope_denials": [],
                }
            ),
            encoding="utf-8",
        )
        newer = runs_dir / "b_newer.json"
        newer.write_text(
            json.dumps(
                {
                    "arm": "A",
                    "kb_enabled": False,
                    "outcome": "CAPPED",
                    "rounds": [],
                    "final_failed": ["tests/test_x.py::test_y"],
                    "scope_denials": [],
                }
            ),
            encoding="utf-8",
        )
        (runs_dir / "broken.json").write_text("{not json", encoding="utf-8")
        (runs_dir / "summary.json").write_text(
            json.dumps({"coupled": {"C": {"outcome": "PASS"}}}),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now, now))

        summaries = api.list_runs()

        self.assertEqual(
            [item["name"] for item in summaries],
            ["b_newer.json", "a_older.json"],
        )
        self.assertEqual(
            set(summaries[0]),
            {
                "path",
                "name",
                "modified_at",
                "arm",
                "kb_enabled",
                "outcome",
                "rounds",
                "final_failed_count",
            },
        )
        self.assertEqual(summaries[0]["arm"], "A")
        self.assertFalse(summaries[0]["kb_enabled"])
        self.assertEqual(summaries[0]["outcome"], "CAPPED")
        self.assertEqual(summaries[0]["rounds"], 0)
        self.assertEqual(summaries[0]["final_failed_count"], 1)
        self.assertEqual(summaries[1]["rounds"], 1)

    def test_list_runs_without_directory_is_empty(self) -> None:
        runs_dir = self.patch_runs_dir()
        self.assertFalse(runs_dir.exists())
        self.assertEqual(api.list_runs(), [])


# ---------------------------------------------------------------------------
# ChatSession（事件循环与 Agent 复用）
# ---------------------------------------------------------------------------
class ChatSessionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def test_two_messages_reuse_one_agent_and_one_event_loop(self) -> None:
        model = LoopBoundModel(fallback_reply="收到")
        session = api.create_chat_session(actors="scripted", model=model)
        self.assertIsNone(session.agent)

        first = await session.reply("第一条消息")
        agent = session.agent
        second = await session.reply("第二条消息")

        self.assertEqual(first, "收到")
        self.assertEqual(second, "收到")
        self.assertIsNotNone(agent)
        self.assertIs(session.agent, agent)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len({id(loop) for loop in model.call_loops}), 1)
        self.assertFalse(model.bound_loop.is_closed())

    async def test_tool_events_reach_the_sink(self) -> None:
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "kb_explain",
                    "input": {
                        "failed_tests": (
                            "tests/test_orders.py::test_checkout_total_with_coupon"
                        )
                    },
                },
                {"type": "text", "text": "已解释"},
            ]
        )
        events: list[dict] = []
        session = api.create_chat_session(model=model, event_sink=events.append, workspace=str(DEMO_REPO))

        reply = await session.reply("为什么派给 billing 专家？")

        self.assertEqual(reply, "已解释")
        tool_events = [
            event for event in events if event.get("event") == "tool_call"
        ]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["tool"], "kb_explain")
        self.assertEqual(api.EVENT_CHANNELS["tool_call"], api.CHANNEL_DISPATCHER)
        for event in events:
            _assert_event_matches_schema(self, event)

    async def test_empty_message_is_rejected_before_building_the_agent(self) -> None:
        session = api.create_chat_session(
            model=FakeChatModel(fallback_reply="收到")
        )
        with self.assertRaises(ValueError):
            await session.reply("   ")
        self.assertIsNone(session.agent)

    async def test_unknown_actor_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            api.create_chat_session(actors="paid")

    async def test_workspace_binding_is_passed_to_dispatcher(self) -> None:
        self.addCleanup(dispatcher._bind_default_workspace, None)
        session = api.create_chat_session(
            model=FakeChatModel(fallback_reply="收到"),
            workspace="/tmp/api-bound-project",
        )

        await session.reply("你好")

        self.assertIn(
            "当前绑定工作区: /tmp/api-bound-project",
            session.agent._system_prompt,
        )

    async def test_images_are_attached_only_when_the_model_can_see(self) -> None:
        """images 默认不附图；vision=true 时同一通道多图全发（PST1）。"""
        image = self.tmp / "shot.png" if hasattr(self, "tmp") else None
        temporary = tempfile.TemporaryDirectory(prefix="api1-image-")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        image = folder / "shot.png"
        extra = folder / "extra.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\noffline")
        extra.write_bytes(b"\x89PNG\r\n\x1a\nalso")

        blind = FakeChatModel(fallback_reply="没图", model_name="deepseek-chat")
        blind.formatter.input_types = ["text/plain", "image/*"]
        blind_session = api.create_chat_session(model=blind)
        self.assertEqual(await blind_session.reply("看图", images=[str(image)]), "没图")
        blind_blocks = [
            item["block_types"]
            for call in blind.calls
            for item in call["messages"]
            if item["role"] == "user"
        ]
        self.assertTrue(blind_blocks)
        self.assertNotIn("DataBlock", [name for row in blind_blocks for name in row])
        self.assertNotIn(
            "附图时你能看到图片内容", blind_session.agent._system_prompt
        )

        seeing = FakeChatModel(fallback_reply="看到了", model_name="deepseek-flash")
        seeing.formatter.input_types = ["text/plain", "image/*"]
        seeing_session = api.create_chat_session(model=seeing)
        reply = await seeing_session.reply(
            "看图", images=[str(image), str(extra)]
        )
        self.assertEqual(reply, "看到了")
        data_counts = [
            item["block_types"].count("DataBlock")
            for call in seeing.calls
            for item in call["messages"]
            if item["role"] == "user"
        ]
        self.assertEqual(sum(data_counts), 2)
        self.assertIn("Doctor Bug", seeing_session.agent._system_prompt)
        self.assertIn(
            "附图时你能看到图片内容，可据此回答。",
            seeing_session.agent._system_prompt,
        )

        plain = FakeChatModel(fallback_reply="纯文本", model_name="deepseek-flash")
        plain_session = api.create_chat_session(model=plain)
        self.assertEqual(await plain_session.reply("没有图"), "纯文本")
        self.assertNotIn(
            "DataBlock",
            [
                name
                for call in plain.calls
                for item in call["messages"]
                if item["role"] == "user"
                for name in item["block_types"]
            ],
        )


class ChatSessionLoopGuardTest(unittest.TestCase):
    def test_second_message_in_a_new_event_loop_is_rejected_clearly(self) -> None:
        session = api.create_chat_session(
            model=FakeChatModel(fallback_reply="收到")
        )
        self.assertEqual(asyncio.run(session.reply("第一条")), "收到")

        with self.assertRaises(RuntimeError) as caught:
            asyncio.run(session.reply("第二条"))

        self.assertIn("事件循环", str(caught.exception))
        self.assertNotIn("Event loop is closed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
