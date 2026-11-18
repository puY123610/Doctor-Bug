# -*- coding: utf-8 -*-
"""BRIEF1b：员工记忆卫生 — 条目带来源哈希，注入时标过期。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core.agent_archive import append_memory_entries, mark_stale_entries
from core.agentscope_actors import _archive_suffix
from core.runner import RunRecord, _write_agent_memory


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AppendWithHashTests(unittest.TestCase):
    def test_entry_line_and_receipt_carry_hash_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            digest = _sha("fixed content")
            written = append_memory_entries(
                profile,
                "w1",
                [
                    {
                        "test": "tests/test_a.py::test_x",
                        "file": "pkg/a.py",
                        "source_hash": digest,
                    }
                ],
            )
            self.assertEqual(written[0]["source_hash"], digest)
            text = (
                profile / "agents" / "w1" / "MEMORY.md"
            ).read_text(encoding="utf-8")
            self.assertIn(f"来源哈希: {digest[:16]}", text)

    def test_entry_without_hash_renders_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            append_memory_entries(
                profile,
                "w1",
                [{"test": "t::x", "file": "pkg/a.py", "cause": "根因甲"}],
            )
            text = (
                profile / "agents" / "w1" / "MEMORY.md"
            ).read_text(encoding="utf-8")
            self.assertIn("- 测试 `t::x`；文件 `pkg/a.py`；根因: 根因甲", text)
            self.assertNotIn("来源哈希", text)


class WriteAgentMemoryHashTests(unittest.TestCase):
    def test_pass_writeback_records_workcopy_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            (profile / "agents" / "w1").mkdir(parents=True)
            workspace = root / "work"
            (workspace / "pkg").mkdir(parents=True)
            (workspace / "pkg" / "a.py").write_text(
                "fixed content", encoding="utf-8"
            )
            record = RunRecord(
                arm="C",
                kb_enabled=True,
                outcome="PASS",
                rounds=[
                    {
                        "round": 1,
                        "status": "FAIL",
                        "failed_tests": ["t::x"],
                        "assignments": {"w1": ["t::x"]},
                        "fix_results": [
                            {
                                "actor_id": "w1",
                                "applied": True,
                                "note": "done; changed=pkg/a.py",
                            }
                        ],
                    }
                ],
            )
            _write_agent_memory(record, profile, workspace)
            text = (
                profile / "agents" / "w1" / "MEMORY.md"
            ).read_text(encoding="utf-8")
            self.assertIn(f"来源哈希: {_sha('fixed content')[:16]}", text)


class MarkStaleEntriesTests(unittest.TestCase):
    def _memory_text(self, profile: Path) -> str:
        append_memory_entries(
            profile,
            "w1",
            [
                {
                    "test": "t::x",
                    "file": "pkg/a.py",
                    "source_hash": _sha("v1"),
                },
                {"test": "t::y", "file": "pkg/b.py"},
            ],
        )
        return (profile / "agents" / "w1" / "MEMORY.md").read_text(
            encoding="utf-8"
        )

    def test_fresh_entry_unmarked_changed_and_missing_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            text = self._memory_text(profile)
            workspace = root / "ws"
            (workspace / "pkg").mkdir(parents=True)
            target = workspace / "pkg" / "a.py"
            target.write_text("v1", encoding="utf-8")
            fresh = mark_stale_entries(text, workspace)
            self.assertNotIn("可能过期", fresh)
            target.write_text("v2", encoding="utf-8")
            stale = mark_stale_entries(text, workspace)
            self.assertIn("可能过期", stale)
            legacy_line = next(
                line for line in stale.splitlines() if "t::y" in line
            )
            self.assertNotIn("可能过期", legacy_line)
            target.unlink()
            gone = mark_stale_entries(text, workspace)
            self.assertIn("可能过期", gone)


class ArchiveSuffixStaleTests(unittest.TestCase):
    def test_suffix_marks_stale_only_in_injected_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            directory = profile / "agents" / "w1"
            directory.mkdir(parents=True)
            (directory / "IDENTITY.md").write_text(
                "# IDENTITY — w1\n", encoding="utf-8"
            )
            append_memory_entries(
                profile,
                "w1",
                [
                    {
                        "test": "t::x",
                        "file": "pkg/a.py",
                        "source_hash": _sha("v1"),
                    }
                ],
            )
            workspace = root / "ws"
            (workspace / "pkg").mkdir(parents=True)
            target = workspace / "pkg" / "a.py"
            target.write_text("v1", encoding="utf-8")
            suffix = _archive_suffix(profile, "w1", workspace)
            self.assertNotIn("可能过期", suffix)
            target.write_text("v2", encoding="utf-8")
            suffix = _archive_suffix(profile, "w1", workspace)
            self.assertIn("可能过期", suffix)
            on_disk = (directory / "MEMORY.md").read_text(encoding="utf-8")
            self.assertNotIn("可能过期", on_disk)


if __name__ == "__main__":
    unittest.main()
