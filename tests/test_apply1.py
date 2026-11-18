# -*- coding: utf-8 -*-
"""APPLY1：一键贴补丁 — 基线指纹防覆盖、整包拒绝、试运行。"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.cli import main as cli_main
from core.results import apply_bundle, build_result_bundle, record_adoption


def _baseline(source: Path) -> dict:
    hashes = {}
    for path in sorted(source.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(source))
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _record(source: Path) -> dict:
    return {
        "arm": "C",
        "kb_enabled": True,
        "outcome": "PASS",
        "rounds": [],
        "final_failed": [],
        "scope_denials": [],
        "baseline_hashes": _baseline(source),
    }


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> dict:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class ApplyBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "source"
        self.final = self.tmp / "final"
        _write(self.source, "pkg/a.py", "alpha\nbeta\n")
        _write(self.source, "old.py", "gone\n")
        _write(self.final, "pkg/a.py", "alpha\nBETA\n")
        _write(self.final, "pkg/c.py", "new file\n")
        self.target = self.tmp / "target"
        _write(self.target, "pkg/a.py", "alpha\nbeta\n")
        _write(self.target, "old.py", "gone\n")
        self.bundle = build_result_bundle(
            _record(self.source),
            self.source,
            self.final,
            bundle_root=self.tmp / "bundles",
            run_id="apply-1",
        )

    def test_clean_apply_matches_final_tree(self):
        result = apply_bundle(self.bundle, self.target)
        self.assertTrue(result["applied"])
        self.assertEqual(_tree(self.target), _tree(self.final))
        adoption = json.loads(
            (self.bundle / "adoption.json").read_text(encoding="utf-8")
        )
        self.assertTrue(adoption["adopted"])
        self.assertEqual(
            sorted(adoption["applied"]["files"]),
            ["old.py", "pkg/a.py", "pkg/c.py"],
        )

    def test_conflict_when_original_edited_writes_nothing(self):
        _write(self.target, "pkg/a.py", "alpha\nboss edit\n")
        before = _tree(self.target)
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(self.bundle, self.target)
        self.assertIn("pkg/a.py", str(ctx.exception))
        self.assertEqual(_tree(self.target), before)

    def test_conflict_when_added_file_now_exists(self):
        _write(self.target, "pkg/c.py", "someone was here\n")
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(self.bundle, self.target)
        self.assertIn("pkg/c.py", str(ctx.exception))
        self.assertFalse((self.target / "pkg" / "a.py").read_text(
            encoding="utf-8") == "alpha\nBETA\n")

    def test_missing_baseline_refused(self):
        record = _record(self.source)
        del record["baseline_hashes"]
        bundle = build_result_bundle(
            record,
            self.source,
            self.final,
            bundle_root=self.tmp / "bundles",
            run_id="apply-nobase",
        )
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(bundle, self.target)
        self.assertIn("基线指纹", str(ctx.exception))

    def test_dry_run_writes_nothing(self):
        before = _tree(self.target)
        result = apply_bundle(self.bundle, self.target, dry_run=True)
        self.assertFalse(result["applied"])
        self.assertEqual(len(result["files"]), 3)
        self.assertEqual(_tree(self.target), before)
        adoption = json.loads(
            (self.bundle / "adoption.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(adoption["adopted"])

    def test_rejected_bundle_refused(self):
        record_adoption(self.bundle, False, "不要")
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(self.bundle, self.target)
        self.assertIn("拒绝", str(ctx.exception))

    def test_secret_path_refused(self):
        final = self.tmp / "final-secret"
        _write(final, "pkg/a.py", "alpha\nbeta\n")
        _write(final, "old.py", "gone\n")
        _write(final, ".env", "TOKEN=should-never-apply\n")
        bundle = build_result_bundle(
            _record(self.source),
            self.source,
            final,
            bundle_root=self.tmp / "bundles",
            run_id="apply-secret",
        )
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(bundle, self.target)
        self.assertIn(".env", str(ctx.exception))
        self.assertFalse((self.target / ".env").exists())

    def test_no_trailing_newline_preserved(self):
        source = self.tmp / "src-nl"
        final = self.tmp / "fin-nl"
        target = self.tmp / "tgt-nl"
        for root, text in (
            (source, "one\ntwo"),
            (final, "one\nTWO"),
            (target, "one\ntwo"),
        ):
            _write(root, "f.txt", text)
        bundle = build_result_bundle(
            _record(source),
            source,
            final,
            bundle_root=self.tmp / "bundles",
            run_id="apply-nl",
        )
        apply_bundle(bundle, target)
        self.assertEqual(
            (target / "f.txt").read_bytes(),
            (final / "f.txt").read_bytes(),
        )

    def test_empty_patch_refused(self):
        bundle = build_result_bundle(
            _record(self.source),
            self.source,
            self.source,
            bundle_root=self.tmp / "bundles",
            run_id="apply-empty",
        )
        with self.assertRaises(ValueError) as ctx:
            apply_bundle(bundle, self.target)
        self.assertIn("为空", str(ctx.exception))


class ApplyCliTests(unittest.TestCase):
    def test_cli_apply_then_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            final = root / "final"
            target = root / "target"
            _write(source, "a.py", "x = 1\n")
            _write(final, "a.py", "x = 2\n")
            _write(target, "a.py", "x = 1\n")
            bundle = build_result_bundle(
                _record(source),
                source,
                final,
                bundle_root=root / "bundles",
                run_id="cli-1",
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli_main(
                    ["apply", str(bundle), "--workspace", str(target)]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                (target / "a.py").read_text(encoding="utf-8"), "x = 2\n"
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli_main(
                    ["apply", str(bundle), "--workspace", str(target)]
                )
            self.assertEqual(rc, 1)
            self.assertIn("未应用", out.getvalue())


if __name__ == "__main__":
    unittest.main()
