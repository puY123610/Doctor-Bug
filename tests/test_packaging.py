"""PACK2: npm packaging, per-project state dirs and BYOK configuration.

The tests avoid the network and never read a real key: every config/env file
used here is created inside a temporary HOME.  npm/Node checks are skipped when
the Node binary is not on the unittest PATH (the login-shell install path is
covered by the PACK2 completion gate / report instead).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import cli
from core import model_config
from core.autoprofile import state_root_for

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
NPM_DIR = PLATFORM_ROOT / "npm"


def _find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if Path(candidate).is_file():
            return candidate
    return None


def _capture_main(argv: list[str], cwd: Path | None = None) -> tuple[int, str]:
    buffer = io.StringIO()
    previous = Path.cwd()
    try:
        if cwd is not None:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
    finally:
        os.chdir(previous)
    return code, buffer.getvalue()


class PackagingManifestTest(unittest.TestCase):
    def test_package_json_names_bin_and_ships_platform(self) -> None:
        package = json.loads((NPM_DIR / "package.json").read_text("utf-8"))

        self.assertEqual(package["name"], "drbug")
        self.assertEqual(package["bin"]["drbug"], "bin/drbug.js")
        self.assertIn("platform", package["files"])
        self.assertTrue((NPM_DIR / "bin" / "drbug.js").is_file())
        self.assertTrue((NPM_DIR / "prepack.js").is_file())

    def test_prepack_dry_run_lists_runtime_assets_and_secrets_are_excluded(
        self,
    ) -> None:
        node = _find_node()
        if node is None:
            self.skipTest("node 不在 unittest PATH（见报告中的登录 shell 验证）")

        completed = subprocess.run(
            [node, str(NPM_DIR / "prepack.js"), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads(completed.stdout)
        for entry in (
            "core",
            "acceptor",
            "kb",
            "agentscope_ext",
            "tools",
            "requirements.txt",
            "pyproject.toml",
            "model_config.example.json",
        ):
            self.assertIn(entry, manifest["entries"])
        for gone in ("demo_repo", "demo_repo2", "profiles"):
            self.assertNotIn(gone, manifest["entries"])
        self.assertIn(".env.local", manifest["excludes"])
        self.assertIn("model_config.json", manifest["excludes"])

    def test_launcher_passes_cwd_and_exit_code_in_source(self) -> None:
        script = (NPM_DIR / "bin" / "drbug.js").read_text("utf-8")

        self.assertIn("cwd: process.cwd()", script)
        self.assertIn("process.exit(child.status", script)
        self.assertIn("~/.drbug", script)
        self.assertIn("sha256", script)
        self.assertIn("python-build-standalone", script)


class StateRootTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pack2-state-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_external_project_uses_dot_bugdoc(self) -> None:
        project = (self.tmp / "project").resolve()
        project.mkdir()

        self.assertEqual(state_root_for(project), project / ".drbug")
        self.assertEqual(
            cli._profiles_root_for(project), project / ".drbug" / "profiles"
        )
        self.assertEqual(
            cli._runs_root_for(project), project / ".drbug" / "runs"
        )

    def test_builtin_demo_keeps_platform_state_root(self) -> None:
        self.assertEqual(state_root_for(DEMO_REPO), PLATFORM_ROOT)
        self.assertEqual(cli._profiles_root_for(DEMO_REPO), cli.PROFILES_ROOT)
        self.assertEqual(cli._runs_root_for(DEMO_REPO), cli.RUNS_DIR)

    def test_explicit_patch_points_still_win(self) -> None:
        project = self.tmp / "project"
        project.mkdir()
        with mock.patch.object(cli, "PROFILES_ROOT", self.tmp / "profiles"):
            self.assertEqual(cli._profiles_root_for(project), self.tmp / "profiles")
        with mock.patch.object(cli, "RUNS_DIR", self.tmp / "runs"):
            self.assertEqual(cli._runs_root_for(project), self.tmp / "runs")


class _FixtureRepo:
    """Tiny broken pytest repository used by the path-default tests."""

    @staticmethod
    def create(root: Path) -> Path:
        repo = root / "calc_project"
        (repo / "calc").mkdir(parents=True)
        (repo / "calc" / "__init__.py").write_text(
            "def double(value):\n    return value * 2 + 1\n",
            encoding="utf-8",
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_calc.py").write_text(
            "from calc import double\n\n\n"
            "def test_double():\n"
            "    assert double(2) == 4\n",
            encoding="utf-8",
        )
        return repo


class DefaultWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pack2-cwd-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_profile_defaults_to_cwd_and_writes_project_state(self) -> None:
        repo = _FixtureRepo.create(self.tmp)

        code, output = _capture_main(["profile"], cwd=repo)

        self.assertEqual(code, 0, output)
        profile_dir = repo / ".drbug" / "profiles" / "calc_project"
        self.assertTrue((profile_dir / "dispatch_table.json").is_file())
        self.assertTrue((profile_dir / "vault" / "manifest.json").is_file())
        self.assertIn("自动建档", output)

    def test_run_defaults_to_cwd_and_uses_project_runs_root(self) -> None:
        from core.runner import RunRecord

        repo = _FixtureRepo.create(self.tmp)
        out = self.tmp / "record.json"
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")

        profile_code, profile_output = _capture_main(["profile"], cwd=repo)
        self.assertEqual(profile_code, 0, profile_output)
        with mock.patch.dict(
            os.environ, {"DRBUG_HOME": str(self.tmp / "drbug-home")}
        ), mock.patch("core.runner.run_arm", return_value=record) as run_mock:
            code, output = _capture_main(
                [
                    "run",
                    "--arm",
                    "C",
                    "--profile",
                    "calc_project",
                    "--out",
                    str(out),
                ],
                cwd=repo,
            )

        self.assertEqual(code, 0, output)
        kwargs = run_mock.call_args.kwargs
        self.assertEqual(kwargs["workspace_src"], repo.resolve())
        self.assertEqual(
            kwargs["work_root"], self.tmp / "drbug-home" / "work"
        )
        self.assertFalse(
            str(kwargs["work_root"]).startswith(str(repo.resolve()))
        )
        self.assertTrue(out.is_file())

    def test_accept_defaults_to_cwd(self) -> None:
        repo = _FixtureRepo.create(self.tmp)
        profile_code, profile_output = _capture_main(["profile"], cwd=repo)
        self.assertEqual(profile_code, 0, profile_output)
        fake_verdict = SimpleNamespace(
            status="PASS",
            passed_count=1,
            failed_count=0,
            tampered_files=[],
        )
        with mock.patch(
            "acceptor.acceptor.FrozenAcceptor.check", return_value=fake_verdict
        ) as check_mock:
            code, output = _capture_main(["accept"], cwd=repo)

        self.assertEqual(code, 0, output)
        self.assertEqual(check_mock.call_args.args[0], repo.resolve())

    def test_chat_defaults_to_cwd(self) -> None:
        repo = _FixtureRepo.create(self.tmp)
        with mock.patch("core.tui.run", return_value=0) as run_mock, mock.patch(
            "core.cli._stdout_is_tty", return_value=True
        ):
            code, output = _capture_main([], cwd=repo)

        self.assertEqual(code, 0, output)
        self.assertEqual(
            run_mock.call_args.kwargs["workspace_root"], repo.resolve()
        )

    def test_chat_profile_root_is_redirected_to_project_state(self) -> None:
        from core.autoprofile import build_profile

        repo = _FixtureRepo.create(self.tmp).resolve()
        with mock.patch.dict(
            os.environ, {"DRBUG_CHAT_WORKSPACE": str(repo)}
        ):
            result = build_profile(
                repo, out_root=repo / "profiles", name="chat_profile"
            )

        self.assertEqual(
            result.profile_dir,
            repo / ".drbug" / "profiles" / "chat_profile",
        )
        self.assertFalse((repo / "profiles").exists())


class ConfigPrecedenceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pack2-config-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.platform_config = self.tmp / "platform_model_config.json"
        self.platform_env = self.tmp / "platform.env"
        env_patcher = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_CONFIG_HOME": ""},
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for name in ("DEEPSEEK_API_KEY", "PLATFORM_MODEL", "DRBUG_MAX_ROUNDS"):
            os.environ.pop(name, None)
        patchers = [
            mock.patch.object(
                model_config, "DEFAULT_CONFIG_PATH", self.platform_config
            ),
            mock.patch.object(model_config, "DEFAULT_ENV_PATH", self.platform_env),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _user_config(self, payload: dict) -> Path:
        target = model_config.user_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def test_user_config_overrides_platform_config(self) -> None:
        self.platform_config.write_text(
            json.dumps(
                {"provider": "deepseek", "model": "platform-model"}
            ),
            encoding="utf-8",
        )
        self._user_config({"provider": "deepseek", "model": "user-model"})

        cfg = model_config.load_config()

        self.assertEqual(cfg.model, "user-model")
        self.assertIn("user-model", cfg.model)
        self.assertIn(str(self.platform_config), cfg.source)
        self.assertIn(str(model_config.user_config_path()), cfg.source)

    def test_user_env_wins_over_legacy_platform_env(self) -> None:
        user_env = model_config.user_env_path()
        user_env.parent.mkdir(parents=True, exist_ok=True)
        user_env.write_text("DEEPSEEK_API_KEY=from-user\n", encoding="utf-8")
        self.platform_env.write_text(
            "DEEPSEEK_API_KEY=from-platform\n", encoding="utf-8"
        )

        model_config.load_local_env()

        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "from-user")

    def test_real_environment_beats_both_files(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "from-process"
        user_env = model_config.user_env_path()
        user_env.parent.mkdir(parents=True, exist_ok=True)
        user_env.write_text("DEEPSEEK_API_KEY=from-user\n", encoding="utf-8")

        model_config.load_local_env()

        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "from-process")

    def test_check_reports_sources_but_never_the_secret(self) -> None:
        sentinel = "sk-pack2-check-secret"
        self._user_config({"provider": "deepseek", "model": "user-model"})
        os.environ["DEEPSEEK_API_KEY"] = sentinel

        lines = cli._model_config_lines()
        rendered = "\n".join(lines)

        self.assertIn("配置来源", rendered)
        self.assertIn("用户配置", rendered)
        self.assertIn("已设置", rendered)
        self.assertNotIn(sentinel, rendered)

    def test_max_rounds_from_config_then_env(self) -> None:
        self._user_config({"model": "user-model", "max_rounds": 4})

        self.assertEqual(model_config.load_max_rounds(), 4)
        os.environ["DRBUG_MAX_ROUNDS"] = "2"
        self.assertEqual(model_config.load_max_rounds(), 2)
        os.environ["DRBUG_MAX_ROUNDS"] = "many"
        with self.assertRaises(ValueError):
            model_config.load_max_rounds()


class FirstRunGuideTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pack2-guide-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        patchers = [
            mock.patch.dict(
                os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": ""}
            ),
            mock.patch.object(
                model_config, "DEFAULT_CONFIG_PATH", self.tmp / "missing.json"
            ),
            mock.patch.object(
                model_config, "DEFAULT_ENV_PATH", self.tmp / "missing.env"
            ),
            mock.patch.object(model_config, "load_local_env"),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("DEEPSEEK_API_KEY", "PLATFORM_MODEL", "DRBUG_MAX_ROUNDS"):
            os.environ.pop(name, None)

    def test_bare_drbug_prints_one_step_byok_guide_without_secret(self) -> None:
        code, output = _capture_main([])

        self.assertEqual(code, 0)
        self.assertIn("首次使用需要配置模型", output)
        self.assertIn(str(self.home / ".config" / "drbug"), output)
        self.assertIn("DEEPSEEK_API_KEY=<你的密钥>", output)
        self.assertIn("drbug check", output)
        self.assertIn("配置来源", output)
        self.assertNotIn("sk-", output)


class TokenMeterTest(unittest.TestCase):
    # CLEAN1：老计数器已删，计量断言改读统一累计器（core.usage）。
    def setUp(self) -> None:
        from core import usage as _usage

        self.usage = _usage
        self.acc = _usage.reset_current()
        self.addCleanup(_usage.reset_current)

    def test_non_streaming_response_usage_is_accumulated(self) -> None:
        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=4,
                cache_input_tokens=2,
                cache_creation_input_tokens=1,
            )
        )

        async def call(*_args, **_kwargs):
            return response

        metered = model_config._MeteredChatModel(call)
        asyncio.run(metered([]))

        usage = self.usage.run_usage_dict(self.acc)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 4)
        self.assertIn("输入 10", self.usage.format_run_usage_line(usage))

    def test_streaming_chunks_are_accumulated(self) -> None:
        async def stream():
            yield SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=3,
                    output_tokens=1,
                    cache_input_tokens=0,
                    cache_creation_input_tokens=0,
                )
            )
            yield SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=7,
                    output_tokens=2,
                    cache_input_tokens=1,
                    cache_creation_input_tokens=0,
                )
            )

        async def call(*_args, **_kwargs):
            return stream()

        async def consume():
            metered = model_config._MeteredChatModel(call)
            chunks = []
            async for chunk in await metered([]):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(consume())

        self.assertEqual(len(chunks), 2)
        # 老计数器把每个 chunk 的 usage 都累加（calls=2、输入 3+7）；累计器
        # 按「一次调用一条、流式取最后一个 chunk 的累计 usage」记录。
        usage = self.usage.run_usage_dict(self.acc)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(usage["output_tokens"], 2)

    def test_wrapped_model_still_runs_inside_agentscope_actor(self) -> None:
        from agentscope_ext.fake_model import FakeChatModel
        from core.agentscope_actors import agentscope_act

        temporary = tempfile.TemporaryDirectory(prefix="pack2-metered-agent-")
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name) / "demo_repo"
        shutil.copytree(
            DEMO_REPO,
            workspace,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        wrapped = model_config._MeteredChatModel(
            FakeChatModel(fallback_reply="no change needed")
        )

        import json as _json

        from core.agentscope_actors import roster_from_table

        roster = roster_from_table(
            _json.loads(
                (PLATFORM_ROOT / "kb" / "dispatch_table.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        result = agentscope_act(
            "billing-specialist",
            workspace,
            ["tests/test_billing.py::test_tax_on_discounted_total"],
            [],
            model=wrapped,
            timeout_s=60,
            roster=roster,
        )

        self.assertFalse(result.applied)
        self.assertIn("completed", result.note)
        usage = self.usage.run_usage_dict(self.acc)
        self.assertGreater(usage["calls"], 0)
        self.assertGreater(usage["input_tokens"], 0)

    def test_metering_is_opt_in_for_build_model(self) -> None:
        class FakeCredential:
            def __init__(self, api_key):
                self.api_key = api_key

        class FakeModel:
            def __init__(self, credential, model):
                self.credential = credential
                self.model = model

        cfg = model_config.ModelConfig(model="deepseek-chat")
        with mock.patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": "sk-test-not-real"}
        ), mock.patch.object(
            model_config, "DeepSeekCredential", FakeCredential
        ), mock.patch.object(
            model_config, "DeepSeekChatModel", FakeModel
        ):
            os.environ.pop("DRBUG_TOKEN_METER", None)
            plain = model_config.build_model(cfg)
            self.assertIsInstance(plain, FakeModel)

            os.environ["DRBUG_TOKEN_METER"] = "1"
            try:
                metered = model_config.build_model(cfg)
            finally:
                os.environ.pop("DRBUG_TOKEN_METER", None)

        self.assertIsInstance(metered, model_config._MeteredChatModel)
        self.assertIsInstance(metered._drbug_inner, FakeModel)


if __name__ == "__main__":
    unittest.main()
