"""Offline tests for the native AgentScope actor backend (WIRE3).

No real model/API call is made here.  ``FakeChatModel`` (from the existing
``agentscope_ext`` package) is injected; the tool-call test only extends it in
this test file to emit AgentScope ``ToolCallBlock`` responses.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, DeepSeekChatModel, FinishedReason

from agentscope_ext.fake_model import FakeChatModel
from core.agentscope_actors import agentscope_act
from core.actors import FixResult
from core.model_config import ModelConfig, build_model, load_config
from core.runner import RunRecord, run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_VAULT = PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault"
DEMO_KB = PLATFORM_ROOT / "kb" / "dispatch_table.json"

from core.agentscope_actors import roster_from_table as _rft

DEMO_ROSTER = _rft(json.loads(DEMO_KB.read_text(encoding="utf-8")))

ASSIGNED = [
    "tests/test_billing.py::test_tax_on_discounted_total",
]
FAILED = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
]

_SECRET = "sk-wire3-offline-SECRET-do-not-leak-0123456789"
_RESPONSE_TIME = "1970-01-01T00:00:00.000000"


def _env_without(*names: str) -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in names}


def _text_response(index: int, text: str) -> ChatResponse:
    return ChatResponse(
        content=[
            TextBlock(
                text=text,
                id=f"wire3-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"wire3-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


class ScriptedToolModel(FakeChatModel):
    """Test-only fake model that can emit AgentScope tool calls.

    ``steps`` is consumed in order; each entry is either::

        {"type": "tool", "name": "Read", "input": {...}}
        {"type": "text", "text": "..."}

    Exhausted scripts fall back to a final text response so a broken loop
    cannot spin forever.
    """

    def __init__(self, steps: list[dict]) -> None:
        super().__init__(fallback_reply="script exhausted")
        self.steps = list(steps)
        self.invocations: list[dict] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        index = len(self.invocations)
        self.invocations.append(
            {
                "input_text": "\n".join(
                    message.get_text_content() or "" for message in messages
                ),
                "tools": [
                    tool["function"]["name"] for tool in (tools or [])
                ],
            }
        )
        if not self.steps:
            return _text_response(index, "done")

        step = self.steps.pop(0)
        if step["type"] == "tool":
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"wire3-tool-{index:04d}",
                        name=step["name"],
                        input=json.dumps(step["input"]),
                    )
                ],
                is_last=True,
                id=f"wire3-response-{index:04d}",
                created_at=_RESPONSE_TIME,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
                finished_reason=FinishedReason.COMPLETED,
            )
        return _text_response(index, step["text"])


class ExplodingModel(FakeChatModel):
    def __init__(self) -> None:
        super().__init__(fallback_reply="unused")

    async def _call_api(self, *args: object, **kwargs: object) -> ChatResponse:
        raise RuntimeError("offline explosion")


class SlowModel(FakeChatModel):
    def __init__(self) -> None:
        super().__init__(fallback_reply="unused")

    async def _call_api(self, *args: object, **kwargs: object) -> ChatResponse:
        await asyncio.sleep(30)
        return _text_response(0, "never reached")


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wire3-actors-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = self.tmp / "workspace"
        backoffice = self.workspace / "backoffice"
        backoffice.mkdir(parents=True)
        for name in ("billing", "inventory", "auth", "db"):
            (backoffice / f"{name}.py").write_text(
                f'NAME = "{name}"\n', encoding="utf-8"
            )


class ModelConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wire3-config-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        env = _env_without(
            "PLATFORM_MODEL",
            "PLATFORM_MODEL_PROVIDER",
            "PLATFORM_MODEL_API_KEY_ENV",
        )
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config(self, payload: dict) -> Path:
        path = self.tmp / "model_config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_model_raises_actionable_error_without_secret(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": _SECRET}):
            path = self._config({"provider": "deepseek"})
            with self.assertRaises(ValueError) as caught:
                load_config(path)

        message = str(caught.exception)
        self.assertIn("model", message)
        self.assertIn("model_config.example.json", message)
        self.assertNotIn(_SECRET, message)
        self.assertNotIn(_SECRET, repr(caught.exception))

    def test_build_model_missing_model_does_not_leak_key(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": _SECRET}):
            cfg = ModelConfig(
                provider="deepseek",
                model="",
                api_key_env="DEEPSEEK_API_KEY",
            )
            with self.assertRaises(ValueError) as caught:
                build_model(cfg)

        rendered = "\n".join(
            (str(caught.exception), repr(caught.exception), repr(cfg))
        )
        self.assertNotIn(_SECRET, rendered)

    def test_missing_key_env_is_reported_by_name_only(self):
        cfg = ModelConfig(
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="WIRE3_ABSENT_KEY",
        )
        with self.assertRaises(RuntimeError) as caught:
            build_model(cfg)

        message = str(caught.exception)
        self.assertIn("WIRE3_ABSENT_KEY", message)
        self.assertNotIn(_SECRET, message)

    def test_deepseek_build_masks_the_key_in_repr(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": _SECRET}):
            model = build_model(ModelConfig(model="deepseek-chat"))

        self.assertIsInstance(model, DeepSeekChatModel)
        self.assertEqual(model.model, "deepseek-chat")
        self.assertNotIn(_SECRET, repr(model))
        self.assertNotIn(_SECRET, repr(model.credential))
        self.assertEqual(
            model.credential.api_key.get_secret_value(), _SECRET
        )

    def test_unknown_provider_raises_not_implemented_with_extension_point(self):
        cfg = ModelConfig(provider="openai", model="some-model")
        with self.assertRaises(NotImplementedError) as caught:
            build_model(cfg)

        message = str(caught.exception)
        self.assertIn("openai", message)
        self.assertIn("deepseek", message)
        self.assertIn("Credential", message)
        self.assertIn("build_model", message)

    def test_defaults_fill_provider_and_key_env_name(self):
        path = self._config({"model": "deepseek-chat"})
        cfg = load_config(path)

        self.assertEqual(cfg.provider, "deepseek")
        self.assertEqual(cfg.model, "deepseek-chat")
        self.assertEqual(cfg.api_key_env, "DEEPSEEK_API_KEY")
        self.assertNotIn(_SECRET, repr(cfg))


class AgentscopeActTest(WorkspaceCase):
    def test_no_file_change_is_not_applied_and_agent_completes(self):
        model = FakeChatModel(replies=["nothing to change"])

        result = agentscope_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            roster=DEMO_ROSTER,
            model=model,
            timeout_s=30,
        )

        self.assertIsInstance(result, FixResult)
        self.assertEqual(result.actor_id, "billing-specialist")
        self.assertFalse(result.applied)
        self.assertIn("completed", result.note)
        self.assertIn("changed=none", result.note)
        self.assertIn("elapsed=", result.note)
        self.assertEqual(model.call_count, 1)

        call = model.calls[0]
        self.assertIn("billing-specialist", call["input_text"])
        self.assertIn("只允许修改 backoffice/billing.py", call["input_text"])
        self.assertIn(ASSIGNED[0], call["input_text"])
        self.assertIn(str(self.workspace.resolve()), call["input_text"])
        self.assertEqual(
            call["tools"], ["Read", "Write", "Edit", "Glob", "Shell"]
        )
        self.assertNotIn("Bash", call["tools"])
        self.assertNotIn("Grep", call["tools"])

    def test_tool_call_read_then_edit_changes_file_and_is_applied(self):
        target = self.workspace / "backoffice" / "billing.py"
        target.write_text(
            'NAME = "billing"\nBROKEN = True\n', encoding="utf-8"
        )
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
                        "old_string": "BROKEN = True",
                        "new_string": "BROKEN = False",
                    },
                },
                {"type": "text", "text": "fixed billing"},
            ]
        )

        result = agentscope_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            roster=DEMO_ROSTER,
            model=model,
            timeout_s=30,
        )

        self.assertTrue(result.applied)
        self.assertIn("completed", result.note)
        self.assertIn("backoffice/billing.py", result.note)
        self.assertIn("fixed billing", result.note)
        self.assertIn("BROKEN = False", target.read_text(encoding="utf-8"))
        self.assertEqual(len(model.invocations), 3)
        self.assertEqual(
            model.invocations[0]["tools"],
            ["Read", "Write", "Edit", "Glob", "Shell"],
        )

    def test_exception_is_reported_not_raised(self):
        result = agentscope_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            roster=DEMO_ROSTER,
            model=ExplodingModel(),
            timeout_s=30,
        )

        self.assertFalse(result.applied)
        self.assertIn("error=RuntimeError: offline explosion", result.note)
        self.assertIn("changed=none", result.note)

    def test_timeout_is_reported_not_raised(self):
        result = agentscope_act(
            "billing-specialist",
            self.workspace,
            ASSIGNED,
            FAILED,
            roster=DEMO_ROSTER,
            model=SlowModel(),
            timeout_s=0.05,
        )

        self.assertFalse(result.applied)
        self.assertIn("timeout", result.note)
        self.assertIn("changed=none", result.note)

    def test_unknown_actor_is_reported_not_raised(self):
        result = agentscope_act(
            "nobody",
            self.workspace,
            ASSIGNED,
            FAILED,
            roster=DEMO_ROSTER,
            model=FakeChatModel(replies=["unused"]),
            timeout_s=30,
        )

        self.assertFalse(result.applied)
        self.assertIn("error=KeyError", result.note)

    def test_default_model_is_built_when_not_injected(self):
        # No model_config.json is read here: the module-level path is patched
        # to a temp file and the key comes from a temp environment variable,
        # so the real DeepSeek model is constructed but never called.
        cfg_path = self.tmp / "model_config.json"
        cfg_path.write_text(
            json.dumps({"model": "deepseek-chat"}), encoding="utf-8"
        )
        with mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": _SECRET,
                "PLATFORM_MODEL": "",
                "PLATFORM_MODEL_PROVIDER": "",
                "PLATFORM_MODEL_API_KEY_ENV": "",
            },
        ):
            with mock.patch(
                "core.model_config.DEFAULT_CONFIG_PATH", cfg_path
            ):
                with mock.patch(
                    "core.agentscope_actors._run_agent",
                    new=mock.AsyncMock(return_value="built"),
                ) as run_agent:
                    result = agentscope_act(
                        "billing-specialist",
                        self.workspace,
                        ASSIGNED,
                        FAILED,
                        roster=DEMO_ROSTER,
                        timeout_s=30,
                    )

        built_model = run_agent.call_args.args[3]
        self.assertIsInstance(built_model, DeepSeekChatModel)
        self.assertNotIn(_SECRET, repr(built_model))
        self.assertFalse(result.applied)
        self.assertIn("completed", result.note)
        self.assertIn("reply=built", result.note)


class RunnerAgentscopeBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wire3-runner-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name) / "work"

    def test_scripted_default_does_not_touch_agentscope_backend(self):
        self.assertEqual(
            inspect.signature(run_arm)
            .parameters["actor_backend"]
            .default,
            "scripted",
        )

        with mock.patch("core.agentscope_actors.agentscope_act") as backend:
            record = run_arm(
                "A",
                DEMO_REPO,
                max_rounds=1,
                work_root=self.work / "scripted",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )

        backend.assert_not_called()
        self.assertEqual(record.outcome, "CAPPED")

    def test_agentscope_backend_is_selected_and_receives_assignments(self):
        mock_result = FixResult(
            actor_id="generalist", applied=False, note="mock agentscope"
        )

        with mock.patch(
            "core.agentscope_actors.agentscope_act",
            return_value=mock_result,
        ) as backend:
            record = run_arm(
                "A",
                DEMO_REPO,
                max_rounds=1,
                work_root=self.work / "agentscope",
                actor_backend="agentscope",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )

        self.assertEqual(backend.call_count, 1)
        actor_id, workspace, assigned, failed = backend.call_args.args
        self.assertEqual(actor_id, "generalist")
        self.assertTrue(Path(workspace).is_dir())
        self.assertEqual(len(assigned), 8)
        self.assertEqual(len(failed), 8)
        self.assertIn(ASSIGNED[0], assigned)
        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(
            record.rounds[0]["fix_results"][0]["note"], "mock agentscope"
        )

    def test_unknown_backend_is_rejected_before_copying(self):
        with self.assertRaises(ValueError):
            run_arm(
                "A",
                DEMO_REPO,
                max_rounds=0,
                work_root=self.work / "bad",
                actor_backend="bogus",
            )

        self.assertFalse((self.work / "bad").exists())


class RunnerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wire3-cli-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_actors_flag_accepts_agentscope(self):
        from core.__main__ import main

        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        out = self.tmp / "agentscope.json"
        argv = [
            "run",
            "--arm",
            "C",
            "--workspace",
            str(DEMO_REPO),
            "--out",
            str(out),
            "--vault",
            str(DEMO_VAULT),
            "--kb",
            str(DEMO_KB),
        ]
        with mock.patch(
            "core.__main__.run_arm", return_value=record
        ) as run_mock:
            self.assertEqual(main(argv), 0)

        self.assertEqual(
            run_mock.call_args.kwargs["actor_backend"], "agentscope"
        )
        self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()
