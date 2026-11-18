# -*- coding: utf-8 -*-
"""TRACE1: run-record listing/detail over existing artifacts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.runs import (
    format_run_detail,
    format_run_line,
    list_runs,
    load_run_detail,
)

REPORT = """# BugDoc 结果包

## 概览

- 结果包: `20261116-141843-c-kb-aaaa`
- 生成时间: 2026-11-16T14:18:43+08:00
- 策略臂: C
- 执行后端: agentscope
- 知识库: 开启
- 终局: PASS
- 轮次: 1
- 最终残留失败: 0
"""

RECORD = {
    "arm": "C",
    "kb_enabled": True,
    "outcome": "PASS",
    "rounds": [
        {
            "round": 1,
            "status": "PASS",
            "failed_tests": [],
            "assignments": {"generalist": ["tests/test_x.py::test_a"]},
            "fix_results": [],
        }
    ],
    "final_failed": [],
    "scope_denials": [],
    "review": {"status": "通过", "rework_rounds": 0, "attempts": []},
    "generalist_scope": {"mode": "per_assignment", "widened": False},
}

OLD_CLI_RECORD = {
    "arm": "A",
    "final_failed": ["tests/test_y.py::test_b"],
    "kb_enabled": False,
    "outcome": "FAIL",
    "rounds": [
        {
            "round": 1,
            "status": "FAIL",
            "failed_tests": ["tests/test_y.py::test_b"],
            "assignments": {},
            "fix_results": [],
        }
    ],
    "scope_denials": [{"file": "x.py"}],
}


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


class ListRunsTests(unittest.TestCase):
    def _root(self, tmp: str) -> Path:
        root = Path(tmp) / "runs"
        bundle = root / "bundles" / "20261116-141843-c-kb-aaaa"
        _write(bundle / "record.json", RECORD)
        _write(bundle / "report.md", REPORT)
        _write(root / "api_run_20261116-141843.json", RECORD)
        _write(root / "cli_run_20261115-202120.json", OLD_CLI_RECORD)
        (root / "api_run_20261116-999999.json").write_text(
            "{not json", encoding="utf-8"
        )
        return root

    def test_merge_skip_and_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace1-") as tmp:
            summaries, skipped = list_runs(self._root(tmp), limit=20)
            self.assertEqual(skipped, 1)
            self.assertEqual(len(summaries), 2)
            first = summaries[0]
            self.assertEqual(first.kind, "bundle")
            self.assertEqual(first.backend, "agentscope")
            self.assertEqual(first.when, "2026-11-16 14:18:43")
            self.assertEqual(first.review_verdict, "通过")
            second = summaries[1]
            self.assertEqual(second.kind, "cli")
            self.assertIsNone(second.backend)
            self.assertIsNone(second.review_verdict)
            self.assertEqual(second.final_failed_count, 1)
            self.assertEqual(second.scope_denials_count, 1)

    def test_limit_applies_after_merge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace1-") as tmp:
            summaries, _ = list_runs(self._root(tmp), limit=1)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].kind, "bundle")

    def test_missing_dir_is_empty(self) -> None:
        summaries, skipped = list_runs(Path("/nonexistent-runs-xyz"))
        self.assertEqual(summaries, [])
        self.assertEqual(skipped, 0)


class FormatTests(unittest.TestCase):
    def test_line_and_detail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="trace1-") as tmp:
            root = Path(tmp) / "runs"
            bundle = root / "bundles" / "20261116-141843-c-kb-aaaa"
            _write(bundle / "record.json", RECORD)
            _write(bundle / "report.md", REPORT)
            summaries, _ = list_runs(root)
            line = format_run_line(summaries[0])
            self.assertIn("PASS", line)
            self.assertIn("agentscope", line)
            self.assertIn("秘书审查：通过", line)
            detail = load_run_detail(summaries[0])
            self.assertEqual(
                detail["report_header"]["执行后端"], "agentscope"
            )
            text = "\n".join(format_run_detail(detail))
            self.assertIn("第 1 轮", text)
            self.assertIn("派单：generalist×1", text)
            self.assertIn("兜底工人授权", text)


if __name__ == "__main__":
    unittest.main()
