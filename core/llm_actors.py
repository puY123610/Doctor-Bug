"""Real-agent actor backend: drive one ``opencode`` subprocess per actor.

The scripted actors in :mod:`core.actors` are deterministic test doubles.  This
module is the corresponding *real* backend: it renders a role prompt, runs the
local ``opencode`` CLI (DeepSeek) with the workspace as its cwd, and detects
repairs by diffing SHA-256 snapshots of ``backoffice/**/*.py``.

The module is offline-testable: ``opencode_path`` is injectable and the real
binary path is only a default.  Nothing here is imported by the default
(scripted) runner path.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

from .actors import FixResult
from .variants import with_variant_note

OPENCODE_BIN = "/opt/homebrew/bin/opencode"
OPENCODE_MODEL = "deepseek/deepseek-flash#max"

# Domain contract per actor.  ``allowed`` is the exact file list used in the
# prompt; ``specialist`` controls whether the no-boundary-crossing clause is
# emitted (the generalist is allowed to work across the three product files).
_ROLE_PROFILES: dict[str, dict[str, str]] = {
    "billing-specialist": {
        "identity": "你是 billing-specialist（计费专家），负责订单计价、优惠券、税基与总额。",
        "domain": "你只负责 backoffice/billing.py。",
        "boundary": "只允许修改 backoffice/billing.py",
        "specialist": "yes",
    },
    "inventory-specialist": {
        "identity": "你是 inventory-specialist（库存专家），负责库存、预留与可售数量核算。",
        "domain": "你只负责 backoffice/inventory.py。",
        "boundary": "只允许修改 backoffice/inventory.py",
        "specialist": "yes",
    },
    "auth-specialist": {
        "identity": "你是 auth-specialist（鉴权专家），负责角色判断与退款权限。",
        "domain": "你只负责 backoffice/auth.py。",
        "boundary": "只允许修改 backoffice/auth.py",
        "specialist": "yes",
    },
    "db-specialist": {
        "identity": "你是 db-specialist（数据库专家），负责订单持久化与数据访问。",
        "domain": "你只负责 backoffice/db.py。",
        "boundary": "只允许修改 backoffice/db.py",
        "specialist": "yes",
    },
    "generalist": {
        "identity": "你是 generalist（通用修复者），可以跨计费、库存、鉴权三个方向定位根因。",
        "domain": (
            "你可以修改的文件只有 backoffice/billing.py、"
            "backoffice/inventory.py、backoffice/auth.py。"
        ),
        "boundary": (
            "只允许修改 backoffice/billing.py、backoffice/inventory.py、"
            "backoffice/auth.py 这三个文件"
        ),
        "specialist": "no",
    },
}

_SYMPTOMS = """\
【工单症状（摘自 tests/fixtures/demo_repo/REGRESSION_NOTES.md，v1.3 发版后回归）】
1. 工单 1「用券订单金额不对」：使用优惠券的订单实付总额比预期偏高；没有使用优惠券的订单金额正常。
2. 工单 2「库存下降过快」：可售库存下降速度约为实际销量的两倍，盘点时账面数量明显低于实物数量。
3. 工单 3「支持账号可以退款」：support 账号可以直接完成退款；按现行流程，退款应只允许 finance 角色操作。
4. 当前状态：v1.3 发版后 CI 出现 8 个失败，上述三个问题尚未定位到具体改动。"""


def build_prompt(
    actor_id: str,
    assigned_nodes: list[str],
    failed_nodes: list[str],
) -> str:
    """Render the role prompt for one actor and one dispatch round."""
    profile = _ROLE_PROFILES.get(actor_id)
    if profile is None:
        raise KeyError(f"unknown actor id: {actor_id!r}")

    assigned_lines = (
        "\n".join(f"- {node}" for node in assigned_nodes)
        if assigned_nodes
        else "- （本轮没有分到失败节点）"
    )
    context_lines = (
        "\n".join(f"- {node}" for node in failed_nodes)
        if failed_nodes
        else "- （无）"
    )

    constraints = [
        f"1. {profile['boundary']}；绝不修改 tests/、conftest.py、pytest.ini。",
        "2. 最小改动：只改与根因直接相关的代码行，不做重构、不改格式、不动无关文件。",
        "3. 不得改动验收器、不得伪造结果或为了让测试通过而修改测试。",
    ]
    if profile["specialist"] == "yes":
        constraints.append(
            "4. 若你判断根因不在自己的领域文件，只许在回复中说明理由和建议接手方，"
            "不许越界修改其他文件。"
        )
    else:
        constraints.append(
            "4. 请按根因在三个允许文件中做最小改动；若根因不在这些文件，只许说明，"
            "不许越界。"
        )
    constraints.append("5. 完成后用几句话简述你改了哪个文件、哪一行、为什么。")

    lines = [
        profile["identity"],
        profile["domain"],
        "",
        _SYMPTOMS,
        "",
        "【本轮分派】",
        "本轮分到的失败 node id（必须针对这些失败定位根因）：",
        assigned_lines,
        "",
        "本轮验收器全部失败 node id（仅作背景；未分给你的部分不要越界处理）：",
        context_lines,
        "",
        "【硬约束】",
        *constraints,
    ]
    return "\n".join(lines)


def snapshot_python(workspace: Path) -> dict[str, str]:
    """Return ``relative path -> sha256`` for every .py under backoffice/."""
    backoffice = Path(workspace) / "backoffice"
    if not backoffice.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(backoffice.rglob("*.py")):
        if not path.is_file():
            continue
        snapshot[str(path.relative_to(workspace))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _output_tail(output: str, max_lines: int = 8, max_chars: int = 800) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = " | ".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = "..." + tail[-max_chars:]
    return tail


def llm_act(
    actor_id: str,
    workspace: Path,
    assigned_nodes: list[str],
    failed_nodes: list[str],
    *,
    timeout_s: float = 600,
    opencode_path: str | Path = OPENCODE_BIN,
    variant_note: str = "",
) -> FixResult:
    """Run one real actor and report whether the workspace changed.

    The function never raises for a failing/timing-out subprocess: the main
    dispatch loop must keep going and let the next acceptance check decide.
    """
    prompt = build_prompt(actor_id, list(assigned_nodes), list(failed_nodes))
    # VAR1：按失败分类选好的对症提醒段，有才拼（见 core.variants）。
    prompt = with_variant_note(prompt, variant_note)
    workspace = Path(workspace)
    before = snapshot_python(workspace)

    command = [
        str(opencode_path),
        "run",
        "--auto",
        "-m",
        OPENCODE_MODEL,
        prompt,
    ]

    started = time.monotonic()
    returncode: int | None = None
    timed_out = False
    error: str | None = None
    output = ""
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        returncode = completed.returncode
        output = _as_text(completed.stdout) + _as_text(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        output = _as_text(exc.stdout) + _as_text(exc.stderr)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started

    after = snapshot_python(workspace)
    changed = _changed_files(before, after)

    applied = not timed_out and returncode == 0 and error is None and bool(changed)

    if timed_out:
        status = f"timeout after {timeout_s}s"
    elif error is not None:
        status = f"error={error}"
    else:
        status = f"exit={returncode}"
    parts = [
        status,
        f"elapsed={elapsed:.2f}s",
        "changed=" + (", ".join(changed) if changed else "none"),
    ]
    tail = _output_tail(output)
    if tail:
        parts.append("output=" + tail)
    note = "; ".join(parts)

    return FixResult(actor_id=actor_id, applied=applied, note=note)
