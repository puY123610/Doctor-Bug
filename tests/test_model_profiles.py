# -*- coding: utf-8 -*-
"""MODEL1：多供应商 profile 配置、构建与 CLI 的离线测试。

全程零网络、零付费：每个 provider 只用一个假密钥构建客户端对象，不断言
任何远程行为；密钥值只存在于测试自建的临时环境变量里，并断言它不会出现在
配置对象、错误信息、CLI 输出或写回文件中。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope.model import (
    AnthropicChatModel,
    DashScopeChatModel,
    DeepSeekChatModel,
    MiniMaxChatModel,
    MoonshotChatModel,
    OpenAIChatModel,
    OpenAIResponseModel,
    VolcengineChatModel,
)

from core import cli, model_config
from core.model_config import (
    ModelProfile,
    activate_profile,
    build_model,
    load_config,
    load_profile_catalog,
    resolve_profile,
)

_ENV_OVERRIDES = (
    "PLATFORM_MODEL_PROVIDER",
    "PLATFORM_MODEL",
    "PLATFORM_MODEL_API_KEY_ENV",
    "PLATFORM_MODEL_PROFILE",
    "DRBUG_TOKEN_METER",
)

_WIRED_MODELS = (
    ("deepseek", DeepSeekChatModel),
    ("anthropic", AnthropicChatModel),
    ("dashscope", DashScopeChatModel),
    ("minimax", MiniMaxChatModel),
    ("moonshot", MoonshotChatModel),
    ("openai_chat", OpenAIChatModel),
    ("openai_response", OpenAIResponseModel),
    ("volcengine", VolcengineChatModel),
)


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="model1-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.platform_config = self.tmp / "model_config.json"
        self.user_config = self.tmp / "user_model_config.json"
        self.user_env = self.tmp / "bugdoc.env"

    def _write(self, payload: dict, path: Path | None = None) -> Path:
        target = path or self.platform_config
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def _patches(self):
        return (
            mock.patch.object(
                model_config, "DEFAULT_CONFIG_PATH", self.platform_config
            ),
            mock.patch.object(
                model_config, "user_config_path", lambda: self.user_config
            ),
            mock.patch.object(
                model_config, "user_env_path", lambda: self.user_env
            ),
            mock.patch.dict(os.environ, {name: "" for name in _ENV_OVERRIDES}),
        )

    def _enter(self):
        patches = self._patches()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class LegacyConfigTest(TempDirTest):
    def test_legacy_single_config_is_one_implicit_profile(self) -> None:
        path = self._write(
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "api_key_env": "MODEL1_LEGACY_KEY",
            }
        )
        cfg = load_config(path)
        self.assertEqual(cfg.provider, "deepseek")
        self.assertEqual(cfg.model, "deepseek-chat")
        self.assertEqual(cfg.api_key_env, "MODEL1_LEGACY_KEY")
        self.assertEqual(cfg.profile, "default")
        self.assertIsNone(cfg.base_url)
        self.assertIsNone(cfg.reasoning_effort)

        catalog = load_profile_catalog(path)
        self.assertFalse(catalog.explicit)
        self.assertEqual(catalog.names, ("default",))
        self.assertEqual(catalog.active, "default")

    def test_legacy_environment_overrides_still_win(self) -> None:
        path = self._write(
            {"provider": "deepseek", "model": "deepseek-chat"}
        )
        with mock.patch.dict(
            os.environ,
            {
                "PLATFORM_MODEL_PROVIDER": "anthropic",
                "PLATFORM_MODEL": "claude-env",
                "PLATFORM_MODEL_API_KEY_ENV": "MODEL1_ENV_KEY",
            },
        ):
            cfg = load_config(path)
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.model, "claude-env")
        self.assertEqual(cfg.api_key_env, "MODEL1_ENV_KEY")

    def test_missing_model_message_is_unchanged(self) -> None:
        path = self._write({"provider": "deepseek"})
        with self.assertRaises(ValueError) as caught:
            load_config(path)
        message = str(caught.exception)
        self.assertIn("model_config.example.json", message)
        self.assertIn("No default model is assumed", message)


class ProfilesConfigTest(TempDirTest):
    def test_profiles_select_active_and_expose_effort(self) -> None:
        path = self._write(
            {
                "profiles": [
                    {
                        "name": "work",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_WORK_KEY",
                        "reasoning_effort": "max",
                    },
                    {
                        "name": "claude",
                        "provider": "anthropic",
                        "model": "claude-test",
                        "api_key_env": "MODEL1_CLAUDE_KEY",
                        "base_url": "https://example.invalid",
                    },
                ],
                "active_profile": "claude",
            }
        )
        cfg = load_config(path)
        self.assertEqual(cfg.profile, "claude")
        self.assertEqual(cfg.provider, "anthropic")
        self.assertEqual(cfg.model, "claude-test")
        self.assertEqual(cfg.base_url, "https://example.invalid")

        catalog = load_profile_catalog(path)
        self.assertTrue(catalog.explicit)
        self.assertEqual(catalog.names, ("work", "claude"))
        self.assertEqual(catalog.active, "claude")
        self.assertEqual(
            catalog.active_profile().reasoning_effort, None
        )
        self.assertEqual(catalog.profiles[0].reasoning_effort, "max")

    def test_user_file_can_select_a_platform_profile(self) -> None:
        self._enter()
        self._write(
            {
                "profiles": [
                    {
                        "name": "a",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_A_KEY",
                    },
                    {
                        "name": "b",
                        "provider": "openai_chat",
                        "model": "gpt-test",
                        "api_key_env": "MODEL1_B_KEY",
                    },
                ],
                "active_profile": "a",
            }
        )
        self.user_config.write_text(
            json.dumps({"active_profile": "b"}), encoding="utf-8"
        )
        catalog = load_profile_catalog()
        self.assertEqual(catalog.names, ("a", "b"))
        self.assertEqual(catalog.active, "b")
        self.assertEqual(load_config().model, "gpt-test")

    def test_user_profiles_replace_platform_profiles_wholesale(self) -> None:
        self._enter()
        self._write(
            {
                "profiles": [
                    {
                        "name": "a",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_A_KEY",
                    }
                ],
                "active_profile": "a",
            }
        )
        self.user_config.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "c",
                            "provider": "moonshot",
                            "model": "kimi-test",
                            "api_key_env": "MODEL1_C_KEY",
                        }
                    ],
                    "active_profile": "c",
                }
            ),
            encoding="utf-8",
        )
        catalog = load_profile_catalog()
        self.assertEqual(catalog.names, ("c",))
        self.assertTrue(catalog.explicit)
        self.assertEqual(load_config().provider, "moonshot")

    def test_unknown_active_profile_lists_available_names(self) -> None:
        path = self._write(
            {
                "profiles": [
                    {
                        "name": "a",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_A_KEY",
                    }
                ],
                "active_profile": "missing",
            }
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path)
        self.assertIn("missing", str(caught.exception))
        self.assertIn("a", str(caught.exception))

    def test_unknown_profile_lookup_lists_available_names(self) -> None:
        path = self._write(
            {
                "profiles": [
                    {
                        "name": "a",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_A_KEY",
                    }
                ]
            }
        )
        with self.assertRaises(ValueError) as caught:
            resolve_profile("nope", path)
        self.assertIn("nope", str(caught.exception))
        self.assertIn("available: a", str(caught.exception))

    def test_profile_shape_errors_are_actionable(self) -> None:
        cases = (
            (
                {"profiles": []},
                "non-empty JSON array",
            ),
            (
                {
                    "profiles": [
                        {
                            "provider": "deepseek",
                            "model": "x",
                            "api_key_env": "K",
                        }
                    ]
                },
                "name",
            ),
            (
                {
                    "profiles": [
                        {
                            "name": "a",
                            "provider": "deepseek",
                            "model": "x",
                            "api_key_env": "K",
                        },
                        {
                            "name": "a",
                            "provider": "deepseek",
                            "model": "y",
                            "api_key_env": "K2",
                        },
                    ]
                },
                "duplicate",
            ),
            (
                {
                    "profiles": [
                        {
                            "name": "a",
                            "provider": "unknown_provider",
                            "model": "x",
                            "api_key_env": "K",
                        }
                    ]
                },
                "unsupported provider",
            ),
            (
                {
                    "profiles": [
                        {
                            "name": "a",
                            "provider": "deepseek",
                            "model": "x",
                            "api_key_env": "K",
                            "api_key": "must-not-be-here",
                        }
                    ]
                },
                "must not contain",
            ),
        )
        for index, (payload, needle) in enumerate(cases):
            with self.subTest(index=index):
                path = self.tmp / f"bad-{index}.json"
                self._write(payload, path)
                with self.assertRaises(ValueError) as caught:
                    load_config(path)
                self.assertIn(needle, str(caught.exception))

    def test_reasoning_effort_is_validated_at_load(self) -> None:
        dashscope = self._write(
            {
                "profiles": [
                    {
                        "name": "qwen",
                        "provider": "dashscope",
                        "model": "qwen-plus",
                        "api_key_env": "MODEL1_QWEN_KEY",
                        "reasoning_effort": "high",
                    }
                ]
            },
            self.tmp / "dashscope.json",
        )
        with self.assertRaises(ValueError) as caught:
            load_config(dashscope)
        self.assertIn("does not support reasoning_effort", str(caught.exception))

        bad_value = self._write(
            {
                "profiles": [
                    {
                        "name": "ds",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_DS_KEY",
                        "reasoning_effort": "medium",
                    }
                ]
            },
            self.tmp / "bad-effort.json",
        )
        with self.assertRaises(ValueError) as caught:
            load_config(bad_value)
        message = str(caught.exception)
        self.assertIn("'medium'", message)
        self.assertIn("high, max", message)


class ActivateProfileTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self._enter()
        self._write(
            {
                "profiles": [
                    {
                        "name": "a",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_A_KEY",
                    },
                    {
                        "name": "b",
                        "provider": "openai_chat",
                        "model": "gpt-test",
                        "api_key_env": "MODEL1_B_KEY",
                    },
                ],
                "active_profile": "a",
            }
        )

    def test_activate_writes_only_active_profile_and_preserves_other_keys(
        self,
    ) -> None:
        sentinel = "sk-model1-must-not-be-written"
        self.user_config.write_text(
            json.dumps({"active_profile": "a", "max_rounds": 2}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"MODEL1_B_KEY": sentinel}):
            written = activate_profile("b")
            self.assertEqual(written, self.user_config)
            payload = json.loads(
                self.user_config.read_text(encoding="utf-8")
            )
            self.assertEqual(payload["active_profile"], "b")
            self.assertEqual(payload["max_rounds"], 2)
            self.assertEqual(load_config().model, "gpt-test")

        serialized = self.user_config.read_text(encoding="utf-8")
        self.assertNotIn(sentinel, serialized)

    def test_activate_unknown_profile_does_not_write(self) -> None:
        with self.assertRaises(ValueError) as caught:
            activate_profile("missing")
        self.assertIn("available: a, b", str(caught.exception))
        self.assertFalse(self.user_config.exists())


class ProviderBuildTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self._enter()

    def test_every_wired_provider_builds_offline_with_fake_key(self) -> None:
        for provider, expected_cls in _WIRED_MODELS:
            with self.subTest(provider=provider):
                env_name = f"MODEL1_{provider.upper()}_KEY"
                sentinel = f"sk-fake-{provider}-secret"
                profile = ModelProfile(
                    name=provider,
                    provider=provider,
                    model=f"{provider}-model",
                    api_key_env=env_name,
                )
                with mock.patch.dict(os.environ, {env_name: sentinel}):
                    model = build_model(profile)
                self.assertIsInstance(model, expected_cls)
                self.assertEqual(model.model, f"{provider}-model")
                self.assertNotIn(sentinel, repr(model))
                self.assertNotIn(sentinel, repr(model.credential))

    def test_base_url_is_passed_to_credential(self) -> None:
        profile = ModelProfile(
            name="custom",
            provider="openai_chat",
            model="gpt-test",
            api_key_env="MODEL1_CUSTOM_KEY",
            base_url="https://example.invalid/v1",
        )
        with mock.patch.dict(
            os.environ, {"MODEL1_CUSTOM_KEY": "sk-fake"}
        ):
            model = build_model(profile)
        self.assertEqual(
            model.credential.base_url, "https://example.invalid/v1"
        )

    def test_reasoning_effort_is_passed_with_thinking_enabled(self) -> None:
        cases = (
            ("deepseek", "max"),
            ("anthropic", "xhigh"),
            ("openai_chat", "high"),
            ("openai_response", "high"),
            ("moonshot", "max"),
            ("volcengine", "high"),
        )
        for provider, effort in cases:
            with self.subTest(provider=provider):
                env_name = f"MODEL1_EFFORT_{provider.upper()}_KEY"
                profile = ModelProfile(
                    name=provider,
                    provider=provider,
                    model=f"{provider}-model",
                    api_key_env=env_name,
                    reasoning_effort=effort,
                )
                with mock.patch.dict(os.environ, {env_name: "sk-fake"}):
                    model = build_model(profile)
                self.assertEqual(
                    model.parameters.reasoning_effort, effort
                )
                self.assertTrue(model.parameters.thinking_enable)

    def test_unknown_provider_is_not_silently_fallen_back(self) -> None:
        profile = ModelProfile(
            name="nope",
            provider="openai",
            model="some-model",
            api_key_env="MODEL1_NOPE_KEY",
        )
        with mock.patch.dict(
            os.environ, {"MODEL1_NOPE_KEY": "sk-fake"}
        ), self.assertRaises(NotImplementedError) as caught:
            build_model(profile)
        message = str(caught.exception)
        self.assertIn("openai", message)
        self.assertIn("deepseek", message)
        self.assertIn("Credential", message)
        self.assertIn("build_model", message)

    def test_missing_key_reports_env_name_and_setup_file_only(self) -> None:
        profile = ModelProfile(
            name="work",
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="MODEL1_ABSENT_KEY",
        )
        with mock.patch.dict(os.environ, {"MODEL1_ABSENT_KEY": ""}):
            with self.assertRaises(RuntimeError) as caught:
                build_model(profile)
        message = str(caught.exception)
        self.assertIn("MODEL1_ABSENT_KEY", message)
        self.assertIn(str(self.user_env), message)
        self.assertIn("never stored", message)

    def test_invalid_effort_on_direct_profile_is_rejected(self) -> None:
        profile = ModelProfile(
            name="ds",
            provider="deepseek",
            model="deepseek-chat",
            api_key_env="MODEL1_DS_KEY",
            reasoning_effort="medium",
        )
        with mock.patch.dict(
            os.environ, {"MODEL1_DS_KEY": "sk-fake"}
        ), self.assertRaises(ValueError) as caught:
            build_model(profile)
        self.assertIn("high, max", str(caught.exception))


class CliModelCommandTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self._enter()
        self._write(
            {
                "profiles": [
                    {
                        "name": "work",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "api_key_env": "MODEL1_WORK_KEY",
                    },
                    {
                        "name": "claude",
                        "provider": "anthropic",
                        "model": "claude-test",
                        "api_key_env": "MODEL1_CLAUDE_KEY",
                    },
                ],
                "active_profile": "work",
            }
        )

    def _capture(self, argv: list[str], env: dict | None = None) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch.object(
            model_config, "load_local_env", lambda *a, **k: None
        ), mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(
            buffer
        ):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_model_lists_profiles_marker_and_key_state(self) -> None:
        sentinel = "sk-model1-cli-secret"
        code, output = self._capture(
            ["model"], {"MODEL1_WORK_KEY": sentinel, "MODEL1_CLAUDE_KEY": ""}
        )
        self.assertEqual(code, 0)
        self.assertIn("work", output)
        self.assertIn("claude", output)
        self.assertIn("当前 profile: work", output)
        self.assertIn("* work · deepseek/deepseek-chat", output)
        self.assertIn("密钥 MODEL1_WORK_KEY 已设置", output)
        self.assertIn("密钥 MODEL1_CLAUDE_KEY 未设置", output)
        self.assertNotIn(sentinel, output)

    def test_model_use_persists_and_reports(self) -> None:
        code, output = self._capture(["model", "use", "claude"])
        self.assertEqual(code, 0)
        self.assertIn("已切换 active_profile: claude", output)
        self.assertIn("anthropic/claude-test", output)
        self.assertEqual(load_config().profile, "claude")
        payload = json.loads(self.user_config.read_text(encoding="utf-8"))
        self.assertEqual(payload["active_profile"], "claude")

    def test_model_use_unknown_lists_available_names(self) -> None:
        code, output = self._capture(["model", "use", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("missing", output)
        self.assertIn("work, claude", output)
        self.assertFalse(self.user_config.exists())

    def test_check_lines_include_profiles_and_current_marker(self) -> None:
        sentinel = "sk-model1-check-secret"
        with mock.patch.dict(
            os.environ, {"MODEL1_WORK_KEY": sentinel, "MODEL1_CLAUDE_KEY": ""}
        ):
            lines = "\n".join(cli._model_config_lines())
        self.assertIn("当前 profile: work", lines)
        self.assertIn("* work · deepseek/deepseek-chat", lines)
        self.assertIn("claude · anthropic/claude-test", lines)
        self.assertIn("密钥 MODEL1_CLAUDE_KEY 未设置", lines)
        self.assertNotIn(sentinel, lines)


if __name__ == "__main__":
    unittest.main()
