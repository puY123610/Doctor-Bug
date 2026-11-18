"""SUB1：「修一轮」抽成可独立调用的一步（repair_round）。

独立调用用真 FrozenAcceptor + 真派单表 + 脚本工人在 demo 副本上直接调；
父子记录、子步骤记录读 run_arm 产出的真实 RunRecord；行为对照把 run_arm
与「同一上下文逐轮直接调 repair_round」的真实结果逐项比。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import FrozenAcceptor
from core.actors import FixResult, act
from core.parallel import run_parallel
from core.policies import policy_for
from core.results import render_scorecard
from core.runner import (
    MAX_SUB_DEPTH,
    REPAIR_ROUND_STEP,
    RoundContext,
    RoundResult,
    _new_ledger,
    repair_round,
    run_arm,
)
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_VAULT = PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault"
DEMO_KB = PLATFORM_ROOT / "kb" / "dispatch_table.json"
PARALLEL_SUMMARY_KEYS = {"index", "outcome", "rounds", "final_failed"}


def _no_fix(actor_id, workspace, assigned_nodes, failed_nodes, **kwargs):
    return FixResult(actor_id=actor_id, applied=False, note="no change")


def _ctx(workspace: Path, run_actor=act, rounds=None) -> RoundContext:
    kb = DispatchKB.from_file(DEMO_KB)
    return RoundContext(
        policy=policy_for("C", kb),
        kb=kb,
        run_actor=run_actor,
        workspace=workspace,
        acceptor=FrozenAcceptor(DEMO_VAULT),
        rounds=[] if rounds is None else rounds,
        scope_denials=[],
        scope_state={"widened": False},
        widen_events=[],
        widen_applications=[],
        ledger=_new_ledger(),
        pick_one=True,
        actor_backend="scripted",
        generalist_grant="per_assignment",
    )


def _run(tmp: str, **kwargs):
    return run_arm(
        "C",
        DEMO_REPO,
        work_root=Path(tmp) / "work",
        vault_dir=DEMO_VAULT,
        kb_path=DEMO_KB,
        **kwargs,
    )


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        shutil.copytree(DEMO_REPO, self.ws)


class StandaloneTest(_Workspace):
    # 1. 子步骤可独立调用：给失败证据，返回本轮结果，输入输出字段齐全
    def test_1_callable_alone_with_full_io(self):
        ctx = _ctx(self.ws)
        verdict = ctx.acceptor.check(self.ws)
        self.assertEqual(verdict.status, "FAIL")
        before = list(verdict.failed_tests)
        result = repair_round(verdict, 1, 6, ctx)
        self.assertIsInstance(result, RoundResult)
        self.assertEqual(result.status, result.verdict.status)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(sorted(result.fixed), sorted(before))
        self.assertEqual(result.unfixed, [])
        self.assertEqual(result.new_failures, [])
        self.assertEqual(result.stopped, "")
        self.assertEqual(len(ctx.rounds), 1)
        self.assertEqual(ctx.rounds[0]["failed_tests"], before)

    def test_1b_unfixed_round_reports_unfixed(self):
        ctx = _ctx(self.ws, run_actor=_no_fix)
        verdict = ctx.acceptor.check(self.ws)
        result = repair_round(verdict, 1, 6, ctx)
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.fixed, [])
        self.assertEqual(sorted(result.unfixed), sorted(verdict.failed_tests))


class ParentChildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "core.runner.act", side_effect=_no_fix
        ):
            cls.record = _run(tmp, max_rounds=2)
        cls.payload = json.loads(json.dumps(cls.record.to_dict()))

    # 2. 父子记录：父记录每轮挂子记录，子记录回指父记录第几轮，形状沿用 parallel 摘要
    def test_2_parent_child_links_with_parallel_summary_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            par = run_parallel(
                "C", DEMO_REPO, attempts=1, work_root=Path(tmp) / "w",
                vault_dir=DEMO_VAULT, kb_path=DEMO_KB,
            )
        self.assertEqual(set(par.attempts[0]), PARALLEL_SUMMARY_KEYS)
        self.assertEqual(len(self.payload["rounds"]), 2)
        for parent_round in self.payload["rounds"]:
            sub = parent_round["sub"]
            self.assertTrue(PARALLEL_SUMMARY_KEYS <= set(sub))
            self.assertEqual(sub["step"], REPAIR_ROUND_STEP)
            self.assertEqual(sub["parent_round"], parent_round["round"])
            self.assertEqual(sub["index"], parent_round["round"])
            self.assertEqual(sub["depth"], 1)
            self.assertEqual(sub["rounds"], 1)
        # 父记录顶层键集不变（子记录不上顶层）
        self.assertNotIn("sub", self.payload)
        card = "\n".join(render_scorecard(self.payload))
        self.assertIn("每一轮都由「修一轮」这一步完成：第 1 轮", card)
        self.assertIn("第 2 轮", card)

    # 5. 子步骤自己的记录：派单、判卷、耗时都在子记录里，能读回
    def test_5_sub_record_has_dispatch_verdict_elapsed(self):
        rounds = self.payload["rounds"]
        for i, parent_round in enumerate(rounds):
            sub = parent_round["sub"]
            self.assertEqual(sub["dispatch"], parent_round["assignments"])
            next_failed = (
                rounds[i + 1]["failed_tests"]
                if i + 1 < len(rounds)
                else self.payload["final_failed"]
            )
            self.assertEqual(sub["final_failed"], len(next_failed))
            self.assertEqual(sub["verdict"]["failed_count"], len(next_failed))
            self.assertEqual(sub["outcome"], sub["verdict"]["status"])
            self.assertIsInstance(sub["elapsed_s"], float)
            self.assertGreaterEqual(sub["elapsed_s"], 0.0)
        self.assertEqual(rounds[-1]["sub"]["outcome"], "FAIL")


class ParityTest(unittest.TestCase):
    """3. 主流程行为不变：run_arm 与同一上下文逐轮直接调 repair_round 一致。"""

    def _manual(self, workspace: Path, run_actor, max_rounds: int):
        ctx = _ctx(workspace, run_actor=run_actor)
        verdict = ctx.acceptor.check(workspace)
        while verdict.status == "FAIL" and len(ctx.rounds) < max_rounds:
            verdict = repair_round(
                verdict, len(ctx.rounds) + 1, max_rounds, ctx
            ).verdict
        return ctx.rounds, verdict

    def test_3_same_rounds_dispatch_and_verdicts(self):
        for name, actor, max_rounds in (
            ("fixes", act, 6),
            ("no_fix", _no_fix, 2),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                with mock.patch("core.runner.act", side_effect=actor):
                    record = _run(tmp, max_rounds=max_rounds)
                ws = Path(tmp) / "manual"
                shutil.copytree(DEMO_REPO, ws)
                rounds, verdict = self._manual(ws, actor, max_rounds)
                self.assertEqual(len(record.rounds), len(rounds))
                for got, want in zip(record.rounds, rounds):
                    self.assertEqual(got["assignments"], want["assignments"])
                    self.assertEqual(got["failed_tests"], want["failed_tests"])
                    self.assertEqual(got["status"], want["status"])
                    self.assertEqual(
                        got["sub"]["verdict"]["status"],
                        want["sub"]["verdict"]["status"],
                    )
                self.assertEqual(record.final_failed, list(verdict.failed_tests))
                expected = {"fixes": "PASS", "no_fix": "CAPPED"}[name]
                self.assertEqual(record.outcome, expected)


class DepthLimitTest(_Workspace):
    # 4. 层数上限：超过即停下并记一行，不派单、不判卷、不死循环
    def test_4_depth_over_limit_stops_and_logs(self):
        self.assertEqual(MAX_SUB_DEPTH, 1)
        calls: list[str] = []

        def spy(actor_id, *args, **kwargs):
            calls.append(actor_id)
            return FixResult(actor_id=actor_id, applied=False, note="x")

        ctx = _ctx(self.ws, run_actor=spy)
        verdict = ctx.acceptor.check(self.ws)
        result = repair_round(verdict, 1, 6, ctx, depth=MAX_SUB_DEPTH + 1)
        self.assertTrue(result.stopped)
        self.assertIn("层数上限", result.stopped)
        self.assertIs(result.verdict, verdict)
        self.assertEqual(calls, [])
        self.assertEqual(ctx.rounds, [])
        self.assertEqual(
            ctx.stops,
            [
                {
                    "step": REPAIR_ROUND_STEP,
                    "parent_round": 1,
                    "depth": 2,
                    "stopped": result.stopped,
                }
            ],
        )

    def test_4b_round_over_cap_stops_and_logs(self):
        ctx = _ctx(self.ws, run_actor=_no_fix)
        verdict = ctx.acceptor.check(self.ws)
        result = repair_round(verdict, 3, 2, ctx)
        self.assertIn("轮数上限", result.stopped)
        self.assertEqual(ctx.rounds, [])
        self.assertEqual(len(ctx.stops), 1)


if __name__ == "__main__":
    unittest.main()
