# -*- coding: utf-8 -*-
"""MODEL2：角色→档案映射 — 可选层、工人档自动降档、展示标记。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import model_config
from core.model_config import (
    activate_role_profile,
    build_model,
    effective_role_profiles,
    ensure_worker_profile,
    load_profile_catalog,
    load_role_profiles,
    resolve_role_profile,
    step_down_effort,
)


def _write_config(tmp: str, data: dict) -> Path:
    path = Path(tmp) / "model_config.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _profiles_data(**extra) -> dict:
    data = {
        "profiles": [
            {
                "name": "boss",
                "provider": "deepseek",
                "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "reasoning_effort": "max",
            },
            {
                "name": "alt",
                "provider": "deepseek",
                "model": "deepseek-reasoner",
                "api_key_env": "DEEPSEEK_API_KEY",
                "reasoning_effort": "max",
            },
        ],
        "active_profile": "boss",
    }
    data.update(extra)
    return data


class LadderTests(unittest.TestCase):
    def test_ladders_match_provider_spec_sets(self):
        for provider, spec in model_config._PROVIDER_SPECS.items():
            ladder = model_config._EFFORT_LADDERS[provider]
            self.assertEqual(set(ladder), set(spec.reasoning_efforts))
            self.assertEqual(len(ladder), len(set(ladder)))

    def test_step_down_rules(self):
        self.assertEqual(step_down_effort("deepseek", "max"), "high")
        self.assertIsNone(step_down_effort("deepseek", "high"))
        self.assertEqual(step_down_effort("anthropic", "xhigh"), "high")
        self.assertIsNone(step_down_effort("openai_chat", "minimal"))
        self.assertEqual(step_down_effort("openai_chat", "low"), "minimal")
        self.assertIsNone(step_down_effort("deepseek", None))
        self.assertIsNone(step_down_effort("deepseek", "ultra"))
        self.assertIsNone(step_down_effort("dashscope", "high"))


class NoMappingTests(unittest.TestCase):
    def test_roles_fall_back_to_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _profiles_data())
            self.assertEqual(load_role_profiles(path), {})
            self.assertEqual(
                resolve_role_profile("secretary", path).name, "boss"
            )
            self.assertEqual(
                resolve_role_profile("worker", path).name, "boss"
            )
            roles = effective_role_profiles(path)
            self.assertFalse(roles["mapped"])
            self.assertEqual(roles["worker"], "boss")
            before = path.read_text(encoding="utf-8")
            resolve_role_profile("worker", path)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_build_model_role_matches_plain_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _profiles_data())
            with mock.patch.dict(
                os.environ, {"DEEPSEEK_API_KEY": "test-only"}
            ):
                plain = build_model(
                    model_config.resolve_profile("boss", path)
                )
                worker = build_model(role="worker", config_path=path)
            self.assertEqual(plain.model, worker.model)


class MappingTests(unittest.TestCase):
    def test_named_roles_resolve_and_missing_name_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                tmp,
                _profiles_data(
                    role_profiles={"secretary": "alt", "worker": "boss"}
                ),
            )
            self.assertEqual(
                resolve_role_profile("secretary", path).name, "alt"
            )
            self.assertEqual(
                resolve_role_profile("worker", path).name, "boss"
            )
            bad = _write_config(
                tmp,
                _profiles_data(role_profiles={"secretary": "ghost"}),
            )
            with self.assertRaises(ValueError):
                resolve_role_profile("secretary", bad)

    def test_worker_auto_generated_stepped_down_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                tmp, _profiles_data(role_profiles={"secretary": "boss"})
            )
            worker = ensure_worker_profile(path)
            self.assertEqual(worker.name, "boss-worker")
            self.assertTrue(worker.auto_generated)
            self.assertEqual(worker.reasoning_effort, "high")
            self.assertEqual(
                resolve_role_profile("secretary", path).reasoning_effort,
                "max",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["role_profiles"]["worker"], "boss-worker")
            entry = next(
                p for p in saved["profiles"] if p["name"] == "boss-worker"
            )
            self.assertTrue(entry["auto_generated"])
            self.assertEqual(entry["reasoning_effort"], "high")
            self.assertNotIn("test-only", path.read_text(encoding="utf-8"))
            again = path.read_text(encoding="utf-8")
            self.assertEqual(ensure_worker_profile(path).name, "boss-worker")
            self.assertEqual(path.read_text(encoding="utf-8"), again)

    def test_worker_floor_keeps_lowest_rung(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _profiles_data(role_profiles={"secretary": "boss"})
            data["profiles"][0]["reasoning_effort"] = "high"
            path = _write_config(tmp, data)
            worker = ensure_worker_profile(path)
            self.assertEqual(worker.reasoning_effort, "high")

    def test_display_resolve_never_generates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                tmp, _profiles_data(role_profiles={"secretary": "boss"})
            )
            roles = effective_role_profiles(path)
            self.assertTrue(roles["mapped"])
            self.assertIsNone(roles["worker"])
            self.assertEqual(
                load_profile_catalog(path).names, ("boss", "alt")
            )


class ActivateRoleTests(unittest.TestCase):
    def test_mapped_switch_moves_secretary_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(
                tmp,
                _profiles_data(
                    role_profiles={"secretary": "boss", "worker": "boss"}
                ),
            )
            activate_role_profile("alt", config_path=path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["role_profiles"]["secretary"], "alt")
            self.assertEqual(saved["role_profiles"]["worker"], "boss")
            self.assertEqual(saved["active_profile"], "boss")

    def test_unmapped_switch_is_legacy_activate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_config(tmp, _profiles_data())
            activate_role_profile("alt", config_path=path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["active_profile"], "alt")
            self.assertNotIn("role_profiles", saved)


class DisplayTests(unittest.TestCase):
    def test_profile_line_marks_and_role_lines(self):
        from core.cli import _profile_line, _role_lines
        from core.model_config import ModelProfile

        profile = ModelProfile(
            name="boss",
            provider="deepseek",
            model="deepseek-flash",
            api_key_env="DEEPSEEK_API_KEY",
            reasoning_effort="max",
            auto_generated=True,
        )
        line = _profile_line(profile, True)
        self.assertIn("看图 有", line)
        self.assertIn("思考 有", line)
        self.assertIn("自动生成", line)
        self.assertIsInstance(_role_lines(), list)


if __name__ == "__main__":
    unittest.main()
