# -*- coding: utf-8 -*-
"""F1: vault-only hidden tests — frozen, judged, never in the work copy."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptor.acceptor import FrozenAcceptor
from acceptor.frozen import freeze
from core.runner import run_arm

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
VISIBLE_TEST = (
    "from calc.ops import add\n\n\n"
    "def test_add_positive():\n    assert add(1, 2) == 3\n"
)
HIDDEN_TEST = (
    "from calc.ops import add\n\n\n"
    "def test_add_negative():\n    assert add(-1, -2) == -3\n"
)


def _repo(root: Path, code: str, *, with_hidden: bool) -> Path:
    repo = root
    (repo / "calc").mkdir(parents=True, exist_ok=True)
    (repo / "calc" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "calc" / "ops.py").write_text(code, encoding="utf-8")
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_visible.py").write_text(
        VISIBLE_TEST, encoding="utf-8"
    )
    if with_hidden:
        (repo / "tests" / "test_hidden.py").write_text(
            HIDDEN_TEST, encoding="utf-8"
        )
    return repo


class HiddenFreezeTests(unittest.TestCase):
    def _setup(self, tmp: str):
        root = Path(tmp)
        fixed = _repo(root / "fixed", FIXED, with_hidden=True)
        vault = root / "vault"
        manifest = freeze(
            fixed, vault, package="calc", hidden=["tests/test_hidden.py"]
        )
        return root, vault, manifest

    def test_manifest_shape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            _, _, manifest = self._setup(tmp)
            self.assertIn("tests/test_hidden.py", manifest["hidden_files"])
            self.assertNotIn("tests/test_hidden.py", manifest["files"])
            self.assertIn("tests/test_visible.py", manifest["files"])

    def test_hidden_runs_but_is_not_required_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            root, vault, _ = self._setup(tmp)
            buggy = _repo(root / "buggy", BUGGY, with_hidden=False)
            verdict = FrozenAcceptor(vault).check(buggy)
            self.assertEqual(verdict.status, "FAIL")
            self.assertFalse(verdict.tampered_files)
            self.assertTrue(
                any("test_hidden" in node for node in verdict.failed_tests),
                verdict.failed_tests,
            )
            fixed_ws = _repo(root / "fixedws", FIXED, with_hidden=False)
            verdict2 = FrozenAcceptor(vault).check(fixed_ws)
            self.assertEqual(verdict2.status, "PASS")

    def test_hidden_present_but_altered_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            root, vault, _ = self._setup(tmp)
            ws = _repo(root / "ws", FIXED, with_hidden=True)
            (ws / "tests" / "test_hidden.py").write_text(
                "def test_add_negative():\n    assert True\n",
                encoding="utf-8",
            )
            verdict = FrozenAcceptor(vault).check(ws)
            self.assertEqual(verdict.status, "TAMPERED")
            self.assertIn("tests/test_hidden.py", verdict.tampered_files)

    def test_visible_tamper_still_caught(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            root, vault, _ = self._setup(tmp)
            ws = _repo(root / "ws", FIXED, with_hidden=False)
            (ws / "tests" / "test_visible.py").write_text(
                "def test_add_positive():\n    assert True\n",
                encoding="utf-8",
            )
            verdict = FrozenAcceptor(vault).check(ws)
            self.assertEqual(verdict.status, "TAMPERED")

    def test_vault_integrity_covers_hidden(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            _, vault, _ = self._setup(tmp)
            (vault / "tests" / "test_hidden.py").unlink()
            with self.assertRaises(RuntimeError):
                FrozenAcceptor(vault)


class HiddenWorkCopyTests(unittest.TestCase):
    def test_work_copy_excludes_hidden_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f1-") as tmp:
            root = Path(tmp)
            source = _repo(root / "src", FIXED, with_hidden=True)
            profile = root / "profile"
            vault = profile / "vault"
            freeze(
                source, vault, package="calc",
                hidden=["tests/test_hidden.py"],
            )
            (profile / "dispatch_table.json").write_text(
                "{}", encoding="utf-8"
            )
            seen: list[str] = []

            def on_event(event: dict) -> None:
                if event.get("event") == "run_started":
                    seen.append(str(event.get("workspace", "")))

            record = run_arm(
                "C",
                source,
                max_rounds=2,
                work_root=root / "work",
                actor_backend="scripted",
                profile_dir=profile,
                on_event=on_event,
            )
            self.assertEqual(record.outcome, "PASS")
            self.assertTrue(seen)
            work_copy = Path(seen[-1])
            self.assertTrue((work_copy / "tests" / "test_visible.py").is_file())
            self.assertFalse((work_copy / "tests" / "test_hidden.py").exists())
            # the source repo keeps its file — only the copy omits it
            self.assertTrue((source / "tests" / "test_hidden.py").is_file())


if __name__ == "__main__":
    unittest.main()
