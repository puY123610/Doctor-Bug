# -*- coding: utf-8 -*-
"""运行记录的只读呈现层（TRACE1）。

platform/runs/ 里有三类既有产物：api_run_<时间戳>.json、
cli_run_<时间戳>.json、bundles/<时间戳>-<臂>-<哈希>/（内含
record.json 与 report.md）。本模块只读它们、整理成列表与详情，
不新增任何记录机制。记录里没有工作区路径与 token/成本数据，
呈现层也不编造。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_STAMP_RE = re.compile(r"(\d{8})-(\d{6})")
_REPORT_LINE_RE = re.compile(r"^-\s*([^:：]+)[:：]\s*(.+?)\s*$")


def default_runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "runs"


def _stamp_of(name: str) -> str | None:
    match = _STAMP_RE.search(name)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _when_of(stamp: str | None, report_header: dict | None) -> str:
    if report_header:
        raw = report_header.get("生成时间", "")
        try:
            from datetime import datetime

            return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            pass
    if stamp:
        return (
            f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} "
            f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
        )
    return "?"


def parse_report_header(report_path: Path) -> dict | None:
    """把结果包 report.md 的概览行解析成 dict（键为原文中文键）。"""
    try:
        lines = report_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    header: dict[str, str] = {}
    for line in lines[:40]:
        match = _REPORT_LINE_RE.match(line.strip())
        if match:
            header[match.group(1).strip()] = match.group(2).strip().strip("`")
    return header or None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    kind: str  # "bundle" | "api" | "cli"
    when: str
    arm: str
    backend: str | None
    kb_enabled: bool | None
    outcome: str
    rounds_count: int
    final_failed_count: int
    review_verdict: str | None
    scope_denials_count: int
    record_path: Path
    bundle_dir: Path | None


def _summary_from_record(
    *,
    run_id: str,
    kind: str,
    stamp: str | None,
    record: dict,
    record_path: Path,
    bundle_dir: Path | None,
    report_header: dict | None,
) -> RunSummary:
    review = record.get("review")
    review_verdict = (
        review.get("status") if isinstance(review, dict) else None
    )
    backend = None
    if report_header:
        backend = report_header.get("执行后端") or None
    outcome = str(record.get("outcome") or "?")
    if outcome == "?" and report_header:
        outcome = report_header.get("终局", "?")
    arm = str(record.get("arm") or "?")
    if arm == "?" and report_header:
        arm = report_header.get("策略臂", "?")
    return RunSummary(
        run_id=run_id,
        kind=kind,
        when=_when_of(stamp, report_header),
        arm=arm,
        backend=backend,
        kb_enabled=record.get("kb_enabled"),
        outcome=outcome,
        rounds_count=len(record.get("rounds") or []),
        final_failed_count=len(record.get("final_failed") or []),
        review_verdict=review_verdict,
        scope_denials_count=len(record.get("scope_denials") or []),
        record_path=record_path,
        bundle_dir=bundle_dir,
    )


def list_runs(
    runs_dir: Path | None = None, limit: int = 20
) -> tuple[list[RunSummary], int]:
    """按时间倒序返回 (摘要列表, 跳过的损坏文件数)。

    同一次运行的 bundle 与 run JSON 按时间戳合并成一条
    （bundle 元数据优先），不重复列。
    """
    root = Path(runs_dir) if runs_dir is not None else default_runs_dir()
    skipped = 0
    summaries: list[RunSummary] = []
    bundle_stamps: set[str] = set()

    bundles_root = root / "bundles"
    bundle_dirs: list[Path] = []
    if bundles_root.is_dir():
        bundle_dirs = sorted(
            (p for p in bundles_root.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
    for bundle_dir in bundle_dirs:
        stamp = _stamp_of(bundle_dir.name)
        record_path = bundle_dir / "record.json"
        record = _read_json(record_path) if record_path.exists() else None
        if record is None:
            if record_path.exists():
                skipped += 1
            record = {}
        report_header = parse_report_header(bundle_dir / "report.md")
        summaries.append(
            _summary_from_record(
                run_id=bundle_dir.name,
                kind="bundle",
                stamp=stamp,
                record=record,
                record_path=record_path,
                bundle_dir=bundle_dir,
                report_header=report_header,
            )
        )
        if stamp:
            bundle_stamps.add(stamp)

    if root.is_dir():
        for path in sorted(root.glob("*_run_*.json"), reverse=True):
            record = _read_json(path)
            if record is None:
                skipped += 1
                continue
            stamp = _stamp_of(path.name)
            if stamp and stamp in bundle_stamps:
                continue  # 已并入 bundle 条目
            kind = "api" if path.name.startswith("api_run_") else "cli"
            summaries.append(
                _summary_from_record(
                    run_id=path.stem,
                    kind=kind,
                    stamp=stamp,
                    record=record,
                    record_path=path,
                    bundle_dir=None,
                    report_header=None,
                )
            )

    # 按时间戳排序（展示字符串里的 "?" 会排到真时间前面）：
    # 无时间戳的条目（如 gen_run_* 结果包）沉底。
    summaries.sort(
        key=lambda s: (_stamp_of(s.run_id) or "", s.run_id), reverse=True
    )
    return summaries[:limit], skipped


def load_run_detail(summary: RunSummary) -> dict:
    record = _read_json(summary.record_path) or {}
    report_header = None
    if summary.bundle_dir is not None:
        report_header = parse_report_header(summary.bundle_dir / "report.md")
    return {
        "summary": summary,
        "record": record,
        "report_header": report_header,
    }


def format_run_line(summary: RunSummary) -> str:
    parts = [summary.when, f"臂 {summary.arm}"]
    if summary.backend:
        parts.append(summary.backend)
    parts.append(summary.outcome)
    if summary.review_verdict:
        parts.append(f"秘书审查：{summary.review_verdict}")
    parts.append(f"{summary.rounds_count} 轮")
    parts.append(f"残留 {summary.final_failed_count}")
    parts.append(summary.run_id)
    return " · ".join(parts)


def format_run_detail(detail: dict) -> list[str]:
    summary: RunSummary = detail["summary"]
    record: dict = detail["record"]
    lines = [
        f"运行：{summary.run_id}",
        f"时间：{summary.when} · 臂 {summary.arm}"
        + (f" · 后端 {summary.backend}" if summary.backend else "")
        + f" · 知识库 {'开启' if summary.kb_enabled else '关闭' if summary.kb_enabled is not None else '未知'}",
        f"终局：{summary.outcome} · 轮次 {summary.rounds_count}"
        f" · 最终残留失败 {summary.final_failed_count}"
        f" · 辖区拒绝 {summary.scope_denials_count} 次",
    ]
    usage = record.get("usage")
    if isinstance(usage, dict) and any(usage.values()):
        # USAGE1：统一口径下未知字段是 None，显示「未知」而不是 None/0。
        def _tok(key: str) -> object:
            value = usage.get(key, 0)
            return "未知" if value is None else value

        lines.append(
            f"token：输入 {_tok('input_tokens')}"
            f" · 输出 {_tok('output_tokens')}"
            f" · 调用 {usage.get('calls', 0)} 次"
        )
    generalist = record.get("generalist_scope")
    if isinstance(generalist, dict):
        lines.append(
            "兜底工人授权：模式 "
            f"{generalist.get('mode', '?')}"
            f" · 已扩权 {'是' if generalist.get('widened') else '否'}"
        )
    review = record.get("review")
    if isinstance(review, dict):
        lines.append(
            f"秘书审查：{review.get('status', '?')}"
            f"（返工 {review.get('rework_rounds', 0)} 轮）"
        )
    for entry in record.get("rounds") or []:
        if not isinstance(entry, dict):
            continue
        failed = entry.get("failed_tests") or []
        lines.append(
            f"第 {entry.get('round', '?')} 轮 · {entry.get('status', '?')}"
            f" · 失败 {len(failed)} 个"
        )
        assignments = entry.get("assignments")
        if isinstance(assignments, dict) and assignments:
            brief = "、".join(
                f"{name}×{len(nodes)}"
                for name, nodes in assignments.items()
                if nodes
            )
            if brief:
                lines.append(f"  派单：{brief}")
    return lines

def read_audit_entries(
    workspace, limit: int = 50
) -> tuple[list[dict], int]:
    """读 <workspace>/.drbug/audit.log 的最后 limit 条（损坏行跳过计数）。"""
    log_path = Path(workspace) / ".drbug" / "audit.log"
    entries: list[dict] = []
    corrupt = 0
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            corrupt += 1
            continue
        if isinstance(item, dict):
            entries.append(item)
        else:
            corrupt += 1
    return entries[-limit:], corrupt


def format_audit_line(entry: dict) -> str:
    ts = str(entry.get("ts", ""))[:19].replace("T", " ")
    return (
        f"{ts} · {entry.get('actor', '?')} · {entry.get('action', '?')}"
        f" · {entry.get('verdict', '?')}（tier {entry.get('tier', '?')}）"
        f" · {entry.get('target', '')}"
    )

