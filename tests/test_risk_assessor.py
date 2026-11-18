# -*- coding: utf-8 -*-
"""Backlog 6a: model risk assessor may only escalate auto-review allows."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import model_config
from core.policy import PolicyContext, PolicyEngine, Rule, append_global_rule


def _ctx(ws: Path, mode: str) -> PolicyContext:
    return PolicyContext(
        mode=mode,
        surface="interactive",
        workspace=ws,
        vault_paths=(),
        role="secretary",
        is_ai=True,
        actor="secretary",
    )


class AssessorGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="risk-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.ws = root / "ws"
        self.ws.mkdir(parents=True)
        (self.ws / "code.py").write_text("x = 1\n", encoding="utf-8")
        self.policy_file = root / "policies.toml"
        append_global_rule(
            Rule("secretary", "read_file", str(self.ws / "code.py"), "ask"),
            path=self.policy_file,
        )

    def _engine(self, assessor) -> PolicyEngine:
        return PolicyEngine(
            workspace=self.ws,
            global_policy_path=self.policy_file,
            risk_assessor=assessor,
        )

    def _evaluate(self, engine: PolicyEngine):
        return engine.evaluate(
            "secretary",
            "read_file",
            str(self.ws / "code.py"),
            _ctx(self.ws, "auto-review"),
        )

    def test_no_assessor_keeps_checklist_allow(self) -> None:
        d = self._evaluate(self._engine(None))
        self.assertEqual(d.verdict, "allow")
        self.assertIn("[自动审核放行]", d.reason)

    def test_ok_assessment_keeps_allow(self) -> None:
        d = self._evaluate(self._engine(lambda a, b, c: False))
        self.assertEqual(d.verdict, "allow")

    def test_escalate_vetoes_conversion_back_to_ask(self) -> None:
        d = self._evaluate(self._engine(lambda a, b, c: True))
        self.assertEqual(d.verdict, "ask")
        self.assertIn("模型风险评估升级人工", d.reason)

    def test_assessor_exception_escalates(self) -> None:
        def boom(a, b, c):
            raise RuntimeError("model down")

        d = self._evaluate(self._engine(boom))
        self.assertEqual(d.verdict, "ask")

    def test_assessor_not_consulted_outside_auto_review(self) -> None:
        calls: list = []

        def spy(a, b, c):
            calls.append((a, b, c))
            return True

        engine = self._engine(spy)
        d = engine.evaluate(
            "secretary",
            "read_file",
            str(self.ws / "code.py"),
            _ctx(self.ws, "ask"),
        )
        self.assertEqual(d.verdict, "ask")
        self.assertEqual(calls, [])


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class FactoryTests(unittest.TestCase):
    def _with_factory(self, reply_text: str):
        profile = types.SimpleNamespace(
            base_url="https://example.invalid/v1",
            provider="deepseek",
            model="fake-model",
            api_key_env="RISK_TEST_KEY",
        )
        originals = (
            model_config._load_merged,
            model_config.resolve_role_profile,
        )
        model_config._load_merged = lambda path: (
            {"auto_review_model_assessment": True},
            "test",
        )
        model_config.resolve_role_profile = lambda role: profile
        fake_httpx = types.SimpleNamespace(
            post=lambda *a, **k: _FakeResponse(reply_text)
        )
        old_httpx = sys.modules.get("httpx")
        sys.modules["httpx"] = fake_httpx
        os.environ["RISK_TEST_KEY"] = "sk-test"
        try:
            assessor = model_config.build_model_risk_assessor()
            self.assertIsNotNone(assessor)
            return assessor("secretary", "read_file", "/ws/code.py")
        finally:
            model_config._load_merged, model_config.resolve_role_profile = (
                originals
            )
            if old_httpx is None:
                sys.modules.pop("httpx", None)
            else:
                sys.modules["httpx"] = old_httpx
            os.environ.pop("RISK_TEST_KEY", None)

    def test_flag_off_returns_none(self) -> None:
        self.assertIsNone(model_config.build_model_risk_assessor())

    def test_ok_reply_means_no_escalation(self) -> None:
        self.assertFalse(self._with_factory("OK"))

    def test_escalate_reply_escalates(self) -> None:
        self.assertTrue(self._with_factory("ESCALATE"))

    def test_garbage_reply_escalates(self) -> None:
        self.assertTrue(self._with_factory("大概没问题"))


if __name__ == "__main__":
    unittest.main()
