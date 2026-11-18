"""VAR1：失败分类器 + 按类别给派单任务说明拼对症提醒段。

分类断言读 acceptor.failure_kinds 对真实 pytest 输出的分类结果（样本由
测试现场跑 pytest 生成）；拼接断言读真后端生成的真实任务说明：llm 后端
用一个只记录参数的假 opencode 抓 prompt，agentscope 后端读 FakeChatModel
收到的输入。判卷断言走 FrozenAcceptor.check 的现行判定路径。
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import FrozenAcceptor
from acceptor.failure_kinds import (
    ASSERTION,
    IMPORT,
    OTHER,
    TIMEOUT,
    UNCLASSIFIED,
    classify_pytest_failures,
)
from acceptor.parsers import parse_pytest
from agentscope_ext.fake_model import FakeChatModel
from core.agentscope_actors import agentscope_act, roster_from_table
from core.llm_actors import build_prompt
from core.llm_actors import llm_act as REAL_LLM_ACT
from core.results import render_scorecard
from core.runner import run_arm
from core.variants import VARIANTS, plan_variants, render_variant_note

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_VAULT = PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault"
DEMO_KB = PLATFORM_ROOT / "kb" / "dispatch_table.json"

SAMPLE_TESTS = textwrap.dedent(
    """\
    import pytest

    def test_assert_plain():
        assert 1 == 2

    def test_assert_msg():
        assert [1] == [2], "mismatch"

    def test_import():
        import no_such_pkg_var1_xyz

    def test_timeout():
        raise TimeoutError("took too long")

    def test_other():
        {}["k"]

    class TestK:
        def test_m(self):
            raise ValueError("bad")
    """
)
N = "tests/test_s.py::"
EXPECTED = {
    N + "test_assert_plain": ASSERTION,
    N + "test_assert_msg": ASSERTION,
    N + "test_import": IMPORT,
    N + "test_timeout": TIMEOUT,
    N + "test_other": OTHER,
    N + "TestK::test_m": OTHER,
}

ASSERT_SEG = VARIANTS[ASSERTION][1]
IMPORT_SEG = VARIANTS[IMPORT][1]
MARK = "【对症提醒"


def _real_pytest_output() -> str:
    """现场跑一次 pytest -q（与判卷默认命令同参数），返回真实输出。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tests").mkdir()
        (root / "tests" / "test_s.py").write_text(SAMPLE_TESTS, encoding="utf-8")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTEST_ADDOPTS", None)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
        )
        return completed.stdout + completed.stderr


def _fake_opencode(directory: Path) -> Path:
    """只把最后一个参数（prompt）落盘的假 opencode，不改任何文件。"""
    script = directory / "opencode"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"d = pathlib.Path({str(directory / 'prompts')!r}); d.mkdir(exist_ok=True)\n"
        "n = len(list(d.iterdir()))\n"
        "(d / f'{n:03d}.txt').write_text(sys.argv[-1], encoding='utf-8')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _prompts(directory: Path) -> list[str]:
    folder = directory / "prompts"
    if not folder.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())]


class ClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = _real_pytest_output()

    # 1. 四类逐条归类
    def test_1_four_kinds_from_real_pytest_output(self):
        parsed = parse_pytest(self.output, 1)
        self.assertEqual(sorted(parsed.failed_tests), sorted(EXPECTED))
        self.assertEqual(parsed.failure_kinds, EXPECTED)
        self.assertEqual(
            classify_pytest_failures(self.output, parsed.failed_tests), EXPECTED
        )

    def test_1b_long_traceback_fallback_when_summary_has_no_message(self):
        # 去掉短摘要里的 " - 消息"，只剩长回溯段末尾的「路径:行号: 异常名」
        stripped = "\n".join(
            line.split(" - ", 1)[0] if line.startswith("FAILED ") else line
            for line in self.output.splitlines()
        )
        self.assertEqual(
            classify_pytest_failures(stripped, list(EXPECTED)), EXPECTED
        )

    # 2. 抽不到 → 未分类，不抛错
    def test_2_unextractable_is_unclassified_without_error(self):
        output = (
            "F                                                    [100%]\n"
            "FAILED tests/test_q.py::test_a\n"
            "FAILED tests/test_q.py::test_b - something odd happened\n"
            "2 failed in 0.01s\n"
        )
        parsed = parse_pytest(output, 1)
        self.assertEqual(
            parsed.failure_kinds,
            {
                "tests/test_q.py::test_a": UNCLASSIFIED,
                "tests/test_q.py::test_b": UNCLASSIFIED,
            },
        )
        self.assertEqual(
            classify_pytest_failures("", ["tests/x.py::t"]),
            {"tests/x.py::t": UNCLASSIFIED},
        )
        self.assertEqual(plan_variants(["tests/x.py::t"], {}, [])[0]["variant"], "")


class SpliceTest(unittest.TestCase):
    """3. 派单任务说明：断言错拼断言段、导入失败拼导入段，未分类不拼。"""

    @classmethod
    def setUpClass(cls):
        cls.kinds = parse_pytest(_real_pytest_output(), 1).failure_kinds
        cls.kinds["tests/test_q.py::test_a"] = UNCLASSIFIED

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.workspace = self.tmp / "ws"
        shutil.copytree(DEMO_REPO, self.workspace)
        self.opencode = _fake_opencode(self.tmp)

    def _llm_prompt(self, nodes: list[str]) -> str:
        note = render_variant_note(plan_variants(nodes, self.kinds, []))
        kwargs = {"variant_note": note} if note else {}
        REAL_LLM_ACT(
            "billing-specialist",
            self.workspace,
            nodes,
            nodes,
            opencode_path=self.opencode,
            timeout_s=30,
            **kwargs,
        )
        return _prompts(self.tmp)[-1]

    def test_3_llm_task_text_gets_segment_by_kind(self):
        a, i = N + "test_assert_plain", N + "test_import"
        prompt = self._llm_prompt([a, i])
        self.assertIn(ASSERT_SEG, prompt)
        self.assertIn(IMPORT_SEG, prompt)
        self.assertNotIn(VARIANTS[TIMEOUT][1], prompt)
        self.assertLess(prompt.index(ASSERT_SEG), prompt.index(IMPORT_SEG))

    def test_3b_unclassified_and_other_get_no_segment(self):
        nodes = ["tests/test_q.py::test_a", N + "test_other"]
        prompt = self._llm_prompt(nodes)
        self.assertNotIn(MARK, prompt)
        self.assertEqual(prompt, build_prompt("billing-specialist", nodes, nodes))

    def test_3c_agentscope_task_text_gets_segment(self):
        roster = roster_from_table(json.loads(DEMO_KB.read_text(encoding="utf-8")))
        nodes = [N + "test_assert_plain"]
        note = render_variant_note(plan_variants(nodes, self.kinds, []))
        model = FakeChatModel(replies=["nothing to change"])
        agentscope_act(
            "billing-specialist",
            self.workspace,
            nodes,
            nodes,
            roster=roster,
            model=model,
            timeout_s=30,
            variant_note=note,
        )
        self.assertIn(ASSERT_SEG, model.calls[0]["input_text"])
        bare = FakeChatModel(replies=["nothing to change"])
        agentscope_act(
            "billing-specialist",
            self.workspace,
            nodes,
            nodes,
            roster=roster,
            model=bare,
            timeout_s=30,
        )
        self.assertNotIn(MARK, bare.calls[0]["input_text"])


class RunRecordTest(unittest.TestCase):
    """4/5. 真 run_arm（llm 后端 + 记录 prompt 的假 opencode，修不好 → 2 轮）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        opencode = _fake_opencode(cls.tmp)
        with mock.patch(
            "core.llm_actors.llm_act",
            functools.partial(REAL_LLM_ACT, opencode_path=opencode, timeout_s=30),
        ):
            cls.record = run_arm(
                "C",
                DEMO_REPO,
                max_rounds=2,
                work_root=cls.tmp / "work",
                actor_backend="llm",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )
        cls.prompts = _prompts(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_4_each_round_logs_node_kind_variant_and_reads_back(self):
        self.assertEqual(self.record.outcome, "CAPPED")
        payload = json.loads(json.dumps(self.record.to_dict()))
        first = payload["rounds"][0]
        assigned = sorted(
            {n for nodes in first["assignments"].values() for n in nodes}
        )
        logged = sorted({e["node"] for e in first["variants"]})
        self.assertEqual(logged, assigned)
        for entry in first["variants"]:
            self.assertEqual(entry["kind"], first["failure_kinds"][entry["node"]])
            expected = VARIANTS[entry["kind"]][0] if entry["kind"] in VARIANTS else ""
            self.assertEqual(entry["variant"], expected)
            self.assertIn("actor_id", entry)
        # demo_repo 首验：多数是断言错（拼断言段），退款权限那条是
        # 「其他异常」（不拼段、variant 为空）——都按真实分类记账。
        kinds = {e["kind"] for e in first["variants"]}
        self.assertIn(ASSERTION, kinds)
        other = [e for e in first["variants"] if e["kind"] not in VARIANTS]
        self.assertTrue(other)
        self.assertTrue(all(e["variant"] == "" for e in other))
        # 每个分到断言错节点的员工，真实任务说明里都带断言段
        self.assertTrue(self.prompts)
        with_assert = {
            e["actor_id"] for e in first["variants"] if e["kind"] == ASSERTION
        }
        self.assertEqual(len(self.prompts), 2 * len(first["assignments"]))
        hits = [p for p in self.prompts if ASSERT_SEG in p]
        self.assertEqual(len(hits), 2 * len(with_assert))
        for prompt in hits:
            seg_part = prompt.split(ASSERT_SEG, 1)[1]
            for entry in other:
                if entry["node"] not in [
                    e["node"] for e in first["variants"] if e["variant"]
                ]:
                    self.assertNotIn(entry["node"], seg_part.split("【硬约束】")[0])
        card = "\n".join(render_scorecard(payload))
        self.assertIn("派单时加的对症提醒：断言错的", card)
        self.assertIn("「断言对照」段", card)

    def test_5_same_failure_next_round_keeps_same_segment(self):
        rounds = self.record.rounds
        self.assertEqual(len(rounds), 2)
        first = {e["node"]: e["variant"] for e in rounds[0]["variants"]}
        for entry in rounds[1]["variants"]:
            if entry["node"] in first:
                self.assertEqual(entry["variant"], first[entry["node"]])
                if entry["variant"]:
                    self.assertEqual(entry["carried_from"], 1)
                else:
                    self.assertNotIn("carried_from", entry)
        self.assertTrue(any(e.get("carried_from") == 1 for e in rounds[1]["variants"]))

    def test_5b_kind_changes_but_segment_is_not_silently_swapped(self):
        node = N + "test_x"
        prior = [{"round": 1, "variants": [{"node": node, "kind": ASSERTION, "variant": "断言对照"}]}]
        entry = plan_variants([node], {node: IMPORT}, prior)[0]
        self.assertEqual(entry, {"node": node, "kind": IMPORT, "variant": "断言对照", "carried_from": 1})
        self.assertIn(ASSERT_SEG, render_variant_note([entry]))
        self.assertNotIn(IMPORT_SEG, render_variant_note([entry]))


def _acceptor(output: str, returncode: int) -> FrozenAcceptor:
    acceptor = FrozenAcceptor.__new__(FrozenAcceptor)
    acceptor.manifest = {"kind": "behavior", "files": {}}
    acceptor.parser = parse_pytest
    acceptor._run_check = lambda workspace: (returncode, output)  # type: ignore[method-assign]
    return acceptor


class VerdictUnchangedTest(unittest.TestCase):
    """6. 分类与拼接不影响判卷：同一批样本 PASS/FAIL 与现行公式一致。"""

    def test_6_status_and_to_dict_unchanged(self):
        real = _real_pytest_output()
        samples = [
            (real, 1),
            ("...   [100%]\n3 passed in 0.01s\n", 0),
            ("F\nFAILED tests/test_q.py::test_a\n1 failed in 0.01s\n", 1),
            (".\n1 passed in 0.01s\n", 1),
        ]
        for output, code in samples:
            with self.subTest(code=code, head=output[:20]):
                verdict = _acceptor(output, code).check(Path("."))
                parsed = parse_pytest(output, code)
                formula = (
                    "PASS"
                    if code == 0 and parsed.failed_count == 0 and not parsed.failed_tests
                    else "FAIL"
                )
                self.assertEqual(verdict.status, formula)
                self.assertEqual(verdict.failure_kinds, parsed.failure_kinds)
                self.assertNotIn("failure_kinds", verdict.to_dict())
                self.assertEqual(
                    [e.node_id for e in verdict.evidence], parsed.failed_tests
                )

    def test_6b_scripted_run_outcome_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_arm(
                "C",
                DEMO_REPO,
                max_rounds=6,
                work_root=Path(tmp) / "work",
                vault_dir=DEMO_VAULT,
                kb_path=DEMO_KB,
            )
        self.assertEqual(record.outcome, "PASS")
        self.assertTrue(record.rounds[0]["variants"])


if __name__ == "__main__":
    unittest.main()
