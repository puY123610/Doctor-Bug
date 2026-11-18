# -*- coding: utf-8 -*-
"""BRIEF1c：秘书审 diff 与否决 + 返工一轮（全离线）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import review as review_mod
from core.actors import FixResult
from core.api import get_run_record
from core.results import build_result_bundle
from core.runner import _baseline_hashes, run_arm
from tests.test_gen5 import (
    BROKEN_FORMAT_LINE,
    _BuiltProfile,
    _write_reexport_shape,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"


class ReviewPatchTests(unittest.TestCase):
    def test_clean_patch_passes(self):
        result = review_mod.review_patch(
            changed=["mslib/format.py"],
            scope_files={"mslib/format.py"},
            scope_denials=[],
            brief_rules=[],
        )
        self.assertEqual(result["verdict"], "通过")
        self.assertEqual(result["reasons"], [])

    def test_test_file_touch_vetoes(self):
        result = review_mod.review_patch(
            changed=["tests/test_money.py"],
            scope_files={"tests/test_money.py"},
            scope_denials=[],
            brief_rules=[],
        )
        self.assertEqual(result["verdict"], "否决")
        self.assertEqual(result["reasons"][0]["check"], "test_file")

    def test_out_of_scope_vetoes_and_names_file(self):
        result = review_mod.review_patch(
            changed=["mslib/format.py", "mslib/extra.py"],
            scope_files={"mslib/format.py"},
            scope_denials=[],
            brief_rules=[],
        )
        self.assertEqual(result["verdict"], "否决")
        checks = [r["check"] for r in result["reasons"]]
        self.assertIn("out_of_scope", checks)
        named = [r["file"] for r in result["reasons"]]
        self.assertIn("mslib/extra.py", named)

    def test_denials_veto(self):
        result = review_mod.review_patch(
            changed=["mslib/format.py"],
            scope_files={"mslib/format.py"},
            scope_denials=[
                {
                    "round": 1,
                    "actor_id": "generalist",
                    "path": "x.py",
                    "reason": "outside",
                }
            ],
            brief_rules=[],
        )
        self.assertEqual(result["verdict"], "否决")
        self.assertEqual(result["reasons"][0]["check"], "scope_denial")

    def test_too_large_vetoes(self):
        changed = [f"pkg/f{i}.py" for i in range(13)]
        result = review_mod.review_patch(
            changed=changed,
            scope_files=set(changed),
            scope_denials=[],
            brief_rules=[],
        )
        self.assertEqual(result["verdict"], "否决")
        self.assertEqual(result["reasons"][0]["check"], "too_large")

    def test_brief_citation_matched_by_changed_file(self):
        result = review_mod.review_patch(
            changed=["mslib/format.py"],
            scope_files={"mslib/format.py"},
            scope_denials=[],
            brief_rules=[
                {"rule": "时长格式含小时", "path": "mslib/format.py", "line": 3}
            ],
        )
        self.assertEqual(len(result["brief_citations"]), 1)

    def test_changed_files_vs_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n", encoding="utf-8")
            (root / "b.py").write_text("y = 2\n", encoding="utf-8")
            baseline = _baseline_hashes(root)
            (root / "a.py").write_text("x = 9\n", encoding="utf-8")
            (root / "b.py").unlink()
            (root / "c.py").write_text("z = 3\n", encoding="utf-8")
            self.assertEqual(
                review_mod.changed_files_vs_baseline(root, baseline),
                ["a.py", "b.py", "c.py"],
            )


class _ReviewRunProfile(_BuiltProfile):
    @classmethod
    def make_repo(cls, root: Path) -> Path:
        return _write_reexport_shape(root)

    def _run(self, *, extra_file=False, comply=True, denial=False, tag="w"):
        calls: list[dict] = []

        def fake_act(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
            calls.append(
                {
                    "actor_id": actor_id,
                    "feedback": kwargs.get("review_feedback", ""),
                }
            )
            ws = Path(workspace)
            target = ws / "mslib" / "format.py"
            text = target.read_text(encoding="utf-8")
            if BROKEN_FORMAT_LINE in text:
                # FIXED_FORMAT_LINE 只覆盖 format_span(5400) == "1h30m"。
                # 冻结验收同时断言 format_span(3600) == "1h"，恒返回
                # "1h30m" 会把运行打成 CAPPED，审查门不会打开。
                passing = (
                    "    if hours:\n"
                    '        return f"{hours}h{minutes}m" if minutes else f"{hours}h"\n'
                    '    return f"{minutes}m{seconds}s"'
                )
                target.write_text(
                    text.replace(BROKEN_FORMAT_LINE, passing),
                    encoding="utf-8",
                )
            if denial and len(calls) == 1:
                sink = kwargs.get("scope_denials")
                if sink is not None:
                    sink.append(
                        {
                            "actor_id": actor_id,
                            "tool": "Edit",
                            "path": "mslib/convert.py",
                            "reason": "path outside the roster files scope",
                        }
                    )
            extra = ws / "mslib" / "extra.py"
            if kwargs.get("review_feedback"):
                if comply and extra.exists():
                    extra.unlink()
            elif extra_file:
                extra.write_text("EXTRA = 1\n", encoding="utf-8")
            return FixResult(actor_id=actor_id, applied=True, note="done")

        with mock.patch(
            "core.agentscope_actors.agentscope_act", new=fake_act
        ):
            record = run_arm(
                "C",
                self.repo,
                max_rounds=3,
                work_root=self.tmp / tag,
                actor_backend="agentscope",
                profile_dir=self.result.profile_dir,
            )
        return record, calls


class ReviewGateRunnerTests(_ReviewRunProfile):
    def test_clean_run_passes_review_without_rework(self):
        record, calls = self._run(tag="clean")
        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.review["status"], "通过")
        self.assertEqual(record.review["rework_rounds"], 0)
        self.assertEqual(len(record.review["attempts"]), 1)
        self.assertEqual(len(calls), 1)

    def test_out_of_scope_veto_then_rework_then_pass(self):
        record, calls = self._run(extra_file=True, comply=True, tag="rework")
        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.review["status"], "通过")
        self.assertEqual(record.review["rework_rounds"], 1)
        self.assertEqual(len(record.review["attempts"]), 2)
        self.assertEqual(record.review["attempts"][0]["status"], "否决")
        self.assertEqual(len(calls), 2)
        self.assertIn("超出本轮派单辖区并集", calls[1]["feedback"])

    def test_persistent_veto_escalates_but_outcome_stays_pass(self):
        record, calls = self._run(
            extra_file=True, comply=False, tag="escalate"
        )
        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.review["status"], "否决")
        self.assertEqual(record.review["rework_rounds"], 1)
        self.assertEqual(len(record.review["attempts"]), 2)
        self.assertEqual(len(calls), 2)

    def test_denial_vetoes_even_with_clean_files(self):
        record, calls = self._run(denial=True, tag="denial")
        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.review["status"], "否决")
        checks = [
            reason["check"]
            for attempt in record.review["attempts"]
            for reason in attempt["reasons"]
        ]
        self.assertIn("scope_denial", checks)


class ScriptedReviewTests(unittest.TestCase):
    def test_scripted_demo_run_passes_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_arm(
                "C",
                DEMO_REPO,
                max_rounds=3,
                work_root=Path(tmp) / "work",
                actor_backend="scripted",
                vault_dir=Path(__file__).resolve().parents[1]
                / "tests"
                / "fixtures"
                / "acceptor_vault",
                kb_path=Path(__file__).resolve().parents[1]
                / "kb"
                / "dispatch_table.json",
            )
        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.review["status"], "通过")
        self.assertIn("review", record.to_dict())


class ReviewSurfaceTests(unittest.TestCase):
    def test_bundle_report_has_review_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            final = root / "final"
            source.mkdir()
            final.mkdir()
            (source / "a.py").write_text("x = 1\n", encoding="utf-8")
            (final / "a.py").write_text("x = 2\n", encoding="utf-8")
            record = {
                "arm": "C",
                "kb_enabled": True,
                "outcome": "PASS",
                "rounds": [],
                "final_failed": [],
                "scope_denials": [],
                "review": {
                    "status": "否决",
                    "rework_rounds": 1,
                    "attempts": [
                        {
                            "status": "否决",
                            "reasons": [
                                {
                                    "check": "out_of_scope",
                                    "file": "a.py",
                                    "detail": (
                                        "改动文件 a.py "
                                        "超出本轮派单辖区并集"
                                    ),
                                }
                            ],
                            "changed_files": ["a.py"],
                            "brief_citations": [],
                        }
                    ],
                    "brief_citations": [],
                },
            }
            bundle = build_result_bundle(
                record, source, final, bundle_root=root / "bundles"
            )
            report = (bundle / "report.md").read_text(encoding="utf-8")
            self.assertIn("## 秘书审查", report)
            self.assertIn("否决", report)
            self.assertIn("采纳请老板慎重", report)

    def test_old_record_without_review_reads_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            path.write_text(
                json.dumps({"arm": "C", "outcome": "PASS"}),
                encoding="utf-8",
            )
            data = get_run_record(path)
            self.assertEqual(data["outcome"], "PASS")
            self.assertNotIn("review", data)


if __name__ == "__main__":
    unittest.main()
