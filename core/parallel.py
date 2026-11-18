# -*- coding: utf-8 -*-
"""PAR1：并行修复尝试与判卷择优。

同一工作区同时跑 N 份互相隔离的尝试：每份有自己的工作根目录与
档案副本（金库与派单表只读共享），全部跑完后按冻结判卷结果择优
（PASS 优先，其次轮次少／残留失败少）。胜出者的员工记忆才合并回
真实档案；胜出者的记录走现有结果包流程。门禁、辖区围栏与判卷
逻辑一概不改——隔离由既有的工作副本机制保证。
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from core.runner import RunRecord, run_arm


def select_winner(records: list[RunRecord]) -> int:
    """返回胜出记录的下标：PASS 优先（先比轮次），否则残留失败少优先。"""

    def key(item: tuple[int, RunRecord]) -> tuple:
        index, record = item
        if record.outcome == "PASS":
            return (0, len(record.rounds), len(record.final_failed), index)
        return (1, len(record.final_failed), len(record.rounds), index)

    return min(enumerate(records), key=key)[0]


@dataclass
class ParallelResult:
    record: RunRecord
    workspaces: list[str]
    attempts: list[dict]


def _merge_winner_memory(winner_profile: Path, real_profile: Path) -> None:
    """只把胜出者档案里的员工 MEMORY.md 覆盖回真实档案，其余文件不碰。"""
    src_agents = winner_profile / "agents"
    if not src_agents.is_dir():
        return
    for memory in sorted(src_agents.glob("*/MEMORY.md")):
        target = real_profile / "agents" / memory.parent.name / "MEMORY.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(memory, target)


def run_parallel(
    arm: str,
    workspace_src,
    *,
    attempts: int = 2,
    kb_enabled: bool = True,
    max_rounds: int = 6,
    work_root=Path("runs/work"),
    actor_backend: str = "scripted",
    on_event=None,
    vault_dir=None,
    kb_path=None,
    profile_dir=None,
    inventory: dict | None = None,
    generalist_grant: str = "per_assignment",
    run_one=None,
) -> ParallelResult:
    if not isinstance(attempts, int) or attempts < 1:
        raise ValueError(f"attempts 必须是 >= 1 的整数，收到 {attempts!r}")
    run_one = run_one or run_arm
    from core import usage as _usage

    # USAGE1：整池用量改读统一累计器（core.usage），不再读老计数器 _USAGE。
    _pool_before = _usage.call_ids()
    workspace_src = Path(workspace_src)
    work_root = Path(work_root)
    relay_lock = threading.Lock()
    started: list[list[str]] = [[] for _ in range(attempts)]

    def make_sink(index: int):
        def sink(event: dict) -> None:
            if event.get("event") == "run_started":
                started[index].append(str(event.get("workspace", "")))
                return
            if on_event is not None:
                with relay_lock:
                    on_event(event)

        return sink

    def attempt(index: int):
        attempt_work_root = work_root / f"par-{index}"
        attempt_profile = None
        if profile_dir is not None:
            attempt_profile = attempt_work_root / "profile"
            if attempt_profile.exists():
                shutil.rmtree(attempt_profile)
            shutil.copytree(Path(profile_dir), attempt_profile)
        record = run_one(
            arm,
            workspace_src,
            kb_enabled=kb_enabled,
            max_rounds=max_rounds,
            work_root=attempt_work_root,
            actor_backend=actor_backend,
            on_event=make_sink(index),
            vault_dir=vault_dir,
            kb_path=kb_path,
            roster=None,
            profile_dir=attempt_profile,
            inventory=inventory,
            generalist_grant=generalist_grant,
            pick_one=False,
        )
        return record, attempt_profile

    records: list[RunRecord | None] = [None] * attempts
    profiles: list[Path | None] = [None] * attempts
    errors: dict[int, Exception] = {}
    with ThreadPoolExecutor(max_workers=attempts) as pool:
        futures = {pool.submit(attempt, i): i for i in range(attempts)}
        for future, index in futures.items():
            try:
                records[index], profiles[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单份失败不拖垮其它尝试
                errors[index] = exc
    if len(errors) == attempts:
        raise errors[min(errors)]

    summaries: list[dict] = []
    for i in range(attempts):
        if i in errors:
            exc = errors[i]
            summaries.append(
                {
                    "index": i,
                    "outcome": "ERROR",
                    "rounds": 0,
                    "final_failed": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            record_i = records[i]
            summaries.append(
                {
                    "index": i,
                    "outcome": record_i.outcome,
                    "rounds": len(record_i.rounds),
                    "final_failed": len(record_i.final_failed),
                }
            )

    ok_indices = [i for i in range(attempts) if i not in errors]
    winner = ok_indices[select_winner([records[i] for i in ok_indices])]
    record = records[winner]
    record.parallel = {
        "attempts": attempts,
        "winner_index": winner,
        "outcomes": [s["outcome"] for s in summaries],
    }
    _pool_delta = _usage.run_usage_dict(_usage.accumulator_since(_pool_before))
    if _pool_delta:
        record.usage = _pool_delta
    if profile_dir is not None and profiles[winner] is not None:
        _merge_winner_memory(profiles[winner], Path(profile_dir))
    if on_event is not None:
        for workspace in started[winner]:
            on_event({"event": "run_started", "workspace": workspace})
    return ParallelResult(
        record=record,
        workspaces=list(started[winner]),
        attempts=summaries,
    )
