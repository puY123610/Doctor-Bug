"""模型目录与选择器：目录每档必须真能构建，Enter 把当前档位落盘。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from core import model_catalog, model_config
from core.model_config import ModelProfile
from core.tui import CifixChatApp, ModelPicker
from textual.widgets import OptionList

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


class CatalogDataTest(unittest.TestCase):
    def test_catalog_covers_exactly_the_wired_providers(self) -> None:
        self.assertEqual(
            set(model_catalog.PROVIDER_MODELS),
            set(model_config.SUPPORTED_PROVIDERS),
        )

    def test_every_offered_effort_passes_framework_validation(self) -> None:
        for provider, rows in model_catalog.PROVIDER_MODELS.items():
            for row in rows:
                for effort in row.efforts:
                    self.assertEqual(
                        model_config._validate_reasoning_effort(
                            provider, effort, "catalog test"
                        ),
                        effort,
                    )

    def test_deepseek_flash_row(self) -> None:
        row = model_catalog.catalog_model("deepseek", "deepseek-flash")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(row.vision)
        self.assertTrue(row.thinking)
        # AgentScope DeepSeek Parameters 只收 high/max，目录不许列 low。
        self.assertEqual(row.efforts, ("high", "max"))

    def test_lookup_miss_returns_none(self) -> None:
        self.assertIsNone(model_catalog.catalog_model("deepseek", "nope"))
        self.assertEqual(model_catalog.models_for("nope"), ())


class UpdateActiveModelTest(unittest.TestCase):
    def _write(self, data: dict) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "model_config.json"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        return tmp

    def test_flat_config_round_trip_and_clear(self) -> None:
        path = self._write(
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "active_profile": "default",
                "active": "keep-me",
            }
        )
        profile = model_config.update_active_model(
            "deepseek", "deepseek-v4-pro", "max", config_path=path
        )
        self.assertEqual(profile.model, "deepseek-v4-pro")
        self.assertEqual(profile.reasoning_effort, "max")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertEqual(data["reasoning_effort"], "max")
        self.assertEqual(data["active"], "keep-me")

        profile = model_config.update_active_model(
            "deepseek", "deepseek-flash", None, config_path=path
        )
        self.assertIsNone(profile.reasoning_effort)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("reasoning_effort", data)

    def test_profiles_array_entry_is_synced(self) -> None:
        path = self._write(
            {
                "active_profile": "default",
                "profiles": [
                    {
                        "name": "default",
                        "provider": "deepseek",
                        "model": "deepseek-flash",
                        "api_key_env": "DEEPSEEK_API_KEY",
                    }
                ],
            }
        )
        model_config.update_active_model(
            "deepseek", "deepseek-v4-flash", "high", config_path=path
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data["profiles"][0]
        self.assertEqual(entry["model"], "deepseek-v4-flash")
        self.assertEqual(entry["reasoning_effort"], "high")

    def test_unsupported_effort_rejected(self) -> None:
        path = self._write({"provider": "deepseek", "model": "deepseek-flash"})
        with self.assertRaises(ValueError):
            model_config.update_active_model(
                "deepseek", "deepseek-flash", "low", config_path=path
            )


class ModelPickerFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_pick_model_enter_persists_cycled_effort(self) -> None:
        """←/→ 把档位轮到 max，Enter 落盘该型号 + 该档，不再进第二段。"""
        config_path = Path(tempfile.mkdtemp()) / "model_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "provider": "deepseek",
                    "model": "deepseek-flash",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "active_profile": "default",
                }
            ),
            encoding="utf-8",
        )
        catalog = mock.Mock()
        catalog.active = "default"
        catalog.profiles = [
            ModelProfile(
                name="default",
                provider="deepseek",
                model="deepseek-flash",
                api_key_env="DEEPSEEK_API_KEY",
            )
        ]
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=PLATFORM_ROOT,
        )
        selected: list[str] = []
        with mock.patch.object(
            CifixChatApp, "_model_selected", lambda self, name: selected.append(name)
        ), mock.patch.object(
            model_config, "DEFAULT_CONFIG_PATH", config_path
        ), mock.patch.object(
            model_config, "user_config_path", return_value=config_path
        ):
            async with app.run_test(size=(110, 34)) as pilot:
                picker = ModelPicker(app, catalog)
                app.push_screen(picker)
                await pilot.pause()
                option_list = picker.query_one("#model-list", OptionList)
                ids = [
                    option_list.get_option_at_index(i).id
                    for i in range(option_list.option_count)
                ]
                self.assertIn("model:deepseek-v4-pro", ids)
                self.assertIn("__setup__", ids)
                self.assertNotIn("effort:max", ids)
                option_list.highlighted = ids.index("model:deepseek-v4-pro")
                await pilot.pause()
                await pilot.press("right")
                await pilot.press("right")
                await pilot.pause()
                self.assertIn(
                    "思考等级：max",
                    str(picker.query_one("#model-effort-status").content),
                )
                await pilot.press("enter")
                await pilot.pause()
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "deepseek-v4-pro")
        self.assertEqual(data["reasoning_effort"], "max")
        self.assertEqual(selected, ["default"])


if __name__ == "__main__":
    unittest.main()
