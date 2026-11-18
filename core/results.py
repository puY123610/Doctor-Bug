"""结果包：每次运行产出 record.json、patch.diff、report.md 与采纳记录。

结果包是用户审查运行产物的唯一入口。本模块只负责「记录」用户是否采纳：

* :func:`build_result_bundle` 把一次运行整理成一个目录；
* :func:`record_adoption` 只更新 ``adoption.json``，**永不**把 patch 应用
  回原仓库。是否合入由用户自己决定。

patch.diff 只用标准库 ``difflib`` 遍历两棵树生成，不要求目标仓库是 git
仓库；二进制文件与 ``__pycache__`` 等噪声会在报告中计数说明。

:func:`apply_bundle`（APPLY1）是唯一的合入路径：它只接受带基线指纹
（``baseline_hashes``，RESULT2）的结果包，应用前逐个被改文件重算原件
哈希，与基线对不上就整包拒绝、一个字不写；无指纹的旧包、已被标记
拒绝的包、涉及密钥/凭据路径的补丁一律拒绝。``record_adoption`` 的
语义不变，仍只记录。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PLATFORM_ROOT / "runs"
BUNDLES_DIR = RUNS_DIR / "bundles"

_NOISE_DIRS = {
    ".drbug",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
}
_NOISE_FILES = {".DS_Store", "Thumbs.db"}
_NOISE_SUFFIXES = {".pyc", ".pyo"}
_BINARY_SNIFF_BYTES = 8192


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_run_id(final_dir: Path) -> str:
    """Timestamp + final-copy name, with a short suffix to avoid collisions."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", final_dir.name).strip("-")
    suffix = uuid.uuid4().hex[:6]
    return f"{stamp}-{slug or 'run'}-{suffix}"


def _is_noise_dir(name: str) -> bool:
    return name in _NOISE_DIRS or name.endswith(".egg-info")


def _is_noise_file(name: str) -> bool:
    return name in _NOISE_FILES or Path(name).suffix in _NOISE_SUFFIXES


def _scan_tree(root: Path) -> tuple[dict[str, Path], int]:
    """Map relative POSIX paths to files, skipping noise; also count noise."""
    files: dict[str, Path] = {}
    skipped_noise = 0
    for dirpath, dirnames, filenames in os.walk(root):
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            if _is_noise_dir(name):
                skipped_noise += 1
            else:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            if _is_noise_file(name):
                skipped_noise += 1
                continue
            full = Path(dirpath) / name
            relative = full.relative_to(root).as_posix()
            files[relative] = full
    return files, skipped_noise


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _unified_diff(
    relative: str,
    old_text: str,
    new_text: str,
    status: str,
) -> list[str]:
    if status == "added":
        fromfile, tofile = "/dev/null", f"b/{relative}"
    elif status == "deleted":
        fromfile, tofile = f"a/{relative}", "/dev/null"
    else:
        fromfile, tofile = f"a/{relative}", f"b/{relative}"

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    rendered: list[str] = []
    for line in difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=fromfile,
        tofile=tofile,
        n=3,
    ):
        if line.endswith("\n"):
            rendered.append(line)
        else:
            rendered.append(line + "\n")
            rendered.append("\\ No newline at end of file\n")
    return rendered


def _build_patch(
    source_dir: Path, final_dir: Path
) -> tuple[str, dict[str, int]]:
    source_files, source_noise = _scan_tree(source_dir)
    final_files, final_noise = _scan_tree(final_dir)

    stats = {
        "added": 0,
        "deleted": 0,
        "modified": 0,
        "skipped_binary": 0,
        "skipped_noise": source_noise + final_noise,
        "skipped_unreadable": 0,
    }
    chunks: list[str] = []

    for relative in sorted(set(source_files) | set(final_files)):
        source_path = source_files.get(relative)
        final_path = final_files.get(relative)
        try:
            old_bytes = source_path.read_bytes() if source_path else None
            new_bytes = final_path.read_bytes() if final_path else None
        except OSError:
            stats["skipped_unreadable"] += 1
            continue

        if old_bytes is not None and new_bytes is not None:
            if old_bytes == new_bytes:
                continue
            status = "modified"
        elif old_bytes is None:
            status = "added"
        else:
            status = "deleted"

        if (old_bytes is not None and _is_binary(old_bytes)) or (
            new_bytes is not None and _is_binary(new_bytes)
        ):
            stats["skipped_binary"] += 1
            continue

        old_text = old_bytes.decode("utf-8") if old_bytes is not None else ""
        new_text = new_bytes.decode("utf-8") if new_bytes is not None else ""
        chunks.extend(_unified_diff(relative, old_text, new_text, status))
        stats[status] += 1

    return "".join(chunks), stats


def _record_dict(record: Any) -> dict:
    data = record.to_dict() if hasattr(record, "to_dict") else record
    if not isinstance(data, dict):
        raise TypeError("record 必须是 RunRecord 或 dict")
    return data


def _table_cell(value: Any) -> str:
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def _failed_text(failed: list) -> str:
    if not failed:
        return "无"
    return "、".join(f"`{item}`" for item in failed)


def _assignment_text(assignments: dict) -> str:
    if not assignments:
        return "无"
    bits = [
        f"{actor}×{len(nodes)}"
        for actor, nodes in assignments.items()
        if isinstance(nodes, (list, tuple, set))
    ]
    return "、".join(bits) if bits else "无"


def _render_report(
    *,
    run_id: str,
    record: dict,
    source_dir: Path,
    final_dir: Path,
    actor_backend: str,
    record_path: Path | None,
    stats: dict[str, int],
    patch_bytes: int,
) -> str:
    rounds = record.get("rounds") or []
    final_failed = record.get("final_failed") or []
    denials = record.get("scope_denials") or []

    lines = [
        "# Doctor Bug 结果包",
        "",
        "## 概览",
        "",
        f"- 结果包: `{run_id}`",
        f"- 生成时间: {_now_iso()}",
        f"- 策略臂: {record.get('arm')}",
        f"- 执行后端: {actor_backend}",
        f"- 知识库: {'开启' if record.get('kb_enabled') else '关闭'}",
        f"- 终局: {record.get('outcome')}",
        f"- 轮次: {len(rounds)}",
        f"- 最终残留失败: {len(final_failed)}",
        f"- 原始仓库: `{source_dir}`",
        f"- 最终修复副本: `{final_dir}`",
        "- 运行记录: "
        + (f"`{record_path}`" if record_path is not None else "（未单独落盘）"),
        "",
        *render_scorecard(record),
        "",
        "## 轮次表",
        "",
        "每一轮记录的是该轮派单所依据的验收结果（修复前），"
        "修复后的复验见下一轮或「概览」里的终局。",
        "",
        "| 轮次 | 验收状态 | 失败用例 | 派单 |",
        "| --- | --- | --- | --- |",
    ]
    for round_record in rounds:
        if not isinstance(round_record, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(round_record.get("round")),
                    _table_cell(round_record.get("status")),
                    _failed_text(round_record.get("failed_tests") or []),
                    _table_cell(
                        _assignment_text(round_record.get("assignments") or {})
                    ),
                ]
            )
            + " |"
        )
    if not rounds:
        lines.append("| — | — | 无 | 无 |")

    lines.extend(["", "## 最终残留失败", ""])
    if final_failed:
        lines.extend(f"- `{node}`" for node in final_failed)
    else:
        lines.append("- 无")

    lines.extend(["", "## Scope denials（scope_denials）", ""])
    if denials:
        for denial in denials:
            if not isinstance(denial, dict):
                lines.append(f"- {denial}")
                continue
            lines.append(
                "- 第 {round} 轮 · {actor} · {tool} · `{path}` — {reason}".format(
                    round=denial.get("round", "?"),
                    actor=denial.get("actor_id", "?"),
                    tool=denial.get("tool", "?"),
                    path=denial.get("path", "?"),
                    reason=denial.get("reason", ""),
                )
            )
    else:
        lines.append("- 无")

    review = record.get("review") or {}
    lines.extend(["", "## 秘书审查", ""])
    if not review:
        lines.append("- 未审查（无审查记录）")
    elif review.get("status") == "跳过":
        lines.append(f"- 跳过: {review.get('reason', '')}")
    else:
        lines.append(f"- 结论: {review.get('status')}")
        lines.append(f"- 返工轮次: {review.get('rework_rounds', 0)}")
        for attempt in review.get("attempts") or []:
            for reason in attempt.get("reasons") or []:
                lines.append(
                    "- 否决理由: " + _table_cell(reason.get("detail", ""))
                )
        for citation in review.get("brief_citations") or []:
            lines.append(
                f"- 相关简报规则: {citation.get('rule', '')}"
                f"（出处 `{citation.get('path', '')}`）"
            )
        if review.get("status") == "否决":
            lines.append("- 秘书已否决，采纳请老板慎重。")

    lines.extend(
        [
            "",
            "## 补丁统计",
            "",
            f"- 修改文件: {stats['modified']}",
            f"- 新增文件: {stats['added']}",
            f"- 删除文件: {stats['deleted']}",
            f"- 跳过二进制文件: {stats['skipped_binary']}",
            f"- 跳过噪声条目: {stats['skipped_noise']}"
            "（`__pycache__`、`.pytest_cache`、`.git`、`.DS_Store` 等）",
            f"- 跳过不可读文件: {stats['skipped_unreadable']}",
            f"- patch.diff 大小: {patch_bytes} 字节",
            "",
            "## 结果包文件索引",
            "",
            "- `record.json` — 现有 RunRecord 原样落盘",
            "- `patch.diff` — 原始仓库 → 最终修复副本的统一 diff",
            "- `report.md` — 本报告",
            "- `adoption.json` — 用户采纳/拒绝记录（初始未决定；只记录，不合入）",
            "",
        ]
    )
    return "\n".join(lines)



# ---------------------------------------------------------------- EVD1 成绩单

_TEST_WRONG_RE = re.compile(r"TEST-WRONG:\s*(\S+)\s*\|\s*(.+)")
_UNFIXED_NOTE_LIMIT = 160


def _node_errored(node: str, errors: list[str]) -> bool:
    """节点本身出 error，或它所在文件收集出错（ERROR 行只有文件路径）。"""
    file_part = node.split("::", 1)[0]
    return any(item == node or item == file_part for item in errors)


def _test_wrong_marks(rounds: list, targets: list[str]) -> dict[str, str]:
    """工人结论里的 TEST-WRONG 行 → {节点: 理由}；只认第一遍查出的毛病。"""
    marks: dict[str, str] = {}
    wanted = set(targets)
    for round_record in rounds:
        if not isinstance(round_record, dict):
            continue
        for result in round_record.get("fix_results") or []:
            if not isinstance(result, dict):
                continue
            for match in _TEST_WRONG_RE.finditer(str(result.get("note") or "")):
                node, reason = match.group(1), match.group(2).strip()
                if node in wanted and node not in marks:
                    marks[node] = reason
    return marks


def _unfixed_reason(rounds: list, node: str) -> str:
    """最后一次派到这条用例的工人结论（截断）；没有就如实说轮数用完。"""
    for round_record in reversed(rounds):
        if not isinstance(round_record, dict):
            continue
        owners = [
            actor
            for actor, nodes in (round_record.get("assignments") or {}).items()
            if node in (nodes or [])
        ]
        for result in round_record.get("fix_results") or []:
            if not isinstance(result, dict):
                continue
            raw = _TEST_WRONG_RE.sub("", str(result.get("note") or ""))
            if result.get("actor_id") in owners and raw.strip():
                note = " ".join(raw.split())
                if len(note) > _UNFIXED_NOTE_LIMIT:
                    note = note[:_UNFIXED_NOTE_LIMIT] + "…"
                return f"修理轮数用完仍没通过；最后一次修理结论：{note}"
    return "修理轮数用完仍没通过"


def build_scorecard(record: dict) -> dict:
    """只用首验（第 1 轮记录）与终验（final_failed / final_errors）算账，不重跑测试。"""
    rounds = [item for item in (record.get("rounds") or []) if isinstance(item, dict)]
    outcome = record.get("outcome")
    if not rounds:
        if outcome == "PASS":
            return {"kind": "clean"}
        return {"kind": "unknown"}
    first = rounds[0]
    targets = list(first.get("failed_tests") or [])
    first_errors = list(first.get("error_tests") or [])
    final_failed = set(record.get("final_failed") or [])
    final_block = record.get("final_errors") or {}
    final_errors = list(final_block.get("error_tests") or [])

    marks = _test_wrong_marks(rounds, targets)
    not_run: list[dict] = []
    counted: list[str] = []
    for node in targets:
        if node in marks:
            continue
        if _node_errored(node, final_errors):
            not_run.append({"node": node, "when": "终验"})
            continue
        counted.append(node)
    fixed = [node for node in counted if node not in final_failed]
    unfixed = [
        {"node": node, "reason": _unfixed_reason(rounds, node)}
        for node in counted
        if node in final_failed
    ]
    for node in first_errors:
        not_run.append({"node": node, "when": "第一遍"})

    regression = None
    first_passed = first.get("passed_count")
    if isinstance(first_passed, int):
        target_set = set(targets)
        broken = sorted(
            node for node in final_failed
            if node not in target_set
        )
        reg_errors = [
            item for item in final_errors
            if not any(_node_errored(node, [item]) for node in targets)
        ]
        for item in reg_errors:
            not_run.append({"node": item, "when": "终验"})
        if final_block and isinstance(final_block.get("passed_count"), int):
            # 终验有 error 时：原来好的仍通过数 = 终验通过数 − 目标用例里终验通过的数
            target_passing = sum(
                1 for node in targets
                if node not in final_failed and not _node_errored(node, final_errors)
            )
            kept = max(final_block["passed_count"] - target_passing, 0)
            total = kept + len(broken)
        else:
            total = first_passed
            kept = max(first_passed - len(broken), 0)
        regression = {"kept": kept, "total": total, "broken": broken}

    return {
        "kind": "scored" if targets else "none_found",
        "fixed": len(fixed),
        "denominator": len(counted),
        "fixed_nodes": fixed,
        "unfixed": unfixed,
        "not_run": not_run,
        "test_wrong": [{"node": n, "reason": r} for n, r in marks.items()],
        "regression": regression,
    }


def render_scorecard(record: dict) -> list[str]:
    """成绩单大白话文案；比率永远带分子分母，零检出不出比率。"""
    card = build_scorecard(record)
    lines = ["## 成绩单", ""]
    if card["kind"] == "clean":
        lines.append("- 目前没有检测出来问题。")
        return lines
    if card["kind"] == "unknown":
        lines.append("- 没有第一遍检查的记录，算不出成绩。")
        return lines
    if card["kind"] == "none_found":
        lines.append("- 目前没有检测出来问题。")
    else:
        lines.append(
            f"- 第一遍查出的毛病：修好了 {card['fixed']} / {card['denominator']} 条"
            f"（该修的 {card['denominator']} 条里，修好 {card['fixed']} 条）。"
        )
        regression = card["regression"]
        if regression is None:
            lines.append("- 原来是好的：这次运行没有记下第一遍的通过数，算不出来。")
        else:
            lines.append(
                f"- 原来是好的：现在仍然好的 {regression['kept']} / {regression['total']} 条。"
            )
            for node in regression["broken"]:
                lines.append(f"  - 被改坏了：`{node}`")
    if card["not_run"]:
        lines.append(
            f"- 没跑起来（测试根本没执行成功，不算修好也不算没修好）：{len(card['not_run'])} 条。"
        )
        groups: dict[str, list[dict]] = {}
        for item in card["not_run"]:
            groups.setdefault(item["node"].split("::", 1)[0], []).append(item)
        for file_part, items in groups.items():
            lines.append(f"  - 文件 `{file_part}`：")
            for item in items:
                lines.append(f"    - `{item['node']}`（{item['when']}检查时报错没跑成）")
    if card.get("test_wrong"):
        lines.append(
            f"- 可能是测试本身写错了（不算没修好，请老板确认）：{len(card['test_wrong'])} 条。"
        )
        for item in card["test_wrong"]:
            lines.append(f"  - `{item['node']}`：{item['reason']}")
    if card.get("unfixed"):
        lines.append(f"- 没修好的：{len(card['unfixed'])} 条。")
        for item in card["unfixed"]:
            lines.append(f"  - `{item['node']}`：{item['reason']}")
    lines.extend(_variant_lines(record))
    lines.extend(_load_lines(record))
    lines.extend(_sub_lines(record))
    return lines


def _sub_lines(record: dict) -> list[str]:
    """SUB1：写明每一轮是「修一轮」这一步做的（只读 rounds[i].sub）。"""
    parts: list[str] = []
    for round_record in record.get("rounds") or []:
        sub = round_record.get("sub")
        if not sub:
            continue
        parts.append(
            f"第 {sub['parent_round']} 轮（修完还剩 {sub['final_failed']} 条没过）"
        )
    if not parts:
        return []
    return ["- 每一轮都由「修一轮」这一步完成：" + "；".join(parts) + "。"]


def _load_lines(record: dict) -> list[str]:
    """LOAD1：同一失败有多个候选时，写明这次派给了活最少的那个人。"""
    picks: list[str] = []
    for round_record in record.get("rounds") or []:
        for item in (round_record.get("load") or {}).get("picks") or []:
            counts = "、".join(
                f"{actor} {count}" for actor, count in item["counts"].items()
            )
            picks.append(
                f"  - 第 {round_record.get('round')} 轮 `{item['node']}`："
                f"派给 {item['picked']}（当时各候选已派活数：{counts}）"
            )
    if not picks:
        return []
    return [
        "- 同一条失败有好几个人能修时，只派一人，派给当时手上活最少的："
    ] + picks


def _variant_lines(record: dict) -> list[str]:
    """VAR1：派单用过对症提醒段时，带一句哪类失败用了哪段（只读运行记录）。"""
    used: dict[tuple[str, str], list[str]] = {}
    for round_record in record.get("rounds") or []:
        for entry in round_record.get("variants") or []:
            name = entry.get("variant") or ""
            if not name:
                continue
            key = (entry.get("kind") or "", name)
            nodes = used.setdefault(key, [])
            if entry.get("node") not in nodes:
                nodes.append(entry.get("node"))
    if not used:
        return []
    parts = [
        f"{kind}的 {len(nodes)} 条用了「{name}」段"
        for (kind, name), nodes in used.items()
    ]
    return ["- 派单时加的对症提醒：" + "；".join(parts) + "。"]


def build_result_bundle(
    record: Any,
    source_dir: str | Path,
    final_dir: str | Path,
    *,
    actor_backend: str = "scripted",
    bundle_root: str | Path | None = None,
    run_id: str | None = None,
    record_path: str | Path | None = None,
) -> Path:
    """Build one result bundle directory and return its path.

    ``record`` is a :class:`core.runner.RunRecord` or its ``to_dict()`` shape.
    ``source_dir`` is the original workspace and ``final_dir`` the final
    private repair copy (``runs/work/<id>``).  The bundle contains exactly
    ``record.json``, ``patch.diff``, ``report.md`` and ``adoption.json``.
    """
    source = Path(source_dir).resolve()
    final = Path(final_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"原始仓库不存在: {source}")
    if not final.is_dir():
        raise FileNotFoundError(f"最终修复副本不存在: {final}")

    data = _record_dict(record)
    root = Path(bundle_root) if bundle_root is not None else BUNDLES_DIR
    root.mkdir(parents=True, exist_ok=True)

    explicit_run_id = run_id is not None
    if run_id is None:
        run_id = _new_run_id(final)
    elif Path(run_id).name != run_id or run_id in {"", ".", ".."}:
        raise ValueError(f"run_id 不能包含路径分隔符: {run_id!r}")

    bundle_dir = root / run_id
    if bundle_dir.exists():
        if explicit_run_id:
            raise FileExistsError(f"结果包目录已存在: {bundle_dir}")
        while bundle_dir.exists():
            bundle_dir = root / f"{run_id}-{uuid.uuid4().hex[:6]}"
    bundle_dir.mkdir(parents=True)

    patch_text, stats = _build_patch(source, final)
    record_file = Path(record_path) if record_path is not None else None
    report = _render_report(
        run_id=bundle_dir.name,
        record=data,
        source_dir=source,
        final_dir=final,
        actor_backend=actor_backend,
        record_path=record_file,
        stats=stats,
        patch_bytes=len(patch_text.encode("utf-8")),
    )
    adoption = {"adopted": None, "decided_at": None, "note": ""}

    (bundle_dir / "record.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "patch.diff").write_text(patch_text, encoding="utf-8")
    (bundle_dir / "report.md").write_text(report, encoding="utf-8")
    (bundle_dir / "adoption.json").write_text(
        json.dumps(adoption, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return bundle_dir


def _default_adoption() -> dict:
    return {"adopted": None, "decided_at": None, "note": ""}


def load_adoption(bundle_dir: str | Path | None) -> dict:
    """Read ``adoption.json``; malformed/missing files return the default."""
    if bundle_dir is None:
        return _default_adoption()
    path = Path(bundle_dir) / "adoption.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_adoption()
    if not isinstance(data, dict):
        return _default_adoption()
    adopted = data.get("adopted")
    return {
        "adopted": adopted if isinstance(adopted, bool) else None,
        "decided_at": (
            data.get("decided_at")
            if isinstance(data.get("decided_at"), str)
            else None
        ),
        "note": data.get("note") if isinstance(data.get("note"), str) else "",
    }


def record_adoption(
    bundle_dir: str | Path,
    adopted: bool,
    note: str = "",
) -> dict:
    """Record the user's decision in ``adoption.json`` and nothing else.

    This function never applies ``patch.diff`` and never touches the original
    repository.  ``adopted`` must be a real bool (``True`` adopts, ``False``
    rejects); the decision timestamp is written in local time.
    """
    if not isinstance(adopted, bool):
        raise TypeError("adopted 必须是 bool（True=采纳，False=拒绝）")
    directory = Path(bundle_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"结果包目录不存在: {directory}")
    if not (directory / "record.json").is_file():
        raise ValueError(f"不是结果包目录（缺少 record.json）: {directory}")

    payload = {
        "adopted": adopted,
        "decided_at": _now_iso(),
        "note": str(note),
    }
    (directory / "adoption.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


def _parse_patch(patch_text: str) -> list[dict]:
    """Parse our own difflib patch text into per-file hunks.

    Each file patch is ``{"path", "status", "hunks"}``; each hunk is
    ``(old_start, old_count, body)`` where body entries are
    ``(kind, text, no_eol)`` — kind is ``" "``/``"-"``/``"+"``, text
    carries no trailing newline, and ``no_eol`` marks the
    ``\\ No newline at end of file`` case.  Malformed input raises
    ``ValueError``; nothing is guessed.
    """
    lines = patch_text.split("\n")
    patches: list[dict] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.startswith("--- "):
            index += 1
            continue
        fromfile = line[4:].strip()
        if index + 1 >= total or not lines[index + 1].startswith("+++ "):
            raise ValueError("patch.diff 格式损坏：--- 后缺少 +++ 行")
        tofile = lines[index + 1][4:].strip()
        index += 2
        if fromfile == "/dev/null":
            status, rel = "added", tofile
        elif tofile == "/dev/null":
            status, rel = "deleted", fromfile
        else:
            status, rel = "modified", tofile
        for prefix in ("a/", "b/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts or not parts:
            raise ValueError(f"patch.diff 含不安全路径：{rel!r}")
        hunks: list[tuple[int, int, list]] = []
        while index < total and lines[index].startswith("@@ "):
            match = _HUNK_RE.match(lines[index])
            if match is None:
                raise ValueError(
                    f"patch.diff 格式损坏：坏的 hunk 头 {lines[index]!r}"
                )
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            index += 1
            body: list[tuple[str, str, bool]] = []
            seen_old = 0
            seen_new = 0
            while index < total and (seen_old < old_count or seen_new < new_count):
                raw = lines[index]
                if raw.startswith("\\"):
                    if body:
                        kind, text, _ = body[-1]
                        body[-1] = (kind, text, True)
                    index += 1
                    continue
                if not raw or raw[0] not in " -+":
                    raise ValueError(
                        f"patch.diff 格式损坏：hunk 内出现怪行 {raw!r}"
                    )
                kind = raw[0]
                body.append((kind, raw[1:], False))
                if kind in " -":
                    seen_old += 1
                if kind in " +":
                    seen_new += 1
                index += 1
            if index < total and lines[index].startswith("\\") and body:
                kind, text, _ = body[-1]
                body[-1] = (kind, text, True)
                index += 1
            hunks.append((old_start, old_count, body))
        patches.append({"path": rel, "status": status, "hunks": hunks})
    return patches


def _hunk_lines(body: list, kinds: str) -> list[str]:
    return [
        text + ("" if no_eol else "\n")
        for kind, text, no_eol in body
        if kind in kinds
    ]


def _apply_file_patch(current_text: str, file_patch: dict) -> str:
    """Apply one file's hunks to its current text, strictly.

    Context and removed lines must match byte-for-byte at the
    recorded positions (adjusted by earlier hunks' size delta);
    any mismatch raises ``ValueError`` and nothing is written.
    """
    lines = current_text.splitlines(keepends=True)
    offset = 0
    for old_start, old_count, body in file_patch["hunks"]:
        pos = (old_start - 1 if old_count > 0 else old_start) + offset
        expected = _hunk_lines(body, " -")
        replacement = _hunk_lines(body, " +")
        if lines[pos:pos + len(expected)] != expected:
            raise ValueError(
                f"补丁与原件对不上（{file_patch['path']} 第 {old_start} 行附近）"
            )
        lines[pos:pos + len(expected)] = replacement
        offset += len(replacement) - len(expected)
    return "".join(lines)


def apply_bundle(
    bundle_dir: str | Path,
    workspace: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Apply a result bundle's patch back into its original workspace.

    The anti-overwrite contract (RESULT2 pairing): the bundle must
    carry ``baseline_hashes``; before anything is written, every
    file the patch touches is re-hashed in *workspace* and compared
    with the baseline.  Any mismatch — boss edited it, it vanished,
    or a to-be-created file now exists — refuses the whole apply
    with every conflict listed and zero bytes written.  Bundles
    without a baseline, bundles the boss already rejected, and
    patches touching secret/credential paths are refused outright.
    All hunks are applied in memory first; the disk is touched
    only after every file checks out.  On success ``adoption.json``
    records ``adopted`` plus an ``applied`` block.
    """
    directory = Path(bundle_dir)
    if directory.is_file() and directory.name == "record.json":
        directory = directory.parent
    if not directory.is_dir():
        raise FileNotFoundError(f"结果包目录不存在: {directory}")
    record_path = directory / "record.json"
    patch_path = directory / "patch.diff"
    if not record_path.is_file() or not patch_path.is_file():
        raise ValueError(f"不是结果包目录（缺少 record.json/patch.diff）: {directory}")
    target_root = Path(workspace)
    if not target_root.is_dir():
        raise FileNotFoundError(f"原仓库目录不存在: {target_root}")

    record = json.loads(record_path.read_text(encoding="utf-8"))
    baseline = record.get("baseline_hashes")
    if not isinstance(baseline, dict) or not baseline:
        raise ValueError(
            "该结果包没有基线指纹（旧包），不能自动应用，请手动处理"
        )
    adoption = load_adoption(directory)
    if adoption["adopted"] is False:
        raise ValueError("该结果包已被标记为拒绝，不应用")

    patches = _parse_patch(patch_path.read_text(encoding="utf-8"))
    if not patches:
        raise ValueError("patch.diff 为空，没有可应用的改动")

    from .policy import is_secret_path

    for file_patch in patches:
        if is_secret_path(Path(file_patch["path"])):
            raise ValueError(
                f"补丁涉及密钥/凭据路径 {file_patch['path']}，拒绝自动应用"
            )

    from .brief import sha256_file

    conflicts: list[str] = []
    for file_patch in patches:
        rel = file_patch["path"]
        target = target_root / rel
        if file_patch["status"] == "added":
            if rel in baseline:
                conflicts.append(f"{rel}（基线里已有此文件，与新建补丁矛盾）")
            elif target.exists():
                conflicts.append(f"{rel}（补丁要新建，但该位置已存在文件）")
            continue
        recorded = baseline.get(rel)
        if recorded is None:
            conflicts.append(f"{rel}（基线指纹里没有这个文件）")
            continue
        current = sha256_file(target)
        if current is None:
            conflicts.append(f"{rel}（原件已不存在）")
        elif current != recorded:
            conflicts.append(f"{rel}（原件在运行之后被改动）")
    if conflicts:
        raise ValueError(
            "原件与基线不一致，拒绝应用（一个字都没写）：\n"
            + "\n".join(f"  - {item}" for item in conflicts)
        )

    new_texts: dict[str, str | None] = {}
    for file_patch in patches:
        rel = file_patch["path"]
        if file_patch["status"] == "deleted":
            new_texts[rel] = None
            continue
        current_text = ""
        if file_patch["status"] == "modified":
            current_text = (target_root / rel).read_text(encoding="utf-8")
        new_texts[rel] = _apply_file_patch(current_text, file_patch)

    plan = [
        {"path": fp["path"], "status": fp["status"]} for fp in patches
    ]
    result = {
        "bundle": str(directory),
        "workspace": str(target_root),
        "files": plan,
        "applied": False,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    for file_patch in patches:
        rel = file_patch["path"]
        target = target_root / rel
        new_text = new_texts[rel]
        if new_text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")

    payload = {
        "adopted": True if adoption["adopted"] is None else adoption["adopted"],
        "decided_at": adoption["decided_at"] or _now_iso(),
        "note": adoption["note"],
        "applied": {
            "at": _now_iso(),
            "workspace": str(target_root),
            "files": [item["path"] for item in plan],
        },
    }
    (directory / "adoption.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result["applied"] = True
    return result


def describe_adoption(bundle_dir: str | Path | None) -> str:
    """Human-readable adoption state used by CLI/TUI summaries."""
    data = load_adoption(bundle_dir)
    if data["adopted"] is None:
        return "未决定"
    state = "已采纳" if data["adopted"] else "已拒绝"
    bits = [state]
    if data["decided_at"]:
        bits.append(data["decided_at"])
    if data["note"]:
        bits.append(f"备注: {data['note']}")
    return "；".join(bits)


def locate_bundle(
    record_path: str | Path,
    bundles_root: str | Path | None = None,
) -> Path | None:
    """Find the bundle for a record path, a bundle dir, or an identical copy."""
    path = Path(record_path)
    if path.is_dir():
        if (path / "record.json").is_file() and (
            path / "adoption.json"
        ).is_file():
            return path
        return None

    parent = path.parent
    if (
        path.name == "record.json"
        and (parent / "record.json").is_file()
        and (parent / "adoption.json").is_file()
    ):
        return parent

    try:
        target = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    root = Path(bundles_root) if bundles_root is not None else BUNDLES_DIR
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "adoption.json").is_file():
            continue
        try:
            candidate = json.loads(
                (child / "record.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if candidate == target:
            matches.append(child)
    return matches[-1] if matches else None


__all__ = [
    "BUNDLES_DIR",
    "build_result_bundle",
    "describe_adoption",
    "load_adoption",
    "locate_bundle",
    "record_adoption",
]
