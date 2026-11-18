# -*- coding: utf-8 -*-
"""F2 多语言建档：语言识别、派单表/金库形状、build_profile 钩子。

证据运行默认打桩（离线）；Go 真跑一条守卫测试（装了 go 才跑）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from acceptor.acceptor import FrozenAcceptor
from core import autoprofile_lang
from core.autoprofile import build_profile
from core.autoprofile_lang import (
    build_profile_lang,
    detect_language,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _go_repo(root: Path) -> Path:
    _write(root, "go.mod", "module example.com/shop\n\ngo 1.21\n")
    _write(
        root,
        "tax/tax.go",
        'package tax\n\n// Rate for a region.\n'
        "func Rate(region string) float64 { return 0 }\n",
    )
    _write(
        root,
        "tax/tax_test.go",
        'package tax\n\nimport "testing"\n\n'
        "func TestRate(t *testing.T) {}\n",
    )
    return root


def _js_repo(root: Path) -> Path:
    _write(
        root,
        "package.json",
        '{"name": "shopjs", "scripts": {"test": "vitest run"},'
        ' "devDependencies": {"vitest": "0.34.6"}}\n',
    )
    _write(
        root,
        "src/tax.js",
        "// Tax rate for a region.\nexport function taxRate(r) { return 0; }\n",
    )
    _write(
        root,
        "src/tax.test.js",
        'import { it } from "vitest";\nit("rate", () => {});\n',
    )
    return root


def _fake_evidence(failed_nodes):
    def _run(repo, command, parser_name, *, language, ambiguities):
        return {
            "test_total": 2,
            "passed_count": 2 - len(failed_nodes),
            "failed_count": len(failed_nodes),
            "failed_nodes": sorted(failed_nodes),
            "returncode": 1 if failed_nodes else 0,
        }

    return _run


class DetectLanguageTest(unittest.TestCase):
    def test_go_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _go_repo(Path(tmp).resolve() / "r")
            self.assertEqual(detect_language(repo), "go")

    def test_js_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _js_repo(Path(tmp).resolve() / "r")
            self.assertEqual(detect_language(repo), "javascript")

    def test_python_only_repo_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "r"
            _write(repo, "tests/test_x.py", "def test_x():\n    pass\n")
            self.assertIsNone(detect_language(repo))

    def test_mixed_repo_picks_more_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _go_repo(Path(tmp).resolve() / "r")
            _write(repo, "src/a.test.js", "it('a', () => {});\n")
            _write(repo, "src/b.test.js", "it('b', () => {});\n")
            self.assertEqual(detect_language(repo), "javascript")


class BuildProfileLangTest(unittest.TestCase):
    def setUp(self):
        self._orig = autoprofile_lang._run_on_copy

    def tearDown(self):
        autoprofile_lang._run_on_copy = self._orig

    def test_go_profile_shape(self):
        autoprofile_lang._run_on_copy = _fake_evidence(
            ["example.com/shop/tax::TestRate"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = _go_repo(base / "r")
            profile_dir = base / "profiles" / "r"
            result = build_profile_lang(
                repo,
                profile_dir=profile_dir,
                profile_name="r",
                language="go",
            )
            self.assertEqual(result.metadata["language"], "go")
            self.assertEqual(result.failed_count, 1)
            table = result.dispatch_table
            self.assertEqual(
                table["module_defaults"], {"tax": ["tax-specialist"]}
            )
            self.assertEqual(table["fallback"], "generalist")
            self.assertEqual(table["node_overrides"], {})
            manifest = json.loads(
                (profile_dir / "vault" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["parser"], "go")
            self.assertEqual(manifest["kind"], "tests")
            self.assertEqual(
                manifest["check_command"],
                ["go", "test", "-count=1", "-v", "./..."],
            )
            self.assertIn("tax/tax_test.go", manifest["files"])
            self.assertIn("go.mod", manifest["files"])
            agents = profile_dir / "agents"
            self.assertTrue((agents / "tax-specialist").is_dir())
            self.assertTrue((agents / "generalist").is_dir())

    def test_js_profile_shape(self):
        autoprofile_lang._run_on_copy = _fake_evidence(
            ["src/tax.test.js::rate"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = _js_repo(base / "r")
            profile_dir = base / "profiles" / "r"
            result = build_profile_lang(
                repo,
                profile_dir=profile_dir,
                profile_name="r",
                language="javascript",
            )
            self.assertEqual(result.metadata["language"], "javascript")
            table = result.dispatch_table
            self.assertEqual(
                table["module_defaults"], {"tax": ["tax-specialist"]}
            )
            manifest = json.loads(
                (profile_dir / "vault" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["parser"], "vitest")
            self.assertEqual(
                manifest["check_command"], ["npx", "vitest", "run"]
            )
            self.assertIn("package.json", manifest["files"])
            self.assertIn("src/tax.test.js", manifest["files"])
            inventory = result.metadata["inventory"]
            self.assertEqual(inventory["tax"]["path"], "src/tax.js")

    def test_hook_through_build_profile(self):
        autoprofile_lang._run_on_copy = _fake_evidence([])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = _go_repo(base / "r")
            result = build_profile(repo, out_root=base / "profiles")
            self.assertEqual(result.metadata["language"], "go")
            self.assertTrue(result.profile_json_path.is_file())


class MultiRootRosterTest(unittest.TestCase):
    def setUp(self):
        self._orig = autoprofile_lang._run_on_copy

    def tearDown(self):
        autoprofile_lang._run_on_copy = self._orig

    def test_two_package_go_repo_gets_root_roster(self):
        from core.agentscope_actors import roster_from_table

        autoprofile_lang._run_on_copy = _fake_evidence(
            ["example.com/shop/tax::TestRate"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = _go_repo(base / "r")
            _write(
                repo,
                "pricing/pricing.go",
                'package pricing\n\n// Total with tax.\n'
                "func Total() float64 { return 0 }\n",
            )
            _write(
                repo,
                "pricing/pricing_test.go",
                'package pricing\n\nimport "testing"\n\n'
                "func TestTotal(t *testing.T) {}\n",
            )
            profile_dir = base / "profiles" / "r"
            result = build_profile_lang(
                repo,
                profile_dir=profile_dir,
                profile_name="r",
                language="go",
            )
            table = result.dispatch_table
            self.assertEqual(table["roster"]["package"], ".")
            roster = roster_from_table(table)
            self.assertEqual(roster.package, ".")
            generalist_files = set(roster.specialists["generalist"].files)
            self.assertIn("tax/tax.go", generalist_files)
            self.assertIn("pricing/pricing.go", generalist_files)
            self.assertEqual(
                table["module_defaults"]["pricing"],
                ["pricing-specialist"],
            )


@unittest.skipUnless(shutil.which("go"), "go toolchain not available")
class GoEvidenceIntegrationTest(unittest.TestCase):
    def test_real_go_evidence_and_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = _go_repo(base / "r")
            (repo / "tax" / "tax_test.go").write_text(
                'package tax\n\nimport "testing"\n\n'
                "func TestRate(t *testing.T) {\n"
                "\tif Rate(\"EU\") != 0.2 { t.Fatal(\"bad rate\") }\n}\n",
                encoding="utf-8",
            )
            result = build_profile(
                repo, out_root=base / "profiles"
            )
            self.assertEqual(result.metadata["language"], "go")
            self.assertEqual(result.failed_count, 1)
            self.assertEqual(
                result.metadata["failed_nodes"],
                ["example.com/shop/tax::TestRate"],
            )
            verdict = FrozenAcceptor(result.vault_dir).check(repo)
            self.assertEqual(verdict.status, "FAIL")
            self.assertEqual(verdict.failed_count, 1)


if __name__ == "__main__":
    unittest.main()
