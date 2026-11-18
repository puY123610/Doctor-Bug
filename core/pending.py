# -*- coding: utf-8 -*-
"""待批准清单：无人能答时被按拒绝落地的 ask，记下来等老板回来批。

记录点在秘书读工具的权限检查（无应答者且裁决为 ask）。批准 =
重新执行那一个动作：先按当前政策重新裁决，再核对目标文件
指纹（记录时的 sha256）没变；变了必须二次确认才执行。
存储：``<workspace>/.drbug/pending_approvals.json``。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PENDING_FILENAME = "pending_approvals.json"
MAX_CONTENT_CHARS = 4000


def pending_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".drbug" / PENDING_FILENAME


def _load(workspace: str | Path) -> list[dict[str, Any]]:
    path = pending_path(workspace)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(workspace: str | Path, entries: list[dict[str, Any]]) -> None:
    path = pending_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _fingerprint(target: str) -> str | None:
    try:
        return hashlib.sha256(Path(target).read_bytes()).hexdigest()
    except OSError:
        return None


def record_pending(
    workspace: str | Path,
    *,
    actor: str,
    action: str,
    target: str,
) -> dict[str, Any] | None:
    """记一条待批准；同 (action, target) 已有未决条目时不重复记。

    存储失败返回 None——记清单失败绝不许影响裁决本身。
    """

    try:
        entries = _load(workspace)
        for entry in entries:
            if (
                entry.get("action") == action
                and entry.get("target") == target
            ):
                return entry
        entry = {
            "id": uuid.uuid4().hex[:12],
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "fingerprint": _fingerprint(target),
        }
        entries.append(entry)
        _save(workspace, entries)
        return entry
    except OSError:
        return None


def list_pending(workspace: str | Path) -> list[dict[str, Any]]:
    return _load(workspace)


def drop_pending(workspace: str | Path, entry_id: str) -> bool:
    entries = _load(workspace)
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return False
    _save(workspace, kept)
    return True


def execute_pending(
    workspace: str | Path,
    entry_id: str,
    *,
    acknowledge_change: bool = False,
) -> dict[str, Any]:
    """批准并重新执行一条待批准（v1 只支持读文件类动作）。

    返回 ``{"status": ...}``：executed（带 content/truncated）/
    changed（文件已变且未二次确认）/ refused（当前政策拒绝，
    带 reason）/ missing（条目或文件不在了）/ unsupported。
    执行成功才删条目并落一条审计。
    """

    from .policy import (
        Decision,
        PolicyContext,
        PolicyEngine,
        discover_vaults,
    )

    entries = _load(workspace)
    entry = next(
        (e for e in entries if e.get("id") == entry_id), None
    )
    if entry is None:
        return {"status": "missing"}
    action = str(entry.get("action", ""))
    target = str(entry.get("target", ""))
    if action not in {"read_file", "cross_dir_read"}:
        return {"status": "unsupported", "action": action}
    root = Path(workspace)
    engine = PolicyEngine(workspace=root)
    ctx = PolicyContext(
        mode="ask",
        surface="interactive",
        workspace=root,
        vault_paths=discover_vaults(root),
        role="secretary",
        is_ai=True,
        actor="secretary",
    )
    decision = engine.decide("secretary", action, target, ctx)
    if decision.verdict == "deny":
        return {"status": "refused", "reason": decision.reason}
    current = _fingerprint(target)
    if current is None:
        return {"status": "missing"}
    if entry.get("fingerprint") != current and not acknowledge_change:
        return {"status": "changed"}
    try:
        text = Path(target).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return {"status": "missing"}
    truncated = len(text) > MAX_CONTENT_CHARS
    drop_pending(workspace, entry_id)
    engine.audit(
        "secretary",
        "pending_approve_execute",
        target,
        Decision(
            verdict="allow",
            tier=decision.tier,
            reason=f"待批准清单批准后重新执行（复裁 {decision.verdict}）",
        ),
        workspace=root,
    )
    return {
        "status": "executed",
        "content": text[:MAX_CONTENT_CHARS],
        "truncated": truncated,
    }
