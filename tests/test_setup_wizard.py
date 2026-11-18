# -*- coding: utf-8 -*-
"""SETUP1 (重制计划阶段 1): first-run setup wizard core.

The wizard is the product's front door: provider choice -> model ->
API key, persisted as non-secret config JSON + a 0600 env file.  These
tests isolate everything the way a *fresh install* sees it — platform
fallback config/env pointed at an empty directory and a throwaway
XDG_CONFIG_HOME — because in the dev tree the platform's own files
would mask exactly the state a new user starts from.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import core.model_config as mc
from core.setup_wizard import (
    choice_for,
    needs_setup,
    provider_choices,
    run_setup,
    setup_status,
)


class _IsolatedHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patchers = [
            mock.patch.object(mc, "DEFAULT_CONFIG_PATH", root / "no_cfg.json"),
            mock.patch.object(mc, "DEFAULT_ENV_PATH", root / "no_env.local"),
            mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(root / "xdg")}
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self) -> None:
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()


class ProviderListTest(_IsolatedHome):
    def test_deepseek_first_and_only_verified(self):
        choices = provider_choices()
        self.assertEqual(choices[0].provider, "deepseek")
        self.assertTrue(choices[0].verified)
        self.assertEqual(choices[0].default_model, "deepseek-flash")
        self.assertTrue(all(not c.verified for c in choices[1:]))
        self.assertEqual(len(choices), 8)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            choice_for("not-a-provider")


class FreshInstallTest(_IsolatedHome):
    def test_needs_setup_on_fresh_install(self):
        self.assertTrue(needs_setup())
        status = setup_status()
        self.assertFalse(status.configured)
        self.assertTrue(status.problems)

    def test_run_setup_flips_status_and_never_stores_key_in_json(self):
        result = run_setup("deepseek", "deepseek-flash", "sk-secret-1")
        self.assertFalse(needs_setup())
        status = setup_status()
        self.assertTrue(status.configured)
        self.assertEqual(status.provider, "deepseek")
        self.assertEqual(status.model, "deepseek-flash")
        self.assertTrue(status.key_set)
        config_text = result.config_path.read_text(encoding="utf-8")
        self.assertNotIn("sk-secret-1", config_text)
        data = json.loads(config_text)
        self.assertEqual(data["provider"], "deepseek")
        self.assertEqual(data["api_key_env"], "DEEPSEEK_API_KEY")
        env_text = result.env_path.read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_API_KEY=sk-secret-1", env_text)
        mode = stat.S_IMODE(result.env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_run_setup_preserves_existing_config_keys(self):
        config_path = mc.user_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"active_profile": "default"}), encoding="utf-8"
        )
        run_setup("deepseek", "deepseek-flash", "sk-secret-2")
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["active_profile"], "default")
        self.assertEqual(data["provider"], "deepseek")

    def test_run_setup_replaces_key_line_instead_of_duplicating(self):
        run_setup("deepseek", "deepseek-flash", "sk-old")
        result = run_setup("deepseek", "deepseek-flash", "sk-new")
        env_text = result.env_path.read_text(encoding="utf-8")
        self.assertEqual(env_text.count("DEEPSEEK_API_KEY="), 1)
        self.assertIn("sk-new", env_text)
        self.assertNotIn("sk-old", env_text)

    def test_empty_inputs_rejected(self):
        with self.assertRaises(ValueError):
            run_setup("deepseek", "", "sk-x")
        with self.assertRaises(ValueError):
            run_setup("deepseek", "deepseek-flash", "  ")
        with self.assertRaises(ValueError):
            run_setup("nope", "m", "sk-x")


if __name__ == "__main__":
    unittest.main()
