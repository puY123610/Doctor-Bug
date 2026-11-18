"""EVD1：证据强度——error_count/error_tests 纯增量与成绩单四类分清。

成绩单断言读 core.results.render_scorecard 的真实渲染输出；判卷断言走
FrozenAcceptor.check 的现行判定路径（只替换跑命令那一步的输出）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from acceptor.acceptor import FrozenAcceptor
from acceptor.parsers import parse_pytest
from core.results import build_scorecard, render_scorecard
from core.runner import run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"

# 真实 pytest -q 输出样本（本地对含 fixture 报错/告警的用例实跑后截取）
OUT_WITH_ERRORS = """\
.F.EE                                                                    [100%]
==================================== ERRORS ====================================
=========================== short test summary info ============================
FAILED tests/test_x.py::test_bad - assert 1 == 2
ERROR tests/test_x.py::test_err - RuntimeError: fixture broke
ERROR tests/test_x.py::Suite::test_err2 - RuntimeError: fixture broke
1 failed, 2 passed, 2 errors in 0.02s
"""
OUT_PLAIN = """\
.F.                                                                      [100%]
=========================== short test summary info ============================
FAILED tests/test_x.py::test_bad - assert 1 == 2
1 failed, 2 passed in 0.02s
"""
OUT_WARNINGS = """\
.F.                                                                      [100%]
=============================== warnings summary ===============================
tests/test_x.py::test_ok
  /tmp/ep/tests/test_x.py:6: UserWarning: careful
    warnings.warn("careful", UserWarning)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_x.py::test_bad - assert 1 == 2
1 failed, 2 passed, 3 warnings in 0.02s
"""
OUT_ALL_PASS = "...                                   [100%]\n3 passed in 0.01s\n"
OUT_PASS_WARN = "...                                   [100%]\n3 passed, 1 warning in 0.01s\n"


def _acceptor(output: str, returncode: int) -> FrozenAcceptor:
    """现行判定路径：真 FrozenAcceptor.check，只把跑命令换成固定输出。"""
    acceptor = FrozenAcceptor.__new__(FrozenAcceptor)
    acceptor.manifest = {"kind": "behavior", "files": {}}
    acceptor.parser = parse_pytest
    acceptor._run_check = lambda workspace: (returncode, output)  # type: ignore[method-assign]
    return acceptor


A = "tests/test_a.py::test_a"
B = "tests/test_b.py::test_b"
C = "tests/test_c.py::test_c"
R1 = "tests/test_r.py::test_r1"


def _record(*, final_failed, first_passed=5, notes=None, final_errors=None,
            first_errors=None, outcome="CAPPED"):
    fix_results = [
        {"actor_id": "generalist", "applied": False, "note": note}
        for note in (notes or ["改了 billing.py，C 仍红"])
    ]
    record = {
        "arm": "C",
        "outcome": outcome,
        "rounds": [
            {
                "round": 1,
                "status": "FAIL",
                "failed_tests": [A, B, C],
                "passed_count": first_passed,
                "failed_count": 3,
                "error_count": len(first_errors or []),
                "error_tests": list(first_errors or []),
                "assignments": {"generalist": [A, B, C]},
                "fix_results": fix_results,
            }
        ],
        "final_failed": list(final_failed),
    }
    if final_errors is not None:
        record["final_errors"] = final_errors
    return record


class ErrorCountTest(unittest.TestCase):
    def test_1_two_errors_parsed_and_passed_through_verdict(self) -> None:
        parsed = parse_pytest(OUT_WITH_ERRORS, 1)
        self.assertEqual(parsed.error_count, 2)
        self.assertEqual(
            parsed.error_tests,
            ["tests/test_x.py::test_err", "tests/test_x.py::Suite::test_err2"],
        )
        verdict = _acceptor(OUT_WITH_ERRORS, 1).check(Path("."))
        self.assertEqual(verdict.error_count, 2)
        self.assertEqual(verdict.error_tests, parsed.error_tests)
        self.assertEqual(verdict.to_dict()["error_count"], 2)
        # 无 error：0 / 空，to_dict 键集与旧版逐字一致
        plain = parse_pytest(OUT_PLAIN, 1)
        self.assertEqual((plain.error_count, plain.error_tests), (0, []))
        plain_verdict = _acceptor(OUT_PLAIN, 1).check(Path("."))
        self.assertEqual(plain_verdict.error_count, 0)
        self.assertNotIn("error_count", plain_verdict.to_dict())

    def test_2_warnings_do_not_change_any_count(self) -> None:
        with_warn = parse_pytest(OUT_WARNINGS, 1)
        without = parse_pytest(OUT_PLAIN, 1)
        for field in ("failed_tests", "passed_count", "failed_count",
                      "executed", "error_count", "error_tests"):
            self.assertEqual(getattr(with_warn, field), getattr(without, field), field)
        self.assertEqual(
            _acceptor(OUT_PASS_WARN, 0).check(Path(".")).to_dict(),
            _acceptor(OUT_ALL_PASS, 0).check(Path(".")).to_dict(),
        )

    def test_7_verdict_status_unchanged_by_error_fields(self) -> None:
        cases = [
            (OUT_ALL_PASS, 0, "PASS"),
            (OUT_PASS_WARN, 0, "PASS"),
            (OUT_PLAIN, 1, "FAIL"),
            (OUT_WARNINGS, 1, "FAIL"),
            (OUT_WITH_ERRORS, 1, "FAIL"),
        ]
        for output, code, expected in cases:
            with self.subTest(expected=expected, code=code):
                verdict = _acceptor(output, code).check(Path("."))
                parsed = parse_pytest(output, code)
                # 现行判定公式（acceptor.check 原文）：rc==0 且无失败才 PASS
                legacy = (
                    "PASS"
                    if code == 0 and parsed.failed_count == 0 and not parsed.failed_tests
                    else "FAIL"
                )
                self.assertEqual(verdict.status, expected)
                self.assertEqual(verdict.status, legacy)
                # error 用例不进 evidence（派活证据只认 FAILED）
                self.assertEqual(
                    [item.node_id for item in verdict.evidence], parsed.failed_tests
                )


class ScorecardTest(unittest.TestCase):
    def test_3_target_and_regression_rates_with_fractions(self) -> None:
        record = _record(final_failed=[C, R1])
        card = build_scorecard(record)
        self.assertEqual((card["fixed"], card["denominator"]), (2, 3))
        self.assertEqual(
            (card["regression"]["kept"], card["regression"]["total"]), (4, 5)
        )
        text = "\n".join(render_scorecard(record))
        self.assertIn("第一遍查出的毛病：修好了 2 / 3 条", text)
        self.assertIn("原来是好的：现在仍然好的 4 / 5 条", text)
        self.assertIn(f"被改坏了：`{R1}`", text)
        self.assertIn(f"没修好的：1 条", text)
        self.assertIn(f"`{C}`：修理轮数用完仍没通过；最后一次修理结论：改了 billing.py，C 仍红", text)
        self.assertNotIn("目标集", text)
        self.assertNotIn("回归集", text)

    def test_4_test_wrong_mark_moves_out_of_denominator(self) -> None:
        record = _record(
            final_failed=[C],
            notes=[f"查清了。\nTEST-WRONG: {C} | 断言要求退款给 support 角色，与需求文档相反"],
        )
        card = build_scorecard(record)
        self.assertEqual((card["fixed"], card["denominator"]), (2, 2))
        self.assertEqual(card["unfixed"], [])
        text = "\n".join(render_scorecard(record))
        self.assertIn("修好了 2 / 2 条", text)
        self.assertIn("可能是测试本身写错了（不算没修好，请老板确认）：1 条", text)
        self.assertIn(f"`{C}`：断言要求退款给 support 角色，与需求文档相反", text)
        self.assertNotIn("没修好的", text)
        # 标记只影响成绩单：终验里 C 仍按实际红算
        self.assertIn(C, record["final_failed"])

    def test_4b_mark_for_node_outside_first_failures_is_ignored(self) -> None:
        record = _record(final_failed=[C, R1], notes=[f"TEST-WRONG: {R1} | 想藏回归"])
        card = build_scorecard(record)
        self.assertEqual(card["test_wrong"], [])
        self.assertEqual(card["regression"]["broken"], [R1])

    def test_5_final_error_on_target_goes_to_not_run(self) -> None:
        record = _record(
            final_failed=[],
            final_errors={"error_count": 1, "error_tests": [C], "passed_count": 7},
        )
        card = build_scorecard(record)
        self.assertEqual((card["fixed"], card["denominator"]), (2, 2))
        self.assertEqual([item["node"] for item in card["not_run"]], [C])
        # 终验通过 7 = 原来好的 5 + 修好的 A/B 2 → 原来好的仍 5/5
        self.assertEqual(
            (card["regression"]["kept"], card["regression"]["total"]), (5, 5)
        )
        text = "\n".join(render_scorecard(record))
        self.assertIn("修好了 2 / 2 条", text)
        self.assertIn("没跑起来（测试根本没执行成功，不算修好也不算没修好）：1 条", text)
        self.assertIn(f"`{C}`（终验检查时报错没跑成）", text)

    def test_5b_collection_error_by_file_keeps_full_node_ids(self) -> None:
        record = _record(
            final_failed=[],
            final_errors={"error_count": 1, "error_tests": ["tests/test_c.py"], "passed_count": 7},
        )
        card = build_scorecard(record)
        self.assertEqual([item["node"] for item in card["not_run"]], [C])
        self.assertIn(f"`{C}`", "\n".join(render_scorecard(record)))

    def test_6_zero_detection_says_so_without_ratios(self) -> None:
        record = {"arm": "C", "outcome": "PASS", "rounds": [], "final_failed": []}
        lines = render_scorecard(record)
        self.assertEqual(lines, ["## 成绩单", "", "- 目前没有检测出来问题。"])
        text = "\n".join(lines)
        self.assertNotIn("/", text)
        self.assertNotIn("0 / 0", text)


class RealRunScorecardTest(unittest.TestCase):
    def test_real_run_records_first_counts_and_scores(self) -> None:
        with tempfile.TemporaryDirectory(prefix="evd1-") as temporary:
            root = Path(temporary)
            source = root / "demo"
            shutil.copytree(
                DEMO_REPO, source,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
            )
            record = run_arm(
                "C",
                source,
                max_rounds=6,
                work_root=root / "work",
                vault_dir=PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault",
                kb_path=PLATFORM_ROOT / "kb" / "dispatch_table.json",
            )
        self.assertEqual(record.outcome, "PASS")
        first = record.rounds[0]
        self.assertEqual(first["failed_count"], len(first["failed_tests"]))
        self.assertIsInstance(first["passed_count"], int)
        self.assertEqual(first["error_count"], 0)
        payload = record.to_dict()
        self.assertNotIn("final_errors", payload)
        card = build_scorecard(payload)
        targets = len(first["failed_tests"])
        self.assertEqual((card["fixed"], card["denominator"]), (targets, targets))
        self.assertEqual(
            (card["regression"]["kept"], card["regression"]["total"]),
            (first["passed_count"], first["passed_count"]),
        )
        self.assertIn(
            f"修好了 {targets} / {targets} 条", "\n".join(render_scorecard(payload))
        )


if __name__ == "__main__":
    unittest.main()
