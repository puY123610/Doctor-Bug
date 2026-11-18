"""LOAD1：同一失败多候选择一，按本次运行累计派单数选最少。

两栏账本：dispatched（累计派单数，只加不减，选人唯一依据）与 in_hand
（在手数，派单 +1、判卷返回 -1、出错路径 -1，轮末归 0）。派单断言读真实
运行记录的 assignments 与 load；计数断言读账本真实值（运行记录 / load
事件）。parallel 各副本经 pick_one=False 保持全派。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import Verdict
from acceptor.evidence import evidence_from_failed
from core.actors import FixResult
import core.runner as runner_mod
from core.parallel import run_parallel
from core.policies import policy_c
from core.results import render_scorecard
from core.runner import (
    _new_ledger,
    _pick_least_loaded,
    _release,
    run_arm,
)
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_VAULT = PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault"
DEMO_KB = PLATFORM_ROOT / "kb" / "dispatch_table.json"
MULTI = "tests/test_orders.py::test_full_checkout_flow"
MULTI_ORDER = ["billing-specialist", "inventory-specialist", "auth-specialist"]


def _run(tmp: str, **kwargs):
    return run_arm(
        "C",
        DEMO_REPO,
        work_root=Path(tmp) / "work",
        vault_dir=DEMO_VAULT,
        kb_path=DEMO_KB,
        **kwargs,
    )


def _holders(assignments: dict, node: str) -> list[str]:
    return [actor for actor, nodes in assignments.items() if node in nodes]


def _two_kb(order: list[str]) -> DispatchKB:
    """只有一条双候选节点覆盖的最小派单表（顺序可控）。"""
    return DispatchKB(
        {
            "fallback": "generalist",
            "node_overrides": {
                "tests/test_x.py::test_x": {"specialists": list(order)}
            },
        }
    )


def _no_fix(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
    return FixResult(actor_id=actor_id, applied=False, note="no change")


class PickOneRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            cls.record = _run(tmp)
        cls.payload = json.loads(json.dumps(cls.record.to_dict()))

    # 1. 多候选只派一人，且是累计派单数最少者
    def test_1_multi_candidate_dispatched_to_one_least_loaded(self):
        first = self.payload["rounds"][0]
        holders = _holders(first["assignments"], MULTI)
        self.assertEqual(len(holders), 1)
        (pick,) = first["load"]["picks"]
        self.assertEqual(pick["node"], MULTI)
        self.assertEqual(pick["candidates"], MULTI_ORDER)
        self.assertEqual(holders, [pick["picked"]])
        self.assertEqual(
            pick["counts"][pick["picked"]], min(pick["counts"].values())
        )
        card = "\n".join(render_scorecard(self.payload))
        self.assertIn("派给当时手上活最少的", card)
        self.assertIn(f"派给 {pick['picked']}", card)

    # 2. 账本：派单 +1（累计栏=派出节点数），判卷返回 -1（在手栏归 0）
    def test_2_ledger_plus_on_dispatch_minus_on_verdict_return(self):
        first = self.payload["rounds"][0]
        sent = {a: len(n) for a, n in first["assignments"].items()}
        self.assertEqual(first["load"]["dispatched"], sent)
        self.assertEqual(set(first["load"]["in_hand"].values()), {0})

        kb = _two_kb(["a", "b"])
        ledger = _new_ledger()
        plan, _ = _pick_least_loaded(
            {"a": ["tests/test_x.py::test_x"], "b": ["tests/test_x.py::test_x"]},
            ["tests/test_x.py::test_x"],
            kb,
            ledger,
        )
        self.assertEqual(ledger["dispatched"], {"a": 1})
        self.assertEqual(ledger["in_hand"], {"a": 1})
        _release(ledger, plan)
        self.assertEqual(ledger["in_hand"], {"a": 0})
        self.assertEqual(ledger["dispatched"], {"a": 1})

    # 6. 单候选仍派那一个：除多候选节点外与现行 policy_c 派单表逐条一致
    def test_6_single_candidate_unchanged(self):
        first = self.payload["rounds"][0]
        kb = DispatchKB.from_file(DEMO_KB)
        verdict = Verdict(
            status="FAIL",
            failed_tests=list(first["failed_tests"]),
            evidence=evidence_from_failed(first["failed_tests"]),
        )
        old = policy_c(verdict, [], kb)
        for actor, nodes in old.items():
            singles = [n for n in nodes if n != MULTI]
            got = [n for n in first["assignments"].get(actor, []) if n != MULTI]
            self.assertEqual(got, singles)


class ErrorPathTest(unittest.TestCase):
    # 3. 工人中途出错：在手栏回落到派单前（0），不漏 -1
    def test_3_error_path_releases_in_hand(self):
        events: list[dict] = []
        ledgers: list[dict] = []
        real_new_ledger = runner_mod._new_ledger

        def capture():
            # 原样建账本，只把这次运行的真实账本对象留底以便读值
            ledger = real_new_ledger()
            ledgers.append(ledger)
            return ledger

        def boom(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
            raise RuntimeError("worker crashed")

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "core.runner.act", side_effect=boom
        ), mock.patch("core.runner._new_ledger", side_effect=capture):
            with self.assertRaises(RuntimeError):
                _run(tmp, on_event=events.append)
        (dispatch,) = [e for e in events if e["event"] == "dispatch"]
        (ledger,) = ledgers
        sent = {a: len(n) for a, n in dispatch["assignments"].items()}
        # 累计栏照记派出数（只加不减），在手栏在出错路径回落到派单前的 0
        self.assertEqual(ledger["dispatched"], sent)
        self.assertEqual(set(ledger["in_hand"]), set(sent))
        self.assertEqual(set(ledger["in_hand"].values()), {0})


class SwapNextRoundTest(unittest.TestCase):
    # 4. 第一轮派 A 没修好，第二轮同一失败派 B（A 累计高于 B）
    def test_4_unit_two_candidates_swap(self):
        kb = _two_kb(["A", "B"])
        node = "tests/test_x.py::test_x"
        ledger = _new_ledger()
        both = {"A": [node], "B": [node]}
        plan1, picks1 = _pick_least_loaded(both, [node], kb, ledger)
        self.assertEqual(plan1, {"A": [node]})
        self.assertEqual(picks1[0]["counts"], {"A": 0, "B": 0})
        _release(ledger, plan1)
        plan2, picks2 = _pick_least_loaded(both, [node], kb, ledger)
        self.assertEqual(plan2, {"B": [node]})
        self.assertEqual(picks2[0]["counts"], {"A": 1, "B": 0})

    def test_4_real_run_swaps_after_unfixed_round(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "core.runner.act", side_effect=_no_fix
        ):
            record = _run(tmp, max_rounds=2)
        self.assertEqual(record.outcome, "CAPPED")
        r1, r2 = record.rounds
        (p1,) = r1["load"]["picks"]
        (p2,) = r2["load"]["picks"]
        self.assertNotEqual(p2["picked"], p1["picked"])
        self.assertGreater(p2["counts"][p1["picked"]], p2["counts"][p2["picked"]])
        self.assertEqual(p2["counts"][p2["picked"]], min(p2["counts"].values()))
        self.assertEqual(_holders(r2["assignments"], MULTI), [p2["picked"]])
        for rd in (r1, r2):
            self.assertEqual(set(rd["load"]["in_hand"].values()), {0})


class FreshLedgerTest(unittest.TestCase):
    # 5. 新一次运行账本从 0 起算，不带上一次的账
    def test_5_new_run_starts_from_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _run(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            second = _run(tmp)
        a = first.rounds[0]["load"]
        b = second.rounds[0]["load"]
        self.assertEqual(a["picks"], b["picks"])
        self.assertEqual(a["dispatched"], b["dispatched"])
        sent = {k: len(v) for k, v in second.rounds[0]["assignments"].items()}
        self.assertEqual(b["dispatched"], sent)


class TieOrderTest(unittest.TestCase):
    # 7. 平局按 kb.lookup 顺序取第一个（不另排序，非字母序）
    def test_7_tie_takes_lookup_first(self):
        node = "tests/test_x.py::test_x"
        for order in (["zeta", "alpha"], ["alpha", "zeta"]):
            with self.subTest(order=order):
                kb = _two_kb(order)
                # policy 给的派单表顺序故意与 lookup 相反
                both = {order[1]: [node], order[0]: [node]}
                plan, picks = _pick_least_loaded(both, [node], kb, _new_ledger())
                self.assertEqual(plan, {order[0]: [node]})
                self.assertEqual(picks[0]["candidates"], order)

    def test_7_demo_tie_follows_table_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = _run(tmp)
        (pick,) = record.rounds[0]["load"]["picks"]
        least = min(pick["counts"].values())
        tied = [a for a in MULTI_ORDER if pick["counts"][a] == least]
        self.assertEqual(pick["picked"], tied[0])


class ParallelUnchangedTest(unittest.TestCase):
    # 8. parallel 各副本仍是全派（pick_one=False），与串行择一对照
    def test_8_parallel_passes_full_dispatch_switch(self):
        seen: list[dict] = []
        records: list = []

        def spy(arm, workspace_src, **kwargs):
            # 只记参数、原样转给真 run_arm；每份副本的真实运行记录留底
            seen.append(kwargs)
            record = run_arm(arm, workspace_src, **kwargs)
            records.append(record)
            return record

        with tempfile.TemporaryDirectory() as tmp:
            run_parallel(
                "C",
                DEMO_REPO,
                attempts=2,
                work_root=Path(tmp) / "work",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
                run_one=spy,
            )
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(kw["pick_one"] is False for kw in seen))
        self.assertEqual(len(records), 2)
        for record in records:
            first = record.rounds[0]
            self.assertEqual(sorted(_holders(first["assignments"], MULTI)), sorted(MULTI_ORDER))
            self.assertNotIn("load", first)
        with tempfile.TemporaryDirectory() as tmp:
            serial = _run(tmp)
        self.assertEqual(len(_holders(serial.rounds[0]["assignments"], MULTI)), 1)


if __name__ == "__main__":
    unittest.main()
