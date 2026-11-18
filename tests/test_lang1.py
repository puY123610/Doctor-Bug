# -*- coding: utf-8 -*-
"""LANG1: jest/vitest + go parsers, JS/Go judging surface, inventory."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptor.evidence import module_from_node_id
from acceptor.frozen import freeze, judging_surface
from acceptor.parsers import get_parser

JEST_OUTPUT = """FAIL src/cart.test.ts
  ● Cart › adds item to cart
  ● Cart › applies discount
PASS src/util.test.ts
Test Suites: 1 failed, 1 passed, 2 total
Tests:       2 failed, 3 passed, 5 total
"""

VITEST_OUTPUT = """ ❯ src/cart.test.ts (3 tests | 1 failed) 12ms
   × adds item to cart 4ms
 ✓ src/util.test.ts (2 tests) 3ms
 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 2 passed (3)
"""

GO_OUTPUT = """=== RUN   TestAdd
--- PASS: TestAdd (0.00s)
=== RUN   TestSub
--- FAIL: TestSub (0.00s)
FAIL
FAIL\tcalc\t0.012s
ok  \tutil\t0.004s
"""


class JestFamilyParserTests(unittest.TestCase):
    def test_jest_output(self) -> None:
        result = get_parser("jest")(JEST_OUTPUT, 1)
        self.assertTrue(result.executed)
        self.assertEqual(result.passed_count, 3)
        self.assertEqual(result.failed_count, 2)
        self.assertEqual(
            result.failed_tests,
            [
                "src/cart.test.ts::Cart › adds item to cart",
                "src/cart.test.ts::Cart › applies discount",
            ],
        )

    def test_vitest_output(self) -> None:
        result = get_parser("vitest")(VITEST_OUTPUT, 1)
        self.assertTrue(result.executed)
        self.assertEqual(result.passed_count, 2)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(
            result.failed_tests,
            ["src/cart.test.ts::adds item to cart"],
        )

    def test_garbage_output_not_executed(self) -> None:
        result = get_parser("jest")("npm error something\n", 1)
        self.assertFalse(result.executed)


VITEST_FAILED_SECTION = """ ❯ src/differenceInSeconds/test.ts (8 tests | 1 failed) 9ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/differenceInSeconds/test.ts > differenceInSeconds > edge cases > does not return -0
 FAIL  src/_lib/getRoundingMethod/test.ts > getRoundingMethod > prevents negative zero

 Test Files  2 failed | 1 passed (3)
      Tests  2 failed | 22 passed (24)
"""


class VitestFailedSectionTests(unittest.TestCase):
    """LANG1-fix1 回归：真实 vitest 的失败在汇总区是
    ``FAIL <文件> > <套件> > <用例>`` 行，不是逐文件 × 行。"""

    def test_failed_section_nodes(self) -> None:
        result = get_parser("vitest")(VITEST_FAILED_SECTION, 1)
        self.assertTrue(result.executed)
        self.assertEqual(result.failed_count, 2)
        self.assertEqual(result.passed_count, 22)
        self.assertEqual(
            result.failed_tests,
            [
                "src/differenceInSeconds/test.ts::differenceInSeconds"
                " > edge cases > does not return -0",
                "src/_lib/getRoundingMethod/test.ts::"
                "getRoundingMethod > prevents negative zero",
            ],
        )


class VitestBrowserModeTests(unittest.TestCase):
    """vitest 浏览器模式：文件头与 FAIL 行多一个浏览器名 token。

    E2E 案例 3（gitea）实测输出形状：项目/浏览器名夹在标记与
    文件路径之间，旧正则匹配不上、失败节点全丢。
    """

    SAMPLE = (
        " RUN  v5.1.0 /repo\n"
        "\n"
        " ❯  chromium  web_src/js/features/comp/EditorMarkdown.test.ts"
        " (3 tests | 1 failed) 48ms\n"
        "   ✓ textareaSplitLines 2ms\n"
        "   × EditorMarkdown 45ms\n"
        " ❯  chromium  web_src/js/markup/tasklist.test.ts"
        " (1 test | 1 failed) 40ms\n"
        "   × toggleTasklistCheckbox 40ms\n"
        "\n"
        "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯\n"
        "\n"
        " FAIL   chromium  web_src/js/markup/tasklist.test.ts"
        " > toggleTasklistCheckbox\n"
        "AssertionError: expected null to deeply equal '- [ ] task'\n"
        "\n"
        " FAIL   chromium  web_src/js/features/comp/EditorMarkdown.test.ts"
        " > EditorMarkdown\n"
        "AssertionError: expected '1. [X] foo' to deeply equal '1. [ ] foo'\n"
        "\n"
        " Test Files  2 failed (2)\n"
        "      Tests  2 failed | 2 passed (4)\n"
    )

    def test_browser_mode_failures_extracted(self) -> None:
        result = get_parser("vitest")(self.SAMPLE, 1)
        self.assertTrue(result.executed)
        self.assertEqual(result.failed_count, 2)
        self.assertEqual(result.passed_count, 2)
        self.assertEqual(
            sorted(result.failed_tests),
            [
                "web_src/js/features/comp/EditorMarkdown.test.ts"
                "::EditorMarkdown",
                "web_src/js/markup/tasklist.test.ts"
                "::toggleTasklistCheckbox",
            ],
        )


class GoParserTests(unittest.TestCase):
    def test_go_verbose_output(self) -> None:
        result = get_parser("go")(GO_OUTPUT, 1)
        self.assertTrue(result.executed)
        self.assertEqual(result.passed_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.failed_tests, ["calc::TestSub"])

    def test_go_build_failure(self) -> None:
        output = "# calc\n./calc.go:3:1: syntax error\nFAIL\tcalc [build failed]\n"
        result = get_parser("go")(output, 1)
        self.assertTrue(result.executed)
        self.assertIn("calc::(build failed)", result.failed_tests)


class EvidenceModuleTests(unittest.TestCase):
    def test_js_test_file_maps_to_module(self) -> None:
        self.assertEqual(
            module_from_node_id("src/cart.test.ts::adds item"), "cart"
        )
        self.assertEqual(
            module_from_node_id("src/util.spec.ts::works"), "util"
        )

    def test_python_mapping_unchanged(self) -> None:
        self.assertEqual(
            module_from_node_id("tests/test_billing.py::test_x"),
            "billing",
        )


class JudgingSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lang1-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def test_js_surface(self) -> None:
        repo = self.root / "js"
        (repo / "src").mkdir(parents=True)
        (repo / "package.json").write_text("{}", encoding="utf-8")
        (repo / "src" / "cart.ts").write_text("", encoding="utf-8")
        (repo / "src" / "cart.test.ts").write_text(
            "", encoding="utf-8"
        )
        names = {
            p.relative_to(repo).as_posix()
            for p in judging_surface(repo)
        }
        self.assertEqual(names, {"package.json", "src/cart.test.ts"})

    def test_js_surface_bare_test_files(self) -> None:
        repo = self.root / "js3"
        (repo / "src" / "add").mkdir(parents=True)
        (repo / "package.json").write_text("{}", encoding="utf-8")
        (repo / "src" / "add" / "index.ts").write_text(
            "", encoding="utf-8"
        )
        (repo / "src" / "add" / "test.ts").write_text(
            "", encoding="utf-8"
        )
        names = {
            p.relative_to(repo).as_posix()
            for p in judging_surface(repo)
        }
        self.assertEqual(
            names, {"package.json", "src/add/test.ts"}
        )

    def test_go_surface(self) -> None:
        repo = self.root / "go"
        repo.mkdir(parents=True)
        (repo / "go.mod").write_text("module calc\n", encoding="utf-8")
        (repo / "go.sum").write_text("calc h1:x=\n", encoding="utf-8")
        (repo / "calc.go").write_text("package calc\n", encoding="utf-8")
        (repo / "calc_test.go").write_text(
            "package calc\n", encoding="utf-8"
        )
        names = {
            p.relative_to(repo).as_posix()
            for p in judging_surface(repo)
        }
        self.assertEqual(names, {"go.mod", "go.sum", "calc_test.go"})

    def test_freeze_js_repo_writes_manifest(self) -> None:
        repo = self.root / "js2"
        (repo / "src").mkdir(parents=True)
        (repo / "package.json").write_text("{}", encoding="utf-8")
        (repo / "src" / "cart.test.ts").write_text(
            "// tests\n", encoding="utf-8"
        )
        manifest = freeze(
            repo, self.root / "vault", package="src",
            check_command=["npx", "jest", "--ci"], parser="jest",
        )
        self.assertEqual(manifest["parser"], "jest")
        self.assertIn("src/cart.test.ts", manifest["files"])


class JsInventoryTests(unittest.TestCase):
    def test_inventory_finds_functions_and_classes(self) -> None:
        from core.js_inventory import scan_js_ts_inventory

        with tempfile.TemporaryDirectory(prefix="lang1-inv-") as tmp:
            repo = Path(tmp)
            (repo / "cart.ts").write_text(
                "/** Cart math helpers. */\n"
                "export function addItem(total: number): number {\n"
                "  return total + 1;\n"
                "}\n"
                "export class Cart {\n"
                "  checkout(): void {}\n"
                "}\n",
                encoding="utf-8",
            )
            result = scan_js_ts_inventory(repo)
            self.assertIn("cart", result["modules"])
            blurb = result["modules"]["cart"]["blurb"]
            self.assertIn("addItem", blurb)
            self.assertIn("Cart", blurb)
            self.assertTrue(
                result["notice"] is None
                or isinstance(result["notice"], str)
            )


if __name__ == "__main__":
    unittest.main()
