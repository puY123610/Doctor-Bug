# -*- coding: utf-8 -*-
"""PAR1: parallel attempts, verdict selection, profile isolation."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parallel import run_parallel, select_winner
from core.runner import RunRecord

PLATFORM = Path(__file__).resolve().parents[1]


def _record(outcome: str, rounds: int, final_failed: int) -> RunRecord:
    return RunRecord(
        arm="C",
        kb_enabled=True,
        outcome=outcome,
        rounds=[{"round": i + 1} for i in range(rounds)],
        final_failed=[f"t{i}" for i in range(final_failed)],
    )


class SelectWinnerTests(unittest.TestCase):
    def test_pass_with_fewer_rounds_wins(self) -> None:
        records = [
            _record("PASS", 3, 0),
            _record("PASS", 1, 0),
            _record("FAIL", 1, 0),
        ]
        self.assertEqual(select_winner(records), 1)

    def test_no_pass_fewest_final_failed_wins(self) -> None:
        records = [_record("FAIL", 2, 5), _record("FAIL", 4, 2)]
        self.assertEqual(select_winner(records), 1)

    def test_tie_takes_lowest_index(self) -> None:
        records = [_record("PASS", 1, 0), _record("PASS", 1, 0)]
        self.assertEqual(select_winner(records), 0)


class RecordShapeTests(unittest.TestCase):
    def test_parallel_key_only_when_set(self) -> None:
        plain = _record("PASS", 1, 0)
        self.assertNotIn("parallel", plain.to_dict())
        plain.parallel = {"attempts": 2, "winner_index": 0}
        self.assertEqual(
            plain.to_dict()["parallel"], {"attempts": 2, "winner_index": 0}
        )


class RunParallelFakeTests(unittest.TestCase):
    def _fake_factory(self, plan: dict, seen_profiles: list):
        def fake(arm, workspace_src, **kwargs):
            profile = kwargs.get("profile_dir")
            seen_profiles.append(profile)
            index = int(str(kwargs["work_root"]).rsplit("par-", 1)[1])
            spec = plan[index]
            if spec == "raise":
                raise RuntimeError("boom")
            if profile is not None:
                marker = profile / "agents" / "w1" / "MEMORY.md"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"winner-{index}\n", encoding="utf-8")
            if kwargs.get("on_event") is not None:
                kwargs["on_event"](
                    {"event": "run_started", "workspace": f"/ws/{index}"}
                )
            return _record(*spec)

        return fake

    def test_isolation_merge_and_annotation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="par1-") as tmp:
            root = Path(tmp)
            real_profile = root / "profile"
            (real_profile / "agents" / "w1").mkdir(parents=True)
            (real_profile / "agents" / "w1" / "MEMORY.md").write_text(
                "original\n", encoding="utf-8"
            )
            (real_profile / "dispatch_table.json").write_text(
                "{}", encoding="utf-8"
            )
            plan = {
                0: ("FAIL", 2, 3),
                1: ("PASS", 1, 0),
                2: ("PASS", 3, 0),
            }
            seen: list = []
            result = run_parallel(
                "C",
                root / "ws",
                attempts=3,
                work_root=root / "work",
                profile_dir=real_profile,
                run_one=self._fake_factory(plan, seen),
            )
            self.assertEqual(result.record.outcome, "PASS")
            self.assertEqual(
                result.record.parallel["winner_index"], 1
            )
            self.assertEqual(
                result.record.parallel["outcomes"],
                ["FAIL", "PASS", "PASS"],
            )
            self.assertEqual(result.workspaces, ["/ws/1"])
            self.assertEqual(len({str(p) for p in seen}), 3)
            merged = (
                real_profile / "agents" / "w1" / "MEMORY.md"
            ).read_text(encoding="utf-8")
            self.assertEqual(merged, "winner-1\n")
            self.assertEqual(
                (real_profile / "dispatch_table.json").read_text(
                    encoding="utf-8"
                ),
                "{}",
            )

    def test_attempts_run_concurrently(self) -> None:
        def slow(arm, workspace_src, **kwargs):
            time.sleep(0.3)
            return _record("PASS", 1, 0)

        with tempfile.TemporaryDirectory(prefix="par1-") as tmp:
            start = time.monotonic()
            run_parallel(
                "C",
                Path(tmp) / "ws",
                attempts=3,
                work_root=Path(tmp) / "work",
                run_one=slow,
            )
            self.assertLess(time.monotonic() - start, 0.75)

    def test_one_error_does_not_sink_others(self) -> None:
        with tempfile.TemporaryDirectory(prefix="par1-") as tmp:
            root = Path(tmp)
            plan = {0: "raise", 1: ("PASS", 1, 0)}
            seen: list = []
            result = run_parallel(
                "C",
                root / "ws",
                attempts=2,
                work_root=root / "work",
                run_one=self._fake_factory(plan, seen),
            )
            self.assertEqual(result.record.outcome, "PASS")
            self.assertEqual(result.attempts[0]["outcome"], "ERROR")
            self.assertIn("boom", result.attempts[0]["error"])


class RunParallelRealSmokeTests(unittest.TestCase):
    def test_demo_repo_scripted_two_attempts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="par1-real-") as tmp:
            result = run_parallel(
                "C",
                PLATFORM / "tests" / "fixtures" / "demo_repo",
                attempts=2,
                max_rounds=3,
                work_root=Path(tmp) / "work",
                actor_backend="scripted",
                vault_dir=PLATFORM / "tests" / "fixtures" / "acceptor_vault",
                kb_path=PLATFORM / "kb" / "dispatch_table.json",
            )
            self.assertEqual(result.record.outcome, "PASS")
            self.assertTrue(result.workspaces)
            self.assertEqual(
                result.record.parallel["outcomes"], ["PASS", "PASS"]
            )


if __name__ == "__main__":
    unittest.main()
