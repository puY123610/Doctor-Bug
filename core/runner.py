"""Main dispatch loop: copy a workspace, accept, dispatch, repair, repeat."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from acceptor.acceptor import FrozenAcceptor, Verdict
from acceptor.evidence import evidence_from_failed, module_from_node_id
from kb.lookup import DispatchKB

from .variants import plan_variants, render_variant_note

from .actors import act
from .policies import policy_for

_log = logging.getLogger(__name__)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROFILES_ROOT = PLATFORM_ROOT / "profiles"
# `.drbug` is the per-project state directory (profiles/runs/bundles); never
# copy it into a work tree, or each run would duplicate its own state.
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", ".drbug", "*.pyc"
)


def _baseline_hashes(root: Path) -> dict[str, str]:
    """Per-file sha256 of the source tree, taken before the copy.

    Mirrors ``shutil.copytree(..., ignore=_COPY_IGNORE)``: the ignore
    callable is consulted per directory with the same argument shape
    copytree uses, so ignored names are neither copied nor recorded.
    Keys are POSIX paths relative to *root*. Files a patch later
    creates are absent by construction: absence means "did not exist
    at baseline time".
    """
    hashes: dict[str, str] = {}

    def walk(directory: Path) -> None:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
        ignored = _COPY_IGNORE(str(directory), [item.name for item in children])
        skipped = set(ignored) if ignored else set()
        for child in children:
            if child.name in skipped:
                continue
            if child.is_dir():
                walk(child)
            elif child.is_file():
                digest = hashlib.sha256()
                with child.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                hashes[child.relative_to(root).as_posix()] = digest.hexdigest()

    walk(root)
    return hashes


def profile_paths(profile_dir: str | Path) -> dict[str, Path]:
    """Resolve the three consumables of a profile directory.

    A generated profile follows the same layout as ``acceptor/vault`` and
    ``kb/dispatch_table.json``; the native roster stays inside the dispatch
    table's optional ``roster`` section, so no fourth file is needed.
    """
    base = Path(profile_dir)
    return {
        "profile_dir": base,
        "vault_dir": base / "vault",
        "kb_path": base / "dispatch_table.json",
    }


def load_inventory(profile_dir: str | Path | None) -> dict | None:
    """Read the optional ``inventory`` field from ``profile.json``.

    Older profiles (GEN2 outputs, hand-written profile directories) do not
    have the field; ``None`` then keeps the pre-GEN3 actor behaviour.  A
    missing or unreadable ``profile.json`` is never an error: dynamic teaming
    is an enrichment, not a requirement for a run.
    """
    if profile_dir is None:
        return None
    path = Path(profile_dir) / "profile.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    inventory = (
        metadata.get("inventory") if isinstance(metadata, dict) else None
    )
    return inventory if isinstance(inventory, dict) and inventory else None


def load_origin(profile_dir: str | Path | None) -> str:
    """Read the optional ``origin`` field from ``profile.json``.

    ``build_profile`` writes ``"tests"`` and ``freeze_repro`` writes
    ``"repro"``.  Every other case (hand-written profiles, GEN2/GEN4 outputs,
    unreadable metadata) is treated as ``"tests"`` so the GEN5 guard stays
    off and the old behaviour is preserved byte-for-byte.
    """
    if profile_dir is None:
        return "tests"
    path = Path(profile_dir) / "profile.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "tests"
    origin = metadata.get("origin") if isinstance(metadata, dict) else None
    return origin if isinstance(origin, str) and origin else "tests"


def _table_scope_files(
    table: dict,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Evidence files per node and per evidence module, from a dispatch table.

    ``node_files`` is the union of the files of a node override's evidence
    specialists; ``module_files`` does the same for ``module_defaults``.  The
    strict repro guard narrows a whole-package fallback generalist with these
    instead of letting it rewrite the package.
    """
    files_by_specialist = {
        str(item.get("id")): tuple(item.get("files", ()))
        for item in table.get("specialists", [])
        if isinstance(item, dict)
    }

    def union(actor_ids) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    path
                    for actor_id in actor_ids
                    for path in files_by_specialist.get(str(actor_id), ())
                }
            )
        )

    node_files: dict[str, tuple[str, ...]] = {}
    overrides = table.get("node_overrides", {})
    if isinstance(overrides, dict):
        for node, override in overrides.items():
            if not isinstance(override, dict):
                continue
            files = union(override.get("specialists", ()))
            if files:
                node_files[str(node)] = files
    module_files: dict[str, tuple[str, ...]] = {}
    defaults = table.get("module_defaults", {})
    if isinstance(defaults, dict):
        for module, actor_ids in defaults.items():
            if not isinstance(actor_ids, (list, tuple)):
                continue
            files = union(actor_ids)
            if files:
                module_files[str(module)] = files
    return node_files, module_files


def _actor_scope_union(
    table: dict,
    rounds: list[dict],
    node_files: dict | None,
    module_files: dict | None,
) -> set[str]:
    """Union of the files every dispatched actor was allowed to touch.

    Roster files come from the dispatch table (both shapes, mirroring
    ``_roster_files`` but from the in-memory table); evidence files
    come from ``evidence_scope_files`` over each round's assignments.
    Used by the secretary review (BRIEF1c) as the jurisdiction union.
    """
    from .agentscope_actors import evidence_scope_files

    files_by_actor: dict[str, set[str]] = {}
    roster = table.get("roster")
    if isinstance(roster, dict):
        specs = roster.get("specialists")
        if isinstance(specs, dict):
            for name, spec in specs.items():
                if isinstance(spec, dict):
                    files_by_actor.setdefault(str(name), set()).update(
                        str(item) for item in spec.get("files") or []
                    )
    for item in table.get("specialists") or []:
        if isinstance(item, dict) and item.get("id"):
            files_by_actor.setdefault(str(item["id"]), set()).update(
                str(path) for path in item.get("files") or []
            )
    union: set[str] = set()
    for round_record in rounds:
        assignments = round_record.get("assignments") or {}
        for actor_id, nodes in assignments.items():
            union.update(files_by_actor.get(str(actor_id), set()))
            union.update(
                evidence_scope_files(list(nodes), node_files, module_files)
            )
    return union


@dataclass
class RunRecord:
    arm: str
    kb_enabled: bool
    outcome: str
    rounds: list[dict] = field(default_factory=list)
    final_failed: list[str] = field(default_factory=list)
    scope_denials: list[dict] = field(default_factory=list)
    baseline_hashes: dict = field(default_factory=dict)
    generalist_scope: dict = field(default_factory=dict)
    review: dict = field(default_factory=dict)
    parallel: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    # EVD1：终验有 error 时才带（键集照 PAR1/usage 先例条件出现，
    # 无 error 的记录键集逐字不变）。
    final_errors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "arm": self.arm,
            "kb_enabled": self.kb_enabled,
            "outcome": self.outcome,
            "rounds": [dict(round_record) for round_record in self.rounds],
            "final_failed": list(self.final_failed),
            "scope_denials": [dict(item) for item in self.scope_denials],
            "baseline_hashes": dict(self.baseline_hashes),
            "generalist_scope": dict(self.generalist_scope),
            "review": dict(self.review),
        }
        # PAR1: 仅并行运行时才带 parallel 键，普通记录键集逐字不变
        # （三处测试把记录键集钉死全等，不许改）。
        if self.parallel:
            payload["parallel"] = dict(self.parallel)
        if any(self.usage.values()):
            payload["usage"] = dict(self.usage)
        if self.final_errors:
            payload["final_errors"] = dict(self.final_errors)
        return payload


def _round_record(
    round_number: int,
    verdict: Verdict,
    assignments: dict,
    fix_results: list,
    variants: list | None = None,
) -> dict:
    return {
        "round": round_number,
        "status": verdict.status,
        "failed_tests": list(verdict.failed_tests),
        # EVD1：该轮判卷（修复前）的计数；第 1 轮即首验，成绩单据此
        # 算「原来是好的」分母。纯增量，不影响派活。
        "passed_count": verdict.passed_count,
        "failed_count": verdict.failed_count,
        "error_count": getattr(verdict, "error_count", 0),
        "error_tests": list(getattr(verdict, "error_tests", []) or []),
        "assignments": {
            actor_id: list(nodes) for actor_id, nodes in assignments.items()
        },
        "fix_results": [result.to_dict() for result in fix_results],
        # VAR1：该轮判卷的失败分类，与每个分到节点用了哪个对症提醒段
        # （每节点一行 node/kind/variant，沿用时带 carried_from）。
        # 纯增量，只读，不影响派活与判卷。
        "failure_kinds": dict(getattr(verdict, "failure_kinds", {}) or {}),
        "variants": [dict(entry) for entry in (variants or [])],
    }


_WIDEN_APPLY_RE = re.compile(r"WIDEN-APPLY:\s*(\S+)\s*\|\s*(.+)")


def _validate_widen_application(
    raw_path: str,
    *,
    workspace: Path,
    package: str,
    failed_tests: list[str],
) -> tuple[bool, str]:
    """Program-side check of one WIDEN-APPLY line (SCOPE1).

    The file must sit inside the roster package, exist in the working
    copy, and be tied to the failures the generalist was assigned: its
    module shows up in the failed evidence, or a failed test file
    references it. Anything else is recorded but never granted.
    """
    relative = raw_path.replace("\\", "/").strip()
    if relative.startswith("/") or ".." in Path(relative).parts:
        return False, "path must be a package-relative path"
    if package not in ("", ".") and not relative.startswith(
        package.rstrip("/") + "/"
    ):
        return False, "path is outside the roster package"
    if not (workspace / relative).is_file():
        return False, "path does not exist in the workspace"
    stem = Path(relative).stem
    failed_modules = {module_from_node_id(node) for node in failed_tests}
    if stem in failed_modules:
        return True, "module appears in the failed evidence"
    for node in failed_tests:
        test_file = workspace / node.split("::", 1)[0]
        try:
            text = test_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if relative in text or stem in text:
            return True, "referenced by a failed test file"
    return False, "path is unrelated to this round's failures"


def _widen_trigger(
    *,
    round_denials: list[dict],
    generalist_result,
    workspace: Path,
    package: str,
    failed_tests: list[str],
    already_widened: bool,
) -> tuple[str | None, list[dict]]:
    """Decide whether the generalist's grant widens next round (SCOPE1).

    Two signals, either is enough: the generalist hit the fence this
    round (a recorded scope denial under its name), or it filed a
    WIDEN-APPLY line that passes validation. The worker can only
    apply; this function is the only place a widening is granted, and
    at most once per run - ``already_widened`` records the application
    but never grants again. A synthesized stand-in is not the
    generalist and cannot trigger a widening.
    """
    applications: list[dict] = []
    if "[synthesized]" in generalist_result.note:
        return None, applications
    trigger: str | None = None
    if any(item.get("actor_id") == "generalist" for item in round_denials):
        trigger = "denial"
    match = _WIDEN_APPLY_RE.search(generalist_result.note)
    if match is not None:
        raw_path = match.group(1).strip()
        valid, why = _validate_widen_application(
            raw_path,
            workspace=workspace,
            package=package,
            failed_tests=failed_tests,
        )
        if already_widened:
            valid, why = False, "already widened once this run"
        applications.append(
            {
                "path": raw_path,
                "reason": match.group(2).strip(),
                "valid": valid,
                "why": why,
            }
        )
        if valid and trigger is None:
            trigger = "widen_apply"
    if already_widened:
        return None, applications
    return trigger, applications


# MEM1: at most two long-term memory lines per employee per PASS run.
_MEMORY_ENTRY_CAP = 2
_CHANGED_RE = re.compile(r"(?:^|;\s*)changed=([^;]*)")
_FIXED_RE = re.compile(r"(?:^|;\s*)fixed\s+([^;]+)")
_CAUSE_RE = re.compile(r"(?:^|;\s*)(?:reply|output)=(.*)\Z", re.DOTALL)
_SYNTH_RE = re.compile(r"\[synthesized\]\s+(\S+)")
_CAUSE_LIMIT = 180


def _note_preamble(note: str) -> str:
    match = re.search(r"(?:^|;\s*)(?:reply|output)=", note)
    if match is None:
        return note
    return note[: match.start()]


def _cause_line(note: str) -> str:
    """The actor's own one-line words, or "" when the record has none.

    Only ``reply=`` / ``output=`` count. A scripted ``fixed billing`` note
    has no root-cause sentence and must not be paraphrased into one.
    """
    match = _CAUSE_RE.search(note)
    if match is None:
        return ""
    text = " ".join(match.group(1).replace("`", "'").split())
    if not text:
        return ""
    if len(text) > _CAUSE_LIMIT:
        return text[:_CAUSE_LIMIT] + "…"
    return text


def _changed_paths(note: str) -> list[str]:
    match = _CHANGED_RE.search(_note_preamble(note))
    if match is None:
        return []
    raw = match.group(1).strip()
    if not raw or raw == "none":
        return []
    paths: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if item and item != "none" and item not in paths:
            paths.append(item)
    return paths


def _fixed_domains(note: str) -> list[str]:
    match = _FIXED_RE.search(_note_preamble(note))
    if match is None:
        return []
    domains: list[str] = []
    for part in match.group(1).split(","):
        item = part.strip()
        if item and item not in domains:
            domains.append(item)
    return domains


def _memory_owner(actor_id: str, note: str) -> str:
    match = _SYNTH_RE.search(note)
    if match is not None:
        return match.group(1)
    if "[synthesis-miss]" in note:
        return "generalist"
    return actor_id


def _roster_files(profile_dir: Path) -> dict[str, list[str]]:
    """actor id -> roster files, from the profile dispatch table. Best effort."""
    try:
        table = json.loads(
            (profile_dir / "dispatch_table.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    if not isinstance(table, dict):
        return {}
    found: dict[str, list[str]] = {}
    roster = table.get("roster")
    if isinstance(roster, dict):
        specs = roster.get("specialists")
        if isinstance(specs, dict):
            for name, spec in specs.items():
                if isinstance(spec, dict):
                    files = spec.get("files") or []
                    found[str(name)] = [str(item) for item in files]
    for item in table.get("specialists") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        actor = str(item["id"])
        if actor not in found:
            found[actor] = [str(path) for path in item.get("files") or []]
    return found


def _files_touched(
    note: str,
    roster_files: dict[str, list[str]],
    owner: str,
    dispatched: str,
) -> list[str]:
    changed = _changed_paths(note)
    if changed:
        return changed
    domains = _fixed_domains(note)
    if not domains:
        return []
    candidates = roster_files.get(owner) or roster_files.get(dispatched) or []
    wanted = set(domains)
    matched: list[str] = []
    for path in candidates:
        if PurePosixPath(path).stem in wanted and path not in matched:
            matched.append(path)
    return matched


def _safe_employee(name: str) -> str:
    cleaned = str(name).strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
    ):
        return ""
    return cleaned


def _memory_batches(record: RunRecord, profile_dir: Path) -> dict[str, list[dict]]:
    """Up to two {test, file, cause?} entries per employee who applied a fix."""
    roster_files = _roster_files(profile_dir)
    batches: dict[str, list[dict]] = {}
    seen: dict[str, set[tuple[str, str]]] = {}
    for round_record in record.rounds:
        assignments = round_record.get("assignments") or {}
        for fix in round_record.get("fix_results") or []:
            if not isinstance(fix, dict) or not fix.get("applied"):
                continue
            note = str(fix.get("note") or "")
            dispatched = str(fix.get("actor_id") or "")
            owner = _safe_employee(_memory_owner(dispatched, note))
            if not owner:
                continue
            directory = profile_dir / "agents" / owner
            if not directory.is_dir():
                continue
            files = _files_touched(note, roster_files, owner, dispatched)
            if not files:
                continue
            tests = list(assignments.get(dispatched) or [])
            if not tests:
                tests = [
                    str(item)
                    for item in (round_record.get("failed_tests") or [])
                ]
            cause = _cause_line(note)
            bucket = batches.setdefault(owner, [])
            known = seen.setdefault(owner, set())
            for test_name in tests:
                test_text = str(test_name).strip()
                if not test_text:
                    continue
                for path in files:
                    key = (test_text, path)
                    if key in known or len(bucket) >= _MEMORY_ENTRY_CAP:
                        continue
                    known.add(key)
                    entry = {"test": test_text, "file": path}
                    if cause:
                        entry["cause"] = cause
                    bucket.append(entry)
                if len(bucket) >= _MEMORY_ENTRY_CAP:
                    break
    return batches


def _write_agent_memory(
    record: RunRecord,
    profile_dir: Path | None,
    workspace: Path | None = None,
) -> None:
    """Append PASS-only memory. Does not mutate ``record``.

    Non-PASS runs return before any file is opened for writing. The
    employee must already have an archive directory; this function does
    not invent one and does not summarise with a model.

    BRIEF1b: when the final work copy is given, every entry gains
    ``source_hash`` — the sha256 of its file in that copy (the state
    the lesson was learned from) — so later runs can mark the entry
    stale when the source file changes.
    """
    if profile_dir is None or record.outcome != "PASS":
        return
    from .agent_archive import append_memory_entries

    base = Path(profile_dir)
    batches = _memory_batches(record, base)
    if workspace is not None:
        from .brief import sha256_file

        for entries in batches.values():
            for entry in entries:
                digest = sha256_file(Path(workspace) / entry["file"])
                if digest is not None:
                    entry["source_hash"] = digest
    for name, entries in batches.items():
        if entries:
            append_memory_entries(base, name, entries)


def _emit(on_event, event: dict) -> None:
    """Deliver one progress event; listener failures never affect the run."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # noqa: BLE001 - reporting must not break dispatch
        pass



# ---------------------------------------------------------------------------
# LOAD1：同一失败多候选时择一，按本次运行累计派单数选最少
# ---------------------------------------------------------------------------
# 两栏账本，只记本次运行、从 0 起，不读历史：
#   dispatched（累计派单数）：派一活（一个失败节点）+1，永不减，选人唯一依据；
#   in_hand（在手数）：派单 +1，判卷返回 -1，出错路径也 -1（finally），
#   不参与选人，每轮结束必归 0，用来防漏记。


def _new_ledger() -> dict:
    return {"dispatched": {}, "in_hand": {}}


def _lookup_order(kb: DispatchKB, node: str) -> list[str]:
    """该失败在派单表里的候选顺序：对单条失败原样调一次 kb.lookup。"""
    plan = kb.lookup(evidence_from_failed([node]))
    return [actor for actor, nodes in plan.assignments.items() if node in nodes]


def _pick_least_loaded(
    assignments: dict,
    failed_tests: list[str],
    kb: DispatchKB,
    ledger: dict,
) -> tuple[dict, list[dict]]:
    """把 policy 给出的派单表改成每个失败只派一人，并按节点逐条记账。

    节点按判卷失败顺序逐条派：单候选照旧派那一个；多候选取累计派单数
    最少者，平局按 kb.lookup 返回顺序取第一个（不另排序）。每派一个节点，
    该员工 dispatched、in_hand 各 +1。返回新派单表与本轮择一明细。
    """
    holders: dict[str, list[str]] = {}
    for actor_id, nodes in assignments.items():
        for node in nodes:
            holders.setdefault(node, []).append(actor_id)
    ordered = [n for n in failed_tests if n in holders]
    ordered += [n for n in holders if n not in ordered]

    dispatched = ledger["dispatched"]
    in_hand = ledger["in_hand"]
    picked: dict[str, list[str]] = {}
    picks: list[dict] = []
    for node in ordered:
        candidates = holders[node]
        if len(candidates) > 1:
            order = [a for a in _lookup_order(kb, node) if a in candidates]
            order += [a for a in candidates if a not in order]
            counts = {a: dispatched.get(a, 0) for a in order}
            least = min(counts.values())
            chosen = next(a for a in order if counts[a] == least)
            picks.append(
                {
                    "node": node,
                    "candidates": order,
                    "counts": counts,
                    "picked": chosen,
                }
            )
        else:
            chosen = candidates[0]
        picked.setdefault(chosen, []).append(node)
        dispatched[chosen] = dispatched.get(chosen, 0) + 1
        in_hand[chosen] = in_hand.get(chosen, 0) + 1
    # 保持 policy 原派单表的员工顺序，被择掉全部节点的员工不再出现
    result = {
        actor_id: picked[actor_id]
        for actor_id in assignments
        if actor_id in picked
    }
    return result, picks


def _release(ledger: dict, assignments: dict) -> None:
    """判卷返回或出错：本轮派出的每个节点 in_hand -1（不低于 0）。"""
    in_hand = ledger["in_hand"]
    for actor_id, nodes in assignments.items():
        in_hand[actor_id] = max(0, in_hand.get(actor_id, 0) - len(nodes))


def _ledger_snapshot(ledger: dict) -> dict:
    return {
        "dispatched": dict(ledger["dispatched"]),
        "in_hand": dict(ledger["in_hand"]),
    }



# ---------------------------------------------------------------------------
# SUB1：「修一轮」抽成可独立调用的一步（子工作流，一层嵌套）
# ---------------------------------------------------------------------------
REPAIR_ROUND_STEP = "repair_round"
# 层数上限：安全阀，不是功能。主流程只在第 1 层调用本步，本步内部不再
# 套「修一轮」；超过即停下并如实记一行（不跳过、不死循环）。
MAX_SUB_DEPTH = 1


@dataclass
class RoundContext:
    """一轮修复要读写的运行内状态（不是输入；输入见 repair_round 参数）。

    由 run_arm 建好、跨轮共享，本步只按原循环体的方式读写。
    """

    policy: object
    kb: DispatchKB
    run_actor: object
    workspace: Path
    acceptor: FrozenAcceptor
    rounds: list
    scope_denials: list
    scope_state: dict
    widen_events: list
    widen_applications: list
    ledger: dict
    pick_one: bool
    actor_backend: str
    generalist_grant: str
    on_event: object = None
    active_roster: object = None
    stops: list = field(default_factory=list)


@dataclass
class RoundResult:
    """本轮结果：修好 / 没修好 / 新冒出的失败、修后判卷结论、子记录。"""

    verdict: Verdict
    status: str
    fixed: list = field(default_factory=list)
    unfixed: list = field(default_factory=list)
    new_failures: list = field(default_factory=list)
    record: dict = field(default_factory=dict)
    stopped: str = ""


def _stop_round(
    ctx: RoundContext, verdict: Verdict, round_number: int, depth: int, reason: str
) -> RoundResult:
    line = {
        "step": REPAIR_ROUND_STEP,
        "parent_round": round_number,
        "depth": depth,
        "stopped": reason,
    }
    ctx.stops.append(line)
    return RoundResult(
        verdict=verdict,
        status=verdict.status,
        unfixed=list(verdict.failed_tests),
        record=dict(line),
        stopped=reason,
    )


def repair_round(
    verdict: Verdict,
    round_number: int,
    max_rounds: int,
    ctx: RoundContext,
    *,
    depth: int = 1,
) -> RoundResult:
    """修一轮：派单 → 逐个跑工人 → 写轮记录 → 判卷 → 放宽授权判定。

    输入：verdict（本轮要处理的失败证据，即上一次判卷结论）、round_number
    （第几轮，从 1 起）、max_rounds（轮数上限）；运行内状态走 ctx。
    输出：RoundResult；子记录同时挂在父记录该轮 rounds[i]["sub"]。
    派单到放宽判定的代码由原 run_arm 循环体逐字搬入，语义不变。
    """
    if depth > MAX_SUB_DEPTH:
        return _stop_round(
            ctx, verdict, round_number, depth,
            f"超过层数上限 {MAX_SUB_DEPTH}，未执行",
        )
    if round_number > max_rounds:
        return _stop_round(
            ctx, verdict, round_number, depth,
            f"超过轮数上限 {max_rounds}，未执行",
        )
    started = time.monotonic()
    before = list(verdict.failed_tests)
    policy = ctx.policy
    kb = ctx.kb
    run_actor = ctx.run_actor
    workspace = ctx.workspace
    acceptor = ctx.acceptor
    rounds = ctx.rounds
    scope_denials = ctx.scope_denials
    scope_state = ctx.scope_state
    widen_events = ctx.widen_events
    widen_applications = ctx.widen_applications
    ledger = ctx.ledger
    pick_one = ctx.pick_one
    actor_backend = ctx.actor_backend
    generalist_grant = ctx.generalist_grant
    on_event = ctx.on_event
    active_roster = ctx.active_roster

    assignments = policy(verdict, rounds)
    round_picks: list[dict] = []
    if pick_one:
        # LOAD1：同一失败多候选只派一人（累计派单数最少，平局按
        # kb.lookup 顺序）。kb/lookup 本身不动；parallel 传 pick_one=False
        # 保持全派。
        assignments, round_picks = _pick_least_loaded(
            assignments, list(verdict.failed_tests), kb, ledger
        )
    _emit(
        on_event,
        {
            "event": "dispatch",
            "round": len(rounds) + 1,
            "assignments": {
                actor_id: list(nodes)
                for actor_id, nodes in assignments.items()
            },
        },
    )
    rounds_before = len(rounds)
    try:
        fix_results = []
        round_variants: list[dict] = []
        for actor_id in sorted(assignments):
            denial_start = len(scope_denials)
            # VAR1：kb/lookup 已定好派给谁，这里只按失败分类给任务说明
            # 选对症提醒段；脚本后端没有任务说明，只记账不拼。
            variant_entries = plan_variants(
                list(assignments[actor_id]),
                dict(getattr(verdict, "failure_kinds", {}) or {}),
                rounds,
            )
            round_variants.extend(
                {**entry, "actor_id": actor_id} for entry in variant_entries
            )
            variant_note = render_variant_note(variant_entries)
            variant_extra: dict = {}
            if variant_note and actor_backend != "scripted":
                variant_extra["variant_note"] = variant_note
            result = run_actor(
                actor_id,
                workspace,
                list(assignments[actor_id]),
                list(verdict.failed_tests),
                **variant_extra,
            )
            for denial in scope_denials[denial_start:]:
                denial.setdefault("round", len(rounds) + 1)
            fix_results.append(result)
            _emit(
                on_event,
                {
                    "event": "actor_done",
                    "round": len(rounds) + 1,
                    "actor_id": result.actor_id,
                    "applied": result.applied,
                    "note": result.note,
                },
            )

        rounds.append(
            _round_record(
                len(rounds) + 1,
                verdict,
                assignments,
                fix_results,
                round_variants,
            )
        )
        verdict = acceptor.check(workspace)
    finally:
        # LOAD1：判卷返回（或工人/判卷中途出错）都把本轮在手数退回，
        # 出错路径同样回落，轮末 in_hand 必归 0。
        if pick_one:
            _release(ledger, assignments)
            snapshot = _ledger_snapshot(ledger)
            if len(rounds) > rounds_before:
                rounds[-1]["load"] = {
                    "picks": [dict(item) for item in round_picks],
                    **snapshot,
                }
    _emit(
        on_event,
        {
            "event": "verdict",
            "round": len(rounds),
            "status": verdict.status,
            "failed_count": verdict.failed_count,
        },
    )
    if (
        verdict.status == "FAIL"
        and actor_backend == "agentscope"
        and generalist_grant == "per_assignment"
    ):
        round_number = len(rounds)
        generalist_result = next(
            (
                result
                for result in fix_results
                if result.actor_id == "generalist"
            ),
            None,
        )
        if generalist_result is not None:
            trigger, applications = _widen_trigger(
                round_denials=[
                    item
                    for item in scope_denials
                    if item.get("round") == round_number
                ],
                generalist_result=generalist_result,
                workspace=workspace,
                package=active_roster.package,
                failed_tests=list(rounds[-1]["failed_tests"]),
                already_widened=bool(scope_state.get("widened", False)),
            )
            for application in applications:
                application["round"] = round_number
                widen_applications.append(application)
            if trigger is not None:
                scope_state["widened"] = True
                widen_events.append(
                    {
                        "round": round_number + 1,
                        "trigger": trigger,
                        "path": next(
                            (
                                item["path"]
                                for item in applications
                                if item["valid"]
                            ),
                            None,
                        ),
                    }
                )

    after = list(verdict.failed_tests)
    sub = {
        "step": REPAIR_ROUND_STEP,
        "index": round_number,
        "outcome": verdict.status,
        "rounds": 1,
        "final_failed": len(after),
        "parent_round": round_number,
        "depth": depth,
        "dispatch": {
            actor_id: list(nodes) for actor_id, nodes in assignments.items()
        },
        "verdict": {
            "status": verdict.status,
            "failed_count": verdict.failed_count,
        },
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    if rounds and rounds[-1].get("round") == round_number:
        rounds[-1]["sub"] = dict(sub)
    return RoundResult(
        verdict=verdict,
        status=verdict.status,
        fixed=[node for node in before if node not in after],
        unfixed=[node for node in before if node in after],
        new_failures=[node for node in after if node not in before],
        record=sub,
    )


def run_arm(
    arm: str,
    workspace_src: Path,
    kb_enabled: bool = True,
    max_rounds: int = 6,
    work_root: Path = Path("runs/work"),
    actor_backend: str = "scripted",
    on_event=None,
    vault_dir: Path | None = None,
    kb_path: Path | None = None,
    roster=None,
    profile_dir: Path | None = None,
    inventory: dict | None = None,
    generalist_grant: str = "per_assignment",
    pick_one: bool = True,
) -> RunRecord:
    """Run one arm on a private copy of ``workspace_src``.

    ``max_rounds`` counts repair rounds, not acceptance checks: round 0 is the
    first check, and a first-check tamper therefore records zero repair rounds.
    ``actor_backend`` selects the actor implementation: ``"scripted"`` (the
    default, unchanged), ``"llm"`` (real opencode/DeepSeek subprocesses), or
    ``"agentscope"`` (native AgentScope agents with platform-built models).

    ``on_event`` is an optional callable receiving progress dicts
    (``run_started``, ``verdict``, ``dispatch``, ``actor_done``,
    ``run_finished``).  ``None`` keeps behaviour byte-for-byte identical; a
    listener that raises is ignored so dispatch always finishes.

    ``profile_dir`` is an optional generated/hand-written profile directory;
    its ``vault/`` and ``dispatch_table.json`` are used unless the explicit
    ``vault_dir``/``kb_path`` arguments override them.  One of the two must
    be given: there is no built-in default vault or dispatch table.  For
    the native backend, ``roster`` may be passed explicitly; when omitted
    it is read from the dispatch table's ``roster`` section (a table
    without one is rejected).

    ``inventory`` is the optional module inventory used by dynamic teaming
    (GEN3).  When omitted, a ``profile_dir``'s ``profile.json`` is consulted;
    without one the native actor layer behaves exactly as before.  The
    synthesis marker travels in the ``actor_done`` ``note`` - no new event
    kind is emitted, so existing CLI/TUI listeners are untouched.

    SCOPE1: the write-scope guard is on for every native-backend run,
    whatever the profile's ``origin`` (GEN5 introduced it for
    ``origin="repro"`` only).  Every actor is hard-limited to its roster
    files, a whole-package fallback generalist is narrowed to its assigned
    nodes' evidence files, and denied writes land in
    ``RunRecord.scope_denials``.  A tool-independent hash backstop inside
    the actor layer rolls back any out-of-scope change the tool layer
    did not see.  Other backends keep their previous behaviour.

    ``generalist_grant`` (SCOPE1) is the fallback generalist's grant
    mode for native runs: ``"per_assignment"`` (the product default)
    narrows it to the assigned failures' module files and lets the
    program widen it to the whole package at most once - when the
    generalist hits the fence or files a valid ``WIDEN-APPLY:`` line.
    The grant, every application and every widening land in
    ``RunRecord.generalist_scope`` (and therefore in the result
    bundle). ``"whole_package"`` is the experiment control: no
    narrowing, no widening, no summary.
    """
    from core import model_config as _model_config

    from core import usage as _usage

    # USAGE1：用量改读统一累计器（core.usage），不再读老计数器 _USAGE。
    _usage_before = _usage.call_ids()
    arm = arm.upper()
    if arm not in {"A", "B", "C"}:
        raise ValueError(f"unknown arm: {arm!r}")
    if max_rounds < 0:
        raise ValueError("max_rounds must be >= 0")
    if actor_backend not in {"scripted", "llm", "agentscope"}:
        raise ValueError(f"unknown actor backend: {actor_backend!r}")
    if generalist_grant not in {"per_assignment", "whole_package"}:
        raise ValueError(f"unknown generalist_grant: {generalist_grant!r}")

    if profile_dir is not None:
        paths = profile_paths(profile_dir)
        if vault_dir is None:
            vault_dir = paths["vault_dir"]
        if kb_path is None:
            kb_path = paths["kb_path"]
    if inventory is None:
        inventory = load_inventory(profile_dir)
    origin = load_origin(profile_dir)
    # SCOPE1: the write-scope guard is native-backend only, but no longer
    # gated on the profile origin: every native run is fenced and every
    # denial is recorded.  (GEN5 had it for origin="repro" only.)
    # ``origin`` is still read for the record trail; it no longer gates.
    strict_scope = actor_backend == "agentscope"
    scope_denials: list[dict] = []
    scope_state: dict = {"widened": False}
    node_files = module_files = None
    review_attempts: list[dict] = []
    rework_used = False
    widen_events: list[dict] = []
    widen_applications: list[dict] = []

    if vault_dir is None:
        raise ValueError(
            "run_arm 缺少验收金库：请传入 profile_dir 或 vault_dir"
            "（已移除内置演示金库默认）"
        )
    if kb_path is None:
        raise ValueError(
            "run_arm 缺少派单表：请传入 profile_dir 或 kb_path"
            "（已移除内置演示派单表默认）"
        )
    active_vault = Path(vault_dir)
    active_kb_path = Path(kb_path)
    if profile_dir is not None:
        # VER1：运行开始比对员工定义哈希，手改过的补记一版「外部修改」。
        # 只在不一致时写盘；这是记账，失败不影响修复。
        try:
            from .agent_versions import check_drift

            check_drift(
                Path(profile_dir),
                "运行开始检测到未经版本入口的修改（外部修改）",
            )
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass

    workspace_src = Path(workspace_src).resolve()
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    if workspace_src == work_root or workspace_src in work_root.parents:
        raise ValueError("work_root must not be inside workspace_src")

    workspace = work_root / f"{arm.lower()}-{'kb' if kb_enabled else 'nokb'}-{uuid.uuid4().hex[:8]}"
    baseline_hashes = _baseline_hashes(workspace_src)
    # F1: vault-only hidden tests never enter the work copy — workers
    # cannot read what the vault keeps hidden (the source repo keeps
    # its files; only the copy omits them).
    _hidden_rels: set[str] = set()
    if vault_dir is not None:
        try:
            _manifest = json.loads(
                (Path(vault_dir) / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            _hidden_rels = set(_manifest.get("hidden_files", {}))
        except (OSError, ValueError):
            _hidden_rels = set()
    if _hidden_rels:
        _base_ignore = _COPY_IGNORE

        def _ignore_with_hidden(directory, names):
            ignored = set(_base_ignore(directory, names))
            for name in names:
                try:
                    rel = (Path(directory) / name).relative_to(
                        workspace_src
                    ).as_posix()
                except ValueError:
                    continue
                if rel in _hidden_rels:
                    ignored.add(name)
            return ignored

        shutil.copytree(workspace_src, workspace, ignore=_ignore_with_hidden)
    else:
        shutil.copytree(workspace_src, workspace, ignore=_COPY_IGNORE)

    _emit(
        on_event,
        {
            "event": "run_started",
            "arm": arm,
            "actors": actor_backend,
            "kb_enabled": kb_enabled,
            "workspace": str(workspace),
        },
    )

    acceptor = FrozenAcceptor(active_vault)
    kb = DispatchKB.from_file(active_kb_path, enabled=kb_enabled)
    policy = policy_for(arm, kb)

    # SUB1：非 agentscope 后端没有名册；先置 None，交给 RoundContext。
    active_roster = None
    if actor_backend == "scripted":
        run_actor = act
    elif actor_backend == "llm":
        from .llm_actors import llm_act

        run_actor = llm_act
    else:
        from .agentscope_actors import agentscope_act, roster_from_table

        active_roster = (
            roster if roster is not None else roster_from_table(kb.table)
        )
        node_files, module_files = _table_scope_files(kb.table)
        run_actor = functools.partial(
            agentscope_act,
            roster=active_roster,
            inventory=inventory,
            strict_scope=strict_scope,
            node_files=node_files,
            module_files=module_files,
            # SCOPE1: the denial sink is wired for every native run, so
            # RunRecord.scope_denials proves the fence fired (empty when
            # it never had to).
            scope_denials=scope_denials,
            # MEM1: synthesized experts are filed in this profile, and an
            # existing archive is appended to the system prompt. None keeps
            # the prompt identical to a run with no archive.
            profile_dir=profile_dir,
            generalist_grant=generalist_grant,
            scope_state=scope_state,
        )

    rounds: list[dict] = []
    # LOAD1：本次运行自己的两栏账本，从 0 起；parallel 各副本各自一本。
    ledger = _new_ledger()

    def _compute_review() -> dict:
        from .review import changed_files_vs_baseline, review_patch

        changed = changed_files_vs_baseline(workspace, baseline_hashes)
        if not changed:
            return {
                "status": "跳过",
                "reason": "无补丁（本次运行没有文件改动）",
                "reasons": [],
                "changed_files": [],
                "brief_citations": [],
            }
        scope_files = None
        if actor_backend == "agentscope":
            scope_files = _actor_scope_union(
                kb.table, rounds, node_files, module_files
            )
        rules: list[dict] = []
        if profile_dir is not None:
            from .brief import brief_rules

            rules = brief_rules(profile_dir)
        result = review_patch(
            changed=changed,
            scope_files=scope_files,
            scope_denials=scope_denials,
            brief_rules=rules,
        )
        return {
            "status": result["verdict"],
            "reasons": result["reasons"],
            "changed_files": result["changed_files"],
            "brief_citations": result["brief_citations"],
        }

    verdict = acceptor.check(workspace)
    _emit(
        on_event,
        {
            "event": "verdict",
            "round": 0,
            "status": verdict.status,
            "failed_count": verdict.failed_count,
        },
    )

    while True:
        if verdict.status == "PASS":
            outcome = "PASS"
            attempt = _compute_review()
            if attempt["status"] != "跳过":
                review_attempts.append(attempt)
            if (
                attempt["status"] == "否决"
                and not rework_used
                and rounds
            ):
                rework_used = True
                feedback = "\n".join(
                    f"- {reason['detail']}"
                    for reason in attempt["reasons"]
                )
                last_assignments = rounds[-1].get("assignments") or {}
                if pick_one:
                    # LOAD1：返工也是派单（沿用上轮已择一的派单表，不重选），
                    # 照样两栏 +1，判卷返回或出错 -1。
                    for actor_id, nodes in last_assignments.items():
                        for key in ("dispatched", "in_hand"):
                            ledger[key][actor_id] = (
                                ledger[key].get(actor_id, 0) + len(nodes)
                            )
                try:
                    for actor_id in sorted(last_assignments):
                        denial_start = len(scope_denials)
                        extra: dict = {}
                        if actor_backend == "agentscope":
                            extra["review_feedback"] = feedback
                        result = run_actor(
                            actor_id,
                            workspace,
                            list(last_assignments[actor_id]),
                            list(verdict.failed_tests),
                            **extra,
                        )
                        for denial in scope_denials[denial_start:]:
                            denial.setdefault("round", len(rounds))
                        _emit(
                            on_event,
                            {
                                "event": "actor_done",
                                "round": len(rounds),
                                "actor_id": result.actor_id,
                                "applied": result.applied,
                                "note": result.note,
                            },
                        )
                    verdict = acceptor.check(workspace)
                finally:
                    if pick_one:
                        _release(ledger, last_assignments)
                        rounds[-1].setdefault("load", {"picks": []}).update(
                            {"rework": True, **_ledger_snapshot(ledger)}
                        )
                _emit(
                    on_event,
                    {
                        "event": "verdict",
                        "round": len(rounds),
                        "status": verdict.status,
                        "failed_count": verdict.failed_count,
                    },
                )
                continue
            break
        if verdict.status == "TAMPERED":
            outcome = "TAMPERED"
            break
        if verdict.status != "FAIL":
            raise RuntimeError(f"unexpected verdict status: {verdict.status!r}")

        if len(rounds) >= max_rounds:
            outcome = "CAPPED"
            break

        verdict = repair_round(
            verdict,
            len(rounds) + 1,
            max_rounds,
            RoundContext(
                policy=policy,
                kb=kb,
                run_actor=run_actor,
                workspace=workspace,
                acceptor=acceptor,
                rounds=rounds,
                scope_denials=scope_denials,
                scope_state=scope_state,
                widen_events=widen_events,
                widen_applications=widen_applications,
                ledger=ledger,
                pick_one=pick_one,
                actor_backend=actor_backend,
                generalist_grant=generalist_grant,
                on_event=on_event,
                active_roster=active_roster,
            ),
        ).verdict

    _emit(
        on_event,
        {
            "event": "run_finished",
            "outcome": outcome,
            "rounds": len(rounds),
            "final_failed": list(verdict.failed_tests),
        },
    )

    generalist_scope: dict = {}
    if (
        actor_backend == "agentscope"
        and generalist_grant == "per_assignment"
        and "initial_files" in scope_state
    ):
        initial_files = list(scope_state["initial_files"])
        changed_beyond: set[str] = set()
        for round_record in rounds:
            for result in round_record["fix_results"]:
                if result["actor_id"] == "generalist":
                    changed_beyond.update(_changed_paths(result["note"]))
        generalist_scope = {
            "mode": generalist_grant,
            "initial_files": initial_files,
            "narrowed": bool(scope_state.get("narrowed", False)),
            "widened": bool(scope_state.get("widened", False)),
            "widen_events": widen_events,
            "widen_applications": widen_applications,
            "changed_beyond_initial": sorted(
                path
                for path in changed_beyond
                if path not in set(initial_files)
            ),
        }

    if review_attempts:
        final_attempt = review_attempts[-1]
        record_review: dict = {
            "status": final_attempt["status"],
            "rework_rounds": 1 if rework_used else 0,
            "attempts": review_attempts,
            "brief_citations": final_attempt.get("brief_citations", []),
        }
    elif outcome == "PASS":
        record_review = {
            "status": "跳过",
            "reason": "无补丁（本次运行没有文件改动）",
            "rework_rounds": 0,
            "attempts": [],
            "brief_citations": [],
        }
    else:
        post = _compute_review()
        record_review = {
            "status": post["status"],
            "rework_rounds": 0,
            "attempts": (
                [] if post["status"] == "跳过" else [post]
            ),
            "brief_citations": post.get("brief_citations", []),
        }
        if post["status"] == "跳过":
            record_review["reason"] = post["reason"]

    record = RunRecord(
        arm=arm,
        kb_enabled=kb_enabled,
        outcome=outcome,
        rounds=rounds,
        final_failed=list(verdict.failed_tests),
        scope_denials=list(scope_denials),
        baseline_hashes=baseline_hashes,
        generalist_scope=generalist_scope,
        review=record_review,
    )
    if getattr(verdict, "error_count", 0) or getattr(verdict, "error_tests", None):
        record.final_errors = {
            "error_count": verdict.error_count,
            "error_tests": list(verdict.error_tests),
            "passed_count": verdict.passed_count,
        }
    _usage_delta = _usage.run_usage_dict(
        _usage.accumulator_since(_usage_before)
    )
    if _usage_delta:
        record.usage = _usage_delta
    # The record object is final. Callers persist its JSON after return.
    # This hook only reads it. A failure (unwritable archive, bad path)
    # must not change the outcome, the rounds, or the returned object.
    try:
        _write_agent_memory(record, profile_dir, workspace)
    except Exception:
        _log.warning(
            "员工长期记忆回写失败，运行结果保持不变", exc_info=True
        )
    return record
