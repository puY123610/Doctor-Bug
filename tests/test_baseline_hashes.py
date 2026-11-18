"""RESULT2: record per-file sha256 of the original tree before copy.

The baseline is the fingerprint of the source workspace at the moment
runner copies it. Ignored names (``_COPY_IGNORE``) are neither copied
nor recorded. The map is stored on the run record and emitted as
``record.json`` field ``baseline_hashes``. It must not be written
inside the working copy. Records written before this field existed
still bundle; a missing field means "no baseline".
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from core.results import build_result_bundle
from core.runner import _COPY_IGNORE, run_arm


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignored_names(directory: Path, names: list[str]) -> set[str]:
    """Call ``_COPY_IGNORE`` the same way ``shutil.copytree`` does."""
    ignored = _COPY_IGNORE(str(directory), list(names))
    if ignored is None:
        return set()
    return set(ignored)


def independent_baseline(root: Path) -> dict[str, str]:
    """Recompute sha256 from the tree itself. Does not call runner code
    for the digest. Skip only names ``_COPY_IGNORE`` would skip, because
    those files are not part of the copy and must not be in the baseline.
    """
    hashes: dict[str, str] = {}

    def walk(directory: Path) -> None:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
        skipped = _ignored_names(directory, [item.name for item in children])
        for child in children:
            if child.name in skipped:
                continue
            if child.is_dir():
                walk(child)
                continue
            if child.is_file():
                relative = child.relative_to(root).as_posix()
                hashes[relative] = _sha256_file(child)

    walk(root)
    return hashes


def _write_original(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "README.md").write_text("original\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00pyc")


def _run_and_bundle(original: Path, out_root: Path):
    """One zero-round scripted run on *original*, then build its bundle.

    Zero rounds: the copy and the baseline happen, no actor runs.
    The work-copy path comes from the run_started event.
    """
    events: list[dict] = []
    _root = Path(__file__).resolve().parents[1]
    record = run_arm(
        "C",
        original,
        kb_enabled=True,
        max_rounds=0,
        work_root=out_root / "work",
        actor_backend="scripted",
        on_event=events.append,
        vault_dir=_root / "tests" / "fixtures" / "acceptor_vault",
        kb_path=_root / "kb" / "dispatch_table.json",
    )
    started = [e for e in events if e.get("event") == "run_started"]
    workspace = Path(started[0]["workspace"])
    bundle = build_result_bundle(
        record, original, workspace, bundle_root=out_root / "bundles"
    )
    return record.to_dict(), workspace, bundle


def _legacy_record() -> dict:
    """The pre-RESULT2 record shape: every key except baseline_hashes."""
    return {
        "arm": "C",
        "kb_enabled": True,
        "outcome": "PASS",
        "rounds": [],
        "final_failed": [],
        "scope_denials": [],
    }


class BaselineFingerprintTests(unittest.TestCase):
    def test_record_baseline_matches_independent_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.mkdir()
            _write_original(original)
            record, workspace, bundle = _run_and_bundle(original, root / "out")

            saved = record["baseline_hashes"]
            self.assertEqual(saved, independent_baseline(original))

            on_disk = json.loads((bundle / "record.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["baseline_hashes"], saved)
            self.assertEqual(on_disk["baseline_hashes"], independent_baseline(original))
            self.assertTrue((workspace / "src" / "app.py").is_file())

    def test_later_edit_of_original_mismatches_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.mkdir()
            _write_original(original)
            _record, _workspace, bundle = _run_and_bundle(original, root / "out")
            baseline = json.loads((bundle / "record.json").read_text(encoding="utf-8"))[
                "baseline_hashes"
            ]

            target = original / "src" / "app.py"
            target.write_text("print('changed after copy')\n", encoding="utf-8")
            recomputed = _sha256_file(target)

            self.assertIn("src/app.py", baseline)
            self.assertNotEqual(recomputed, baseline["src/app.py"])
            self.assertNotEqual(
                independent_baseline(original)["src/app.py"],
                baseline["src/app.py"],
            )
            self.assertEqual(
                _sha256_file(original / "README.md"),
                baseline["README.md"],
            )

    def test_legacy_record_without_baseline_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            final = root / "final"
            source.mkdir()
            final.mkdir()
            (source / "README.md").write_text("original\n", encoding="utf-8")
            (final / "README.md").write_text("original\n", encoding="utf-8")

            bundle = build_result_bundle(
                _legacy_record(), source, final, bundle_root=root / "bundles"
            )
            on_disk = json.loads((bundle / "record.json").read_text(encoding="utf-8"))
            self.assertNotIn("baseline_hashes", on_disk)
            self.assertIsNone(on_disk.get("baseline_hashes"))

    def test_baseline_manifest_is_not_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            original.mkdir()
            _write_original(original)
            record, workspace, _bundle = _run_and_bundle(original, root / "out")
            baseline = record["baseline_hashes"]

            copied_files = set()
            for path in workspace.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(workspace)
                if relative.parts[0] in {"__pycache__", ".pytest_cache", ".drbug"}:
                    continue
                if path.suffix == ".pyc":
                    continue
                copied_files.add(relative.as_posix())
            self.assertEqual(copied_files, set(independent_baseline(original)))
            for path in workspace.rglob("*"):
                if not path.is_file():
                    continue
                self.assertNotIn(b"baseline_hashes", path.read_bytes())

    def test_build_result_bundle_writes_baseline_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            final = root / "final"
            source.mkdir()
            final.mkdir()
            (source / "README.md").write_text("original\n", encoding="utf-8")
            (final / "README.md").write_text("fixed\n", encoding="utf-8")
            record = _legacy_record()
            record["baseline_hashes"] = {"README.md": "abc"}

            bundle = build_result_bundle(
                record, source, final, bundle_root=root / "bundles"
            )
            on_disk = json.loads((bundle / "record.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["baseline_hashes"], {"README.md": "abc"})


if __name__ == "__main__":
    unittest.main()
