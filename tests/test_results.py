"""Offline tests for result bundles, patch.diff, report.md and adoption."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import dispatcher
from core.cli import main as cli_main
from core.results import (
    build_result_bundle,
    describe_adoption,
    locate_bundle,
    record_adoption,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", "*.pyc", ".DS_Store"
)


def _capture_main(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(argv)
    return code, buffer.getvalue()


def _write_tree_pair(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    final = root / "final"
    (source / "pkg").mkdir(parents=True)
    (final / "pkg").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (final / "pkg" / "mod.py").write_text("value = 2\n", encoding="utf-8")
    (source / "pkg" / "old.py").write_text("old\n", encoding="utf-8")
    (final / "pkg" / "new.py").write_text("new\n", encoding="utf-8")
    (source / "keep.txt").write_text("same\n", encoding="utf-8")
    (final / "keep.txt").write_text("same\n", encoding="utf-8")

    (source / "assets").mkdir()
    (final / "assets").mkdir()
    (source / "assets" / "blob.bin").write_bytes(b"\x00old")
    (final / "assets" / "blob.bin").write_bytes(b"\x00new")

    (source / "__pycache__").mkdir()
    (final / "__pycache__").mkdir()
    (source / "__pycache__" / "noise.pyc").write_bytes(b"old")
    (final / "__pycache__" / "noise.pyc").write_bytes(b"new")
    (source / ".DS_Store").write_bytes(b"old")
    (final / ".DS_Store").write_bytes(b"new")
    return source, final


def _record() -> dict:
    return {
        "arm": "C",
        "kb_enabled": True,
        "outcome": "FAIL",
        "rounds": [
            {
                "round": 1,
                "status": "FAIL",
                "failed_tests": [
                    "tests/test_orders.py::test_checkout_total_with_coupon"
                ],
                "assignments": {
                    "billing-specialist": [
                        "tests/test_orders.py::test_checkout_total_with_coupon"
                    ],
                    "inventory-specialist": [
                        "tests/test_orders.py::test_stock_after_single_checkout"
                    ],
                },
                "fix_results": [
                    {
                        "actor_id": "billing-specialist",
                        "applied": True,
                        "note": "fixed billing",
                    }
                ],
            }
        ],
        "final_failed": [
            "tests/test_orders.py::test_checkout_total_with_coupon"
        ],
        "scope_denials": [
            {
                "round": 1,
                "actor_id": "generalist",
                "tool": "Edit",
                "path": "pkg/other.py",
                "reason": "outside roster files scope",
            }
        ],
    }


class BuildBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="result-bundle-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_bundle_contains_four_files_and_counts_skipped_entries(self) -> None:
        source, final = _write_tree_pair(self.tmp)
        record = _record()

        bundle = build_result_bundle(
            record,
            source,
            final,
            actor_backend="scripted",
            bundle_root=self.tmp / "bundles",
            run_id="run-1",
        )

        self.assertEqual(
            sorted(path.name for path in bundle.iterdir()),
            ["adoption.json", "patch.diff", "record.json", "report.md"],
        )
        self.assertEqual(
            json.loads((bundle / "record.json").read_text(encoding="utf-8")),
            record,
        )
        self.assertEqual(
            json.loads((bundle / "adoption.json").read_text(encoding="utf-8")),
            {"adopted": None, "decided_at": None, "note": ""},
        )

        patch = (bundle / "patch.diff").read_text(encoding="utf-8")
        self.assertIn("--- a/pkg/mod.py", patch)
        self.assertIn("+++ b/pkg/mod.py", patch)
        self.assertIn("+value = 2", patch)
        self.assertIn("--- a/pkg/old.py", patch)
        self.assertIn("+++ /dev/null", patch)
        self.assertIn("--- /dev/null", patch)
        self.assertIn("+++ b/pkg/new.py", patch)
        self.assertNotIn("blob.bin", patch)
        self.assertNotIn("__pycache__", patch)
        self.assertNotIn(".DS_Store", patch)

        report = (bundle / "report.md").read_text(encoding="utf-8")
        self.assertIn("# Doctor Bug 结果包", report)
        self.assertIn("终局: FAIL", report)
        self.assertIn("| 1 | FAIL |", report)
        self.assertIn("billing-specialist×1", report)
        self.assertIn("generalist", report)
        self.assertIn("outside roster files scope", report)
        self.assertIn("scope_denials", report)
        self.assertIn("结果包文件索引", report)
        self.assertIn("修改文件: 1", report)
        self.assertIn("新增文件: 1", report)
        self.assertIn("删除文件: 1", report)
        self.assertIn("跳过二进制文件: 1", report)
        self.assertIn("跳过噪声条目: 4", report)
        self.assertIn("最终残留失败", report)

    def test_patch_applies_to_a_clean_copy(self) -> None:
        if shutil.which("patch") is None:
            self.skipTest("系统没有 patch 命令")
        source, final = _write_tree_pair(self.tmp)
        bundle = build_result_bundle(
            _record(),
            source,
            final,
            bundle_root=self.tmp / "bundles",
        )

        applied = self.tmp / "applied"
        shutil.copytree(source, applied)
        completed = subprocess.run(
            ["patch", "-p1", "-E", "-i", str(bundle / "patch.diff")],
            cwd=applied,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(
            (applied / "pkg" / "mod.py").read_text(encoding="utf-8"),
            "value = 2\n",
        )
        self.assertFalse((applied / "pkg" / "old.py").exists())
        self.assertEqual(
            (applied / "pkg" / "new.py").read_text(encoding="utf-8"),
            "new\n",
        )

    def test_duplicate_explicit_run_id_is_refused(self) -> None:
        source, final = _write_tree_pair(self.tmp)
        build_result_bundle(
            _record(),
            source,
            final,
            bundle_root=self.tmp / "bundles",
            run_id="same",
        )
        with self.assertRaises(FileExistsError):
            build_result_bundle(
                _record(),
                source,
                final,
                bundle_root=self.tmp / "bundles",
                run_id="same",
            )


class AdoptionTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="result-adopt-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        source, final = _write_tree_pair(self.tmp)
        self.bundle = build_result_bundle(
            _record(),
            source,
            final,
            bundle_root=self.tmp / "bundles",
            run_id="adopt-1",
        )
        self.patch_before = (self.bundle / "patch.diff").read_text(
            encoding="utf-8"
        )

    def test_record_adoption_updates_only_adoption_json(self) -> None:
        self.assertEqual(describe_adoption(self.bundle), "未决定")

        payload = record_adoption(self.bundle, True, "可以合入")
        self.assertIs(payload["adopted"], True)
        self.assertTrue(payload["decided_at"])
        self.assertEqual(payload["note"], "可以合入")
        self.assertEqual(
            json.loads(
                (self.bundle / "adoption.json").read_text(encoding="utf-8")
            ),
            payload,
        )
        self.assertIn("已采纳", describe_adoption(self.bundle))
        self.assertIn("可以合入", describe_adoption(self.bundle))

        rejected = record_adoption(self.bundle, False)
        self.assertIs(rejected["adopted"], False)
        self.assertEqual(rejected["note"], "")
        self.assertEqual(
            describe_adoption(self.bundle),
            f"已拒绝；{rejected['decided_at']}",
        )

        # 红线：采纳只写记录，patch 与两棵树都不动。
        self.assertEqual(
            (self.bundle / "patch.diff").read_text(encoding="utf-8"),
            self.patch_before,
        )
        self.assertTrue(
            (self.tmp / "source" / "pkg" / "mod.py").is_file()
        )

    def test_record_adoption_rejects_non_bool_and_bad_directories(self) -> None:
        with self.assertRaises(TypeError):
            record_adoption(self.bundle, "yes")  # type: ignore[arg-type]
        with self.assertRaises(FileNotFoundError):
            record_adoption(self.tmp / "missing", True)
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            record_adoption(empty, True)


class EndToEndBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="result-e2e-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_cli_run_bundle_patch_accept_and_adopt(self) -> None:
        if shutil.which("patch") is None:
            self.skipTest("系统没有 patch 命令")
        runs_dir = self.tmp / "runs"
        out = self.tmp / "cli-record.json"

        with mock.patch.object(dispatcher, "RUNS_DIR", runs_dir), \
                mock.patch("core.cli.RUNS_DIR", runs_dir), \
                mock.patch.dict(os.environ):
            code, output = _capture_main(
                [
                    "run",
                    "--arm",
                    "C",
                    "--workspace",
                    str(DEMO_REPO),
                    "--out",
                    str(out),
                ]
            )

        self.assertEqual(code, 0, output)
        self.assertIn("结果包:", output)
        self.assertTrue(out.is_file())
        bundles = list((runs_dir / "bundles").iterdir())
        self.assertEqual(len(bundles), 1)
        bundle = bundles[0]
        self.assertEqual(
            json.loads((bundle / "record.json").read_text(encoding="utf-8"))[
                "outcome"
            ],
            "PASS",
        )

        clean = self.tmp / "clean_repo"
        shutil.copytree(DEMO_REPO, clean, ignore=COPY_IGNORE)
        shutil.copytree(
            PLATFORM_ROOT / "profiles" / "demo_repo",
            clean / ".drbug" / "profiles" / "clean_repo",
            ignore=COPY_IGNORE,
        )
        completed = subprocess.run(
            ["patch", "-p1", "-i", str(bundle / "patch.diff")],
            cwd=clean,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + completed.stderr
        )
        accept_code, accept_output = _capture_main(
            ["accept", "--workspace", str(clean)]
        )
        self.assertEqual(accept_code, 0, accept_output)
        self.assertIn("验收结论: PASS", accept_output)

        adopt_yes, yes_output = _capture_main(
            ["adopt", str(bundle), "--yes", "--note", "演示采纳"]
        )
        self.assertEqual(adopt_yes, 0, yes_output)
        self.assertIn("不会把 patch.diff 应用到原仓库", yes_output)
        adoption = json.loads(
            (bundle / "adoption.json").read_text(encoding="utf-8")
        )
        self.assertIs(adoption["adopted"], True)
        self.assertEqual(adoption["note"], "演示采纳")

        adopt_no, no_output = _capture_main(["adopt", str(bundle), "--no"])
        self.assertEqual(adopt_no, 0, no_output)
        adoption = json.loads(
            (bundle / "adoption.json").read_text(encoding="utf-8")
        )
        self.assertIs(adoption["adopted"], False)
        self.assertEqual(adoption["note"], "")
        # adopt 绝不改原仓库：demo_repo 里的旧 bug 仍在。
        self.assertIn(
            "tax = _round_cents(Decimal(subtotal) * TAX_RATE)",
            (DEMO_REPO / "backoffice" / "billing.py").read_text(
                encoding="utf-8"
            ),
        )

    def test_show_run_and_run_repair_include_bundle_and_adoption(self) -> None:
        source, final = _write_tree_pair(self.tmp)
        record = {
            "arm": "C",
            "kb_enabled": True,
            "outcome": "PASS",
            "rounds": [],
            "final_failed": [],
            "scope_denials": [],
        }
        bundle = build_result_bundle(
            record,
            source,
            final,
            bundle_root=self.tmp / "bundles",
            run_id="show-1",
        )

        summary = dispatcher.show_run(str(bundle))
        self.assertIn("结果包:", summary)
        self.assertIn("采纳: 未决定", summary)
        record_adoption(bundle, True, "看过报告")
        summary = dispatcher.show_run(str(bundle / "record.json"))
        self.assertIn("已采纳", summary)
        self.assertIn("看过报告", summary)
        self.assertEqual(locate_bundle(bundle), bundle)

        runs_dir = self.tmp / "runs"
        with mock.patch.object(dispatcher, "RUNS_DIR", runs_dir):
            output = dispatcher.run_repair(
                arm="C",
                actors="scripted",
                workspace=str(DEMO_REPO),
                max_rounds=3,
                out=str(self.tmp / "tui-record.json"),
            )
            # --out 副本按 RUNS_DIR 反查结果包。记录含审查栏之后，
            # 不能再靠安装目录里旧结果包的逐字重合。
            summary = dispatcher.show_run(str(self.tmp / "tui-record.json"))
        self.assertIn("结果包=", output)
        self.assertIn("采纳=未决定", output)
        self.assertEqual(
            len(list((runs_dir / "bundles").iterdir())), 1
        )
        self.assertIn("结果包:", summary)
        self.assertIn("采纳: 未决定", summary)


if __name__ == "__main__":
    unittest.main()
