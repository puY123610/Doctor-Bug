"""Native AgentScope actor backend.

Each expert is one :class:`agentscope.agent.Agent` whose role prompt and
write scope come from a *roster configuration*:

    specialist_id -> {"files": [repository-relative paths], "blurb": "..."}

The default roster is the existing demo roster, so demo runs keep their old
prompt byte-for-byte (``legacy_prompt=True`` delegates to
:func:`core.llm_actors.build_prompt`) and their old ``backoffice/**/*.py``
snapshot.  Non-demo rosters (for example ``src/studyroom``) get a generic
roster prompt and a package-rooted snapshot.

The model is built by the platform's own :mod:`core.model_config` (never by
OpenCode), and the tools are the AgentScope file tools only (``Read`` /
``Write`` / ``Edit`` / ``Glob``) — no ``Bash``, to keep the blast radius
small.  ``Write`` and ``Edit`` are wrapped in a scoped subclass: the
permission layer denies any path outside the roster's ``files`` list, so the
hard write limit is enforced even if the model ignores the prompt.

The workspace copy is the tool root: reads/writes require absolute paths and
``Glob`` defaults to the workspace through a small
:class:`agentscope.tool.LocalBackend` subclass.  Permission mode is
``DONT_ASK`` (the unattended counterpart of ``ACCEPT_EDITS``): edits inside
the workspace are auto-allowed, everything that would prompt an absent user
is denied instead of hanging.

Write-scope guard (GEN5 introduced it for repro-origin profiles; SCOPE1
turned it on for every native run): the runner passes ``strict_scope=True``
for all AgentScope runs: every actor is hard-limited to its roster ``files``
list, and the whole-package fallback generalist is narrowed to the union of
the evidence files of its assigned nodes.  Denied writes are appended to a
caller-owned ``scope_denials`` list so the run record proves the guard fired.
On top of the tool layer sits a tool-independent hash backstop: when a
denial sink is wired, the actor layer snapshots the whole workspace tree
before the act and rolls back any out-of-scope change afterwards, recording
one denial per rolled-back file.  There is deliberately no automatic
widening inside an act: an actor that believes the root cause lives
elsewhere says so in its reply, and the runner decides between rounds.
Direct callers that pass ``strict_scope=False`` keep the old behaviour.

Dynamic teaming (GEN3): when the dispatch plan names an actor that the roster
does not know, :func:`synthesize_specialist` builds a one-file specialist from
the *inventory* (evidence module -> repository-relative path + docstring first
line) and that entry acts instead.  A roster whose generalist is the last
resort is upgraded the same way when its whole assignment points at a single
module that no specialist owns.  Successful synthesis is auditable through the
``[synthesized]`` prefix of :class:`core.actors.FixResult` ``note``; without an
inventory every path stays byte-for-byte the old behaviour.

Repairs are detected with SHA-256 snapshots of the package root (``backoffice``
or ``src/studyroom``) before and after the call.  The synchronous runner calls
the async agent through :func:`asyncio.run`; timeout and any runtime exception
become ``applied=False`` notes, never exceptions in the main dispatch loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Iterable

from acceptor.evidence import module_from_node_id
from agentscope.agent import Agent
from agentscope.message import Msg, TextBlock
from agentscope.model import ChatModelBase
from agentscope.permission import (
    AdditionalWorkingDirectory,
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
)
from agentscope.state import AgentState
from agentscope.tool import (
    Edit,
    Glob,
    LocalBackend,
    Read,
    Toolkit,
    Write,
)

from .variants import with_variant_note
from .actors import FixResult
from .agent_archive import (
    ensure_archives,
    mark_stale_entries,
    read_archive,
)
from .llm_actors import build_prompt
from .policy import PolicyContext, PolicyEngine, discover_vaults, format_denial
from .shell_tool import ShellTool, command_verdict

# Long-term memory injected into one expert's system prompt. The rest of
# MEMORY.md stays on disk; the cut is marked so the model can see it.
_MEMORY_INJECT_LIMIT = 4000
_MEMORY_TRUNCATION_MARK = "…（MEMORY 已截断，仅注入前 4000 字）"


@dataclass(frozen=True)
class SpecialistProfile:
    """One roster entry: writable files plus a one-sentence duty."""

    files: tuple[str, ...]
    blurb: str
    specialist: bool = True


@dataclass(frozen=True)
class Roster:
    """Package root plus the specialist -> files/blurb mapping."""

    package: str
    specialists: dict[str, SpecialistProfile]
    symptoms: str = ""
    legacy_prompt: bool = False

    def profile(self, actor_id: str) -> SpecialistProfile:
        try:
            return self.specialists[actor_id]
        except KeyError as exc:
            raise KeyError(f"unknown actor id: {actor_id!r}") from exc

    def allowed_files(self, actor_id: str) -> tuple[str, ...]:
        return self.profile(actor_id).files


def _normalise_relative(value: str) -> str:
    raw = str(value).replace("\\", "/").strip()
    pure = PurePosixPath(raw)
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not raw or pure.is_absolute() or ".." in pure.parts or not parts:
        raise ValueError(f"invalid repository-relative path: {value!r}")
    return "/".join(parts)


def roster_from_table(table: dict) -> Roster:
    """Build a roster from a dispatch table's optional ``roster`` section.

    A table without a roster section is a malformed profile: raise
    instead of silently substituting any built-in roster.
    """
    section = table.get("roster")
    if section is None:
        raise ValueError(
            "派单表缺少 roster 段：无法组建团队（旧表请重新建档生成）"
        )
    if not isinstance(section, dict):
        raise ValueError("roster must be an object")

    raw_package = str(section["package"]).strip()
    # F2: "." (or empty) names the repository root — multi-package
    # JS/Go repos have no single directory prefixing every source
    # file.  The root package contains every relative path.
    package = (
        "." if raw_package in ("", ".") else _normalise_relative(raw_package)
    )
    raw_specialists = section.get("specialists")
    if not isinstance(raw_specialists, dict) or not raw_specialists:
        raise ValueError("roster.specialists must be a non-empty object")

    specialists: dict[str, SpecialistProfile] = {}
    prefix = "" if package == "." else package.rstrip("/") + "/"
    for actor_id, config in raw_specialists.items():
        if not isinstance(config, dict):
            raise ValueError(f"roster entry {actor_id!r} must be an object")
        files = tuple(
            _normalise_relative(item) for item in config.get("files", ())
        )
        if not files:
            raise ValueError(f"roster entry {actor_id!r} has no files")
        for item in files:
            if not item.startswith(prefix):
                raise ValueError(
                    f"roster entry {actor_id!r} file {item!r} is outside "
                    f"package {package!r}"
                )
        blurb = str(config.get("blurb", "")).strip()
        if not blurb:
            raise ValueError(f"roster entry {actor_id!r} has no blurb")
        specialists[str(actor_id)] = SpecialistProfile(
            files=files,
            blurb=blurb,
            specialist=bool(config.get("specialist", True)),
        )

    if "generalist" not in specialists:
        raise ValueError("roster must contain a 'generalist' fallback")
    return Roster(
        package=package,
        specialists=specialists,
        symptoms=str(section.get("symptoms", "")).strip(),
        legacy_prompt=False,
    )


#: Roster-level fallback actor id (``roster_from_table`` requires it).
FALLBACK_ACTOR = "generalist"
#: Suffix of every generated specialist id.
SYNTHETIC_SUFFIX = "-specialist"


def _neutral_blurb(module: str) -> str:
    """The same neutral sentence autoprofile uses for undocumented modules."""
    return f"负责 {module} 模块的修复。"


def synthesize_specialist(
    module: str,
    inventory: Mapping[str, object] | None,
) -> tuple[str, SpecialistProfile] | None:
    """Build a one-file roster entry for one evidence module.

    ``inventory`` maps an evidence module (the test-stem module produced by
    :func:`acceptor.evidence.module_from_node_id`) to an object with
    ``"path"`` (repository-relative source file) and ``"blurb"`` (first
    docstring line, may be empty).  The entry id is ``<module>-specialist``
    and its write scope is exactly that one file.

    Returns ``(actor_id, profile)``, or ``None`` when the module is absent
    from the inventory or its path is not a safe repository-relative path;
    the caller then keeps the pre-existing generalist behaviour.
    """
    if not inventory or not module:
        return None
    entry = inventory.get(module)
    if not isinstance(entry, Mapping):
        return None
    try:
        path = _normalise_relative(str(entry.get("path", "")))
    except ValueError:
        return None
    blurb = str(entry.get("blurb", "")).strip() or _neutral_blurb(module)
    return (
        f"{module}{SYNTHETIC_SUFFIX}",
        SpecialistProfile(files=(path,), blurb=blurb),
    )


def _evidence_modules(assigned_nodes: Iterable[str]) -> list[str]:
    """Evidence modules of assigned nodes, first-seen order, deduplicated."""
    modules: list[str] = []
    for node_id in assigned_nodes:
        module = module_from_node_id(node_id)
        if module not in modules:
            modules.append(module)
    return modules


def evidence_scope_files(
    assigned_nodes: Iterable[str],
    node_files: Mapping[str, Iterable[str]] | None,
    module_files: Mapping[str, Iterable[str]] | None,
) -> tuple[str, ...]:
    """Union of the evidence files of the assigned nodes (GEN5 repro guard).

    ``node_files`` mirrors the dispatch table's ``node_overrides``
    (node -> files of its evidence specialists) and ``module_files`` mirrors
    ``module_defaults`` (evidence module -> files).  Both are already
    evidence-derived, so the result is exactly "the modules this node's
    failure evidence points at".  An empty result means there is no evidence
    to narrow to; the caller then keeps the roster scope unchanged.
    """
    node_files = node_files or {}
    module_files = module_files or {}
    files: set[str] = set()
    for node_id in assigned_nodes:
        direct = tuple(node_files.get(node_id, ()))
        if direct:
            files.update(direct)
            continue
        files.update(module_files.get(module_from_node_id(node_id), ()))
    return tuple(sorted(files))


# GEN5: the scoped tools run deep inside AgentScope, so the caller-owned
# denial sink travels in a context variable instead of changing the
# published ``_run_agent`` signature (offline tests patch that coroutine).
_SCOPE_DENIALS: ContextVar[list[dict] | None] = ContextVar(
    "scope_denials", default=None
)
_SCOPE_ACTOR: ContextVar[str] = ContextVar("scope_actor", default="")


def _inside_package(path: str, package: str) -> bool:
    if package in ("", "."):
        return True
    return path.startswith(package.rstrip("/") + "/")


def _synthesis_module(
    actor_id: str,
    assigned_nodes: Iterable[str],
    inventory: Mapping[str, object] | None,
) -> str | None:
    """Pick the module to synthesize for an unknown actor id.

    The module named by the actor id wins (``conflicts-specialist`` ->
    ``conflicts``); otherwise the first assigned node whose module is in the
    inventory is used.
    """
    if not inventory:
        return None
    modules = _evidence_modules(assigned_nodes)
    if actor_id.endswith(SYNTHETIC_SUFFIX):
        stem = actor_id[: -len(SYNTHETIC_SUFFIX)]
        if stem in modules and stem in inventory:
            return stem
    for module in modules:
        if module in inventory:
            return module
    return None


def _specialist_covered_files(roster: Roster) -> set[str]:
    """Files owned by an explicit specialist (the fallback does not count)."""
    return {
        path
        for profile in roster.specialists.values()
        if profile.specialist
        for path in profile.files
    }


def _fallback_synthesis_module(
    assigned_nodes: Iterable[str],
    roster: Roster,
    inventory: Mapping[str, object] | None,
) -> str | None:
    """Module to upgrade a whole-package generalist for, if any.

    Only a single-module assignment qualifies: with several evidence modules
    in flight one synthesized file scope would silently drop the others.  The
    module must be in the inventory, inside the roster package (that package
    is the audit/snapshot root) and not owned by any explicit specialist -
    that is exactly the roster gap this feature fills.
    """
    modules = _evidence_modules(assigned_nodes)
    if len(modules) != 1:
        return None
    module = modules[0]
    entry = synthesize_specialist(module, inventory)
    if entry is None:
        return None
    path = entry[1].files[0]
    if not _inside_package(path, roster.package):
        return None
    if path in _specialist_covered_files(roster):
        return None
    return module


def _synthesized_marker(
    entry_id: str,
    module: str,
    assigned_actor: str,
    path: str,
) -> str:
    """One-line audit prefix that survives the CLI's 120-char note cut."""
    assigned = (
        "" if entry_id == assigned_actor else f" (assigned {assigned_actor})"
    )
    return (
        f"[synthesized] {entry_id} for module {module}{assigned}"
        f"; files={path}"
    )


def _overlay_blocked_modules(profile_dir: Path | None) -> frozenset[str]:
    """老板在覆盖层里删掉的员工所属模块：运行时不许再合成出来。

    覆盖层条目记的 module 就是证据模块名，与 synthesize_specialist
    的入参同名。没有 profile_dir 或没有覆盖层时返回空集（行为不变）。
    """
    if profile_dir is None:
        return frozenset()
    from .roster import load_overlay

    overlay = load_overlay(Path(profile_dir))
    modules: set[str] = set()
    for entry in overlay.get("removed") or []:
        if isinstance(entry, dict):
            module = str(entry.get("module") or "").strip()
            if module:
                modules.add(module)
    return frozenset(modules)


def _blocked_marker(module: str | None, assigned_actor: str) -> str:
    """Audit prefix when the boss overlay forbids synthesizing a module."""
    return (
        f"[synthesis-blocked] module {module} (assigned {assigned_actor});"
        " removed via roster overlay"
    )


def _resolve_actor(
    actor_id: str,
    assigned_nodes: list[str],
    roster: Roster,
    inventory: Mapping[str, object] | None,
    *,
    blocked_modules: frozenset[str] = frozenset(),
) -> tuple[str, SpecialistProfile, str | None]:
    """Resolve who actually acts, synthesizing a specialist on a roster gap.

    Returns ``(profile_id, profile, marker)``.  ``marker`` is ``None`` for the
    unchanged path, a ``[synthesized]`` prefix on success and a
    ``[synthesis-miss]`` prefix when an unknown actor had to fall back to the
    roster's generalist.  Raises ``KeyError`` exactly like before when an
    unknown actor has no fallback to land on.
    """
    if actor_id in roster.specialists:
        profile = roster.specialists[actor_id]
        if actor_id != FALLBACK_ACTOR or inventory is None:
            return actor_id, profile, None
        module = _fallback_synthesis_module(
            assigned_nodes, roster, inventory
        )
        if module is None:
            return actor_id, profile, None
        if module in blocked_modules:
            return actor_id, profile, _blocked_marker(module, actor_id)
        synthesized = synthesize_specialist(module, inventory)
        if synthesized is None:  # defensive: validated above
            return actor_id, profile, None
        entry_id, entry_profile = synthesized
        return (
            entry_id,
            entry_profile,
            _synthesized_marker(
                entry_id, module, actor_id, entry_profile.files[0]
            ),
        )

    if inventory is None:
        # No inventory means dynamic teaming is off: keep the pre-GEN3
        # KeyError exactly as before (the profile lookup raises it).
        return actor_id, roster.profile(actor_id), None

    module = _synthesis_module(actor_id, assigned_nodes, inventory)
    blocked_hit = module is not None and module in blocked_modules
    synthesized = (
        synthesize_specialist(module, inventory)
        if module and not blocked_hit
        else None
    )
    if synthesized is not None and _inside_package(
        synthesized[1].files[0], roster.package
    ):
        entry_id, entry_profile = synthesized
        return (
            entry_id,
            entry_profile,
            _synthesized_marker(
                entry_id, module, actor_id, entry_profile.files[0]
            ),
        )

    fallback = roster.specialists.get(FALLBACK_ACTOR)
    if fallback is None:
        raise KeyError(f"unknown actor id: {actor_id!r}")
    if blocked_hit:
        return FALLBACK_ACTOR, fallback, _blocked_marker(module, actor_id)
    return (
        FALLBACK_ACTOR,
        fallback,
        f"[synthesis-miss] {actor_id}: no synthesizable inventory entry for"
        f" module {module or '?'}; fell back to generalist",
    )


def build_roster_prompt(
    actor_id: str,
    assigned_nodes: list[str],
    failed_nodes: list[str],
    roster: Roster,
) -> str:
    """Render a role prompt from a non-demo roster entry."""
    profile = roster.profile(actor_id)
    files_line = "、".join(profile.files)

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
        f"1. 只允许修改 {files_line}；绝不修改 tests/、conftest.py、pytest.ini。",
        "2. 最小改动：只改与根因直接相关的代码行，不做重构、不改格式、不动无关文件。",
        "3. 不得改动验收器、不得伪造结果或为了让测试通过而修改测试。",
    ]
    if profile.specialist:
        constraints.append(
            "4. 若你判断根因不在自己的领域文件，只许在回复中说明理由和建议接手方，"
            "不许越界修改其他文件。"
        )
    else:
        constraints.append(
            "4. 请按根因在允许文件中做最小改动；若根因不在这些文件，只许说明，"
            "不许越界。"
        )
    constraints.append("5. 完成后用几句话简述你改了哪个文件、哪一行、为什么。")

    lines = [
        profile.blurb,
        f"你只负责 {files_line}。",
        "",
    ]
    if roster.symptoms:
        lines.extend(
            [
                "【工单症状（摘自仓库 REGRESSION_NOTES.md）】",
                roster.symptoms,
                "",
            ]
        )
    lines.extend(
        [
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
    )
    return "\n".join(lines)


class _WorkspaceBackend(LocalBackend):
    """Local backend whose default working directory is the workspace copy.

    ``Glob`` falls back to ``backend.getcwd()`` when the model omits a path;
    pointing that at the workspace keeps searches inside the copy instead of
    the platform process's current directory.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = str(workspace)

    async def getcwd(self) -> str:
        return self._workspace


# Tool name → PolicyEngine action. Glob is not a file read/write.
# Shell's target is the command string; _policy_denial routes it
# through shell_tool.command_verdict (whitelist + engine), because
# the engine alone does not Tier-1 a bare path given as a command.
_POLICY_ACTIONS = {
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Shell": "shell_exec",
}


def _record_shell_denial(command: str, reason: str) -> None:
    sink = _SCOPE_DENIALS.get()
    if sink is not None:
        sink.append(
            {
                "actor_id": _SCOPE_ACTOR.get(),
                "tool": "Shell",
                "path": command,
                "reason": reason,
            }
        )


def _bind_policy_engine(tool: object, workspace: Path | None) -> None:
    """Attach one default engine. No new config files or rule sets."""

    resolved = Path(workspace).resolve() if workspace is not None else None
    tool._policy_workspace = resolved  # type: ignore[attr-defined]
    tool._policy_engine = PolicyEngine(workspace=resolved)  # type: ignore[attr-defined]


def _policy_denial(tool: object, tool_input: dict) -> PermissionDecision | None:
    """Engine gate for the worker face. ``None`` means allow, keep going.

    ``surface`` is ``runtime``, the value ``_default_context`` already uses
    for a worker. A literal ``surface="worker"`` is not special in the
    engine: workspace writes then come back as tier-4 ask, and this face
    turns every ask into deny, so in-scope edits would all be rejected.
    Ask has no confirm channel here — a worker cannot approve itself.
    """

    tool_name = getattr(tool, "name", None)
    if not isinstance(tool_name, str):
        tool_name = type(tool).__name__
    action = _POLICY_ACTIONS.get(tool_name)
    if action is None:
        return None
    raw_path = str(tool_input.get("file_path", "")).strip()
    workspace = getattr(tool, "_policy_workspace", None)
    engine = getattr(tool, "_policy_engine", None)
    if not isinstance(engine, PolicyEngine):
        engine = PolicyEngine(workspace=workspace)
    actor = _SCOPE_ACTOR.get() or "worker"
    if action == "shell_exec":
        command = str(
            tool_input.get("command") or tool_input.get("file_path", "")
        ).strip()
        shell_workspace = (
            workspace or getattr(tool, "_workspace", None) or Path.cwd()
        )
        decision = command_verdict(
            command, workspace=Path(shell_workspace), actor=actor
        )
        if decision.verdict == "allow":
            return None
        sink = _SCOPE_DENIALS.get()
        if sink is not None:
            sink.append(
                {
                    "actor_id": _SCOPE_ACTOR.get(),
                    "tool": tool_name,
                    "path": command,
                    "reason": decision.reason,
                }
            )
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=format_denial(decision),
            decision_reason=decision.reason,
        )
    policy_context = PolicyContext(
        mode="ask",
        surface="runtime",
        workspace=workspace,
        vault_paths=discover_vaults(workspace),
        role="worker",
        is_ai=True,
        actor=actor,
    )
    decision = engine.decide(actor, action, raw_path, policy_context)
    if decision.verdict == "allow":
        return None
    sink = _SCOPE_DENIALS.get()
    if sink is not None:
        sink.append(
            {
                "actor_id": _SCOPE_ACTOR.get(),
                "tool": tool_name,
                "path": raw_path,
                "reason": decision.reason,
            }
        )
    return PermissionDecision(
        behavior=PermissionBehavior.DENY,
        message=format_denial(decision),
        decision_reason=decision.reason,
    )


class _PolicyRead(Read):
    """``Read`` with the same engine gate. Reads have no roster file list."""

    def __init__(self, *, workspace: Path | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[call-arg]
        _bind_policy_engine(self, workspace)

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        denied = _policy_denial(self, tool_input)
        if denied is not None:
            return denied
        return await super().check_permissions(tool_input, context)


class _ScopedWriteMixin:
    """Deny writes the engine rejects, then paths outside the roster list.

    The engine runs before the roster check and before AgentScope's own
    permission flow. Ask is deny on this face. Allow keeps the old roster
    conclusion. ``Path.resolve`` collapses ``..`` and symlinks before the
    roster comparison.
    """

    def __init__(
        self,
        allowed_paths: Iterable[Path],
        *,
        workspace: Path | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[call-arg]
        self._allowed_paths = tuple(
            Path(path).resolve() for path in allowed_paths
        )
        _bind_policy_engine(self, workspace)

    async def check_permissions(
        self,
        tool_input: dict,
        context: PermissionContext,
    ) -> PermissionDecision:
        denied = _policy_denial(self, tool_input)
        if denied is not None:
            return denied
        raw_path = str(tool_input.get("file_path", "")).strip()
        allowed = bool(raw_path) and (
            Path(raw_path).resolve() in self._allowed_paths
        )
        if not allowed:
            sink = _SCOPE_DENIALS.get()
            if sink is not None:
                # GEN5: repro-origin runs prove the guard fired.  The denial
                # itself is the pre-existing roster mechanism (GEN3); only
                # the recording of it is new.
                sink.append(
                    {
                        "actor_id": _SCOPE_ACTOR.get(),
                        "tool": self.name,
                        "path": raw_path,
                        "reason": "path outside the roster files scope",
                    }
                )
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    f"Permission denied for {self.name}: path outside the "
                    "specialist roster scope"
                ),
                decision_reason=(
                    "Write/Edit is restricted to the roster's files list"
                ),
            )
        return await super().check_permissions(tool_input, context)  # type: ignore[misc]


class ScopedWrite(_ScopedWriteMixin, Write):
    """``Write`` restricted to explicitly allowed absolute files."""


class ScopedEdit(_ScopedWriteMixin, Edit):
    """``Edit`` restricted to explicitly allowed absolute files."""


def _tool_rules(workspace: Path, allowed_files: tuple[str, ...]) -> str:
    root = str(workspace)
    allowed_line = "、".join(allowed_files)
    return "\n".join(
        [
            "【AgentScope 文件工具（本轮的双手）】",
            f"- 工作区根目录（绝对路径）：{root}",
            "- Read / Write / Edit 只接受绝对路径；Glob 未给 path 时"
            "默认从工作区根目录开始搜索。",
            f"- 只允许修改名册列出的本领域文件：{allowed_line}；其他领域文件、"
            "tests/、conftest.py、pytest.ini、验收器一律不许改。",
            "- 禁止访问工作区之外的任何路径；不要读取或输出环境变量、密钥、"
            "配置文件。",
            "- 不要创建与根因无关的新文件；不要做重构或改格式。",
            "- 完成后用一条简短消息说明改了哪个文件、哪一行、为什么。",
            "- Shell 工具只许自查：pytest --collect-only 查导入与收集、"
            "用 pytest 跑本模块相关用例、git diff 看自己的改动面；"
            "其他命令会被拒绝。",
            "- 自查结果只作诊断：判卷只认平台冻结验收，自跑通过"
            "不等于修好，不许在交工说明里把自跑绿灯写成验收结论。",
        ]
    )


_NARROWED_GRANT_NOTE = (
    "【本次授权（程序核定）】\n"
    "- 本轮你只能修改上面列出的文件。根因若在授权之外的文件里，"
    "不要直接改它：在交工说明里单独写一行\n"
    "WIDEN-APPLY: <相对路径> | <理由>\n"
    "- 申请文件必须在本项目包内、且与本轮失败有关（出现在失败"
    "证据里，或被失败的测试引用）。程序核验通过后，下一轮起"
    "你的授权放宽到整个项目包；本轮强改授权外文件会被拒绝"
    "并记录。"
)

_WIDENED_GRANT_NOTE = (
    "【本次授权（程序核定）】\n"
    "- 你的授权已放宽到整个项目包：本轮可改包内任何文件；"
    "包外路径与 tests/ 仍不许改。"
)


_TASK_MESSAGE = (
    "请开始本轮修复。先用 Glob / Read 定位相关文件和根因，"
    "再对允许的文件做最小改动，最后简要汇报。"
)


def build_toolkit(
    workspace: Path,
    allowed_files: Iterable[str] | None = None,
    *,
    actor_id: str | None = None,
    extra_tools: list | None = None,
) -> Toolkit:
    """File tools plus the read-only Shell, rooted at *workspace* (no general ``Bash``).

    When *allowed_files* is given, ``Write`` and ``Edit`` are scoped to those
    workspace-relative files.  ``None`` keeps the old unrestricted-inside-
    workspace behaviour for direct callers.
    """
    backend = _WorkspaceBackend(workspace)
    if allowed_files is None:
        write_tool: Write = Write(backend=backend)
        edit_tool: Edit = Edit(backend=backend)
    else:
        allowed_paths = [Path(workspace) / item for item in allowed_files]
        write_tool = ScopedWrite(
            allowed_paths, workspace=workspace, backend=backend
        )
        edit_tool = ScopedEdit(
            allowed_paths, workspace=workspace, backend=backend
        )
    # 不装配搜索工具：本机没有系统 ``rg``（ripgrep），一调用必报错；
    # 定位代码用 Glob + Read 已足够。
    tools: list = [
        _PolicyRead(workspace=workspace, backend=backend),
        write_tool,
        edit_tool,
        Glob(backend=backend),
        ShellTool(
            workspace=workspace,
            actor=actor_id or "worker",
            on_denial=_record_shell_denial,
        ),
    ]
    if extra_tools:
        tools.extend(extra_tools)
    return Toolkit(tools=tools)


def build_state(workspace: Path) -> AgentState:
    """Agent state with unattended permissions scoped to *workspace*."""
    root = str(workspace)
    return AgentState(
        permission_context=PermissionContext(
            mode=PermissionMode.DONT_ASK,
            working_directories={
                root: AdditionalWorkingDirectory(
                    path=root,
                    source="platform-workspace",
                )
            },
        )
    )


def snapshot_roster_python(
    workspace: Path,
    roster: Roster,
) -> dict[str, str]:
    """Return ``relative path -> sha256`` for every .py under the roster package."""
    workspace = Path(workspace)
    package = workspace / roster.package
    if not package.is_dir():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(package.rglob("*.py")):
        if not path.is_file():
            continue
        snapshot[path.relative_to(workspace).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshot


_BACKSTOP_IGNORE = {"__pycache__", ".pytest_cache", ".git", ".drbug"}


def _snapshot_tree(workspace: Path) -> dict[str, str]:
    """Whole-tree ``relative path -> sha256`` for the hash backstop."""
    workspace = Path(workspace)
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        if any(part in _BACKSTOP_IGNORE for part in relative.parts):
            continue
        try:
            snapshot[relative.as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError:
            continue
    return snapshot


def _rollback_out_of_scope(
    workspace: Path,
    backup: Path,
    before: dict[str, str],
    allowed: tuple[str, ...],
) -> list[str]:
    """Roll back every change outside ``allowed``; return rolled-back paths.

    Compares the live tree against the pre-act snapshot ``before`` and
    restores changed/deleted files from ``backup`` (a full copy taken at
    the same moment); files created out of scope are deleted.  Only the
    worker's private copy is touched - never the original or the vault.
    """
    workspace = Path(workspace)
    after = _snapshot_tree(workspace)
    allowed_set = set(allowed)
    rolled_back: list[str] = []
    for relative in _changed_files(before, after):
        if relative in allowed_set:
            continue
        target = workspace / relative
        source = backup / relative
        try:
            if relative in before:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            elif target.is_file():
                target.unlink()
        except OSError:
            continue
        rolled_back.append(relative)
    return rolled_back


async def _run_agent(
    actor_id: str,
    system_prompt: str,
    workspace: Path,
    model: ChatModelBase,
    allowed_files: tuple[str, ...],
) -> str:
    from core.mcp_tools import MCPToolSet, load_mcp_servers

    toolset: MCPToolSet | None = None
    extra_tools: list = []
    if load_mcp_servers():
        toolset = MCPToolSet(workspace=workspace, actor=actor_id)
        extra_tools = await toolset.open()
    try:
        agent = Agent(
            name=actor_id,
            system_prompt=system_prompt,
            model=model,
            toolkit=build_toolkit(
                workspace,
                allowed_files,
                actor_id=actor_id,
                extra_tools=extra_tools,
            ),
            state=build_state(workspace),
        )
        task = Msg(
            name="dispatcher",
            role="user",
            content=[TextBlock(text=_TASK_MESSAGE)],
        )
        reply = await agent.reply(task)
        return reply.get_text_content() or ""
    finally:
        if toolset is not None:
            await toolset.close()


async def _run_with_deadline(
    actor_id: str,
    system_prompt: str,
    workspace: Path,
    model: ChatModelBase,
    timeout_s: float,
    allowed_files: tuple[str, ...],
) -> str:
    """Run one agent with a hard wall-clock deadline.

    ``Agent.reply`` catches cancellation internally and can still return an
    "interrupted" reply instead of propagating ``CancelledError``; both
    ``asyncio.wait_for`` and ``asyncio.timeout`` would then report normal
    completion.  Racing the task against an independent timer keeps the
    timeout observable: the task is cancelled at the deadline and the caller
    always gets ``TimeoutError``.
    """
    task = asyncio.ensure_future(
        _run_agent(actor_id, system_prompt, workspace, model, allowed_files)
    )
    done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    if task in done:
        return task.result()

    task.cancel()
    # Bounded grace period so a cooperative agent can unwind.  If it ignores
    # cancellation, asyncio.run's shutdown will cancel it again.
    await asyncio.wait({task}, timeout=1.0)
    if task.done() and not task.cancelled():
        task.exception()  # consume a swallowed-cancellation result
    raise TimeoutError(f"agent exceeded {timeout_s}s")


def _file_synthesized_archive(
    profile_dir: Path,
    actor_id: str,
    profile: SpecialistProfile,
) -> None:
    """File a synthesized specialist. Never overwrite an existing archive.

    ``source`` is written into the existing IDENTITY line so the roster can
    tell this person from an auto/manual employee. The dispatch table is
    left untouched: synthesis stays a runtime gap-fill, not a roster edit.
    """
    module = ""
    if profile.files:
        stem = PurePosixPath(profile.files[0]).stem
        if stem and stem != "__init__":
            module = stem
    try:
        ensure_archives(
            profile_dir,
            {
                actor_id: {
                    "files": list(profile.files),
                    "specialist": profile.specialist,
                    "module": module,
                    "source": "synthesized",
                }
            },
        )
    except Exception:
        # Archiving is bookkeeping. A failure must not change the repair.
        return


def _archive_suffix(
    profile_dir: Path | None,
    name: str,
    workspace: Path | None = None,
) -> str:
    """IDENTITY + MEMORY to append to a system prompt, or "" if absent.

    A missing archive leaves the prompt exactly as it was. MEMORY longer
    than 4000 characters is cut and marked. Read errors are ignored.
    """
    if profile_dir is None:
        return ""
    try:
        archive = read_archive(profile_dir, name)
    except Exception:
        return ""
    if archive is None:
        return ""
    memory = archive.memory
    if workspace is not None:
        memory = mark_stale_entries(memory, workspace)
    truncated = False
    if len(memory) > _MEMORY_INJECT_LIMIT:
        memory = memory[:_MEMORY_INJECT_LIMIT]
        truncated = True
    identity = archive.identity
    try:
        # VER1：版本头只在展示时拼进标题行之后；IDENTITY.md 一字节不碰。
        # 没有版本记录的员工头为空，注入内容与从前逐字节一致。
        from .agent_versions import version_header, with_version_header

        identity = with_version_header(
            identity, version_header(profile_dir, archive.name)
        )
    except Exception:  # noqa: BLE001 - display only
        identity = archive.identity
    parts = [identity.rstrip("\n")]
    memory_body = memory.rstrip("\n")
    if memory_body:
        parts.append(memory_body)
    if truncated:
        parts.append(_MEMORY_TRUNCATION_MARK)
    body = "\n\n".join(part for part in parts if part)
    if not body.strip():
        return ""
    return (
        "【员工档案】\n"
        "以下是你的身份与长期记忆。长期记忆只在验收 PASS 后由平台写入；"
        "不要自己改这些文件。\n\n"
        f"{body}\n"
    )


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _tail(text: str, max_chars: int = 600) -> str:
    compact = " | ".join(
        line.strip() for line in text.splitlines() if line.strip()
    )
    if len(compact) > max_chars:
        return "..." + compact[-max_chars:]
    return compact


def agentscope_act(
    actor_id: str,
    workspace: Path,
    assigned_nodes: list[str],
    failed_nodes: list[str],
    *,
    model: ChatModelBase | None = None,
    timeout_s: float = 600,
    roster: Roster | None = None,
    inventory: Mapping[str, object] | None = None,
    strict_scope: bool = False,
    node_files: Mapping[str, Iterable[str]] | None = None,
    module_files: Mapping[str, Iterable[str]] | None = None,
    scope_denials: list[dict] | None = None,
    profile_dir: Path | None = None,
    generalist_grant: str = "per_assignment",
    scope_state: dict | None = None,
    review_feedback: str = "",
    variant_note: str = "",
) -> FixResult:
    """Run one native AgentScope actor and report whether files changed.

    ``model`` is injectable for offline tests (any ``ChatModelBase``, e.g.
    ``agentscope_ext.fake_model.FakeChatModel``).  When omitted, the platform
    config builds a real model via :func:`core.model_config.build_model`.

    ``roster`` defaults to the demo roster.  It decides both the snapshot
    package and the hard write scope; the function never raises into the
    dispatch loop: timeouts and runtime errors are reported as
    ``applied=False`` with the cause in ``note``.

    ``inventory`` is the optional profile inventory (evidence module ->
    ``{"path", "blurb"}``).  With ``None`` nothing is synthesized and every
    path is byte-for-byte the previous behaviour.  When the roster has a gap
    for the assigned evidence module, a one-file specialist is synthesized
    and the ``note`` starts with ``[synthesized]``.

    ``strict_scope`` is the write-scope guard (GEN5 introduced it for
    repro-origin profiles; SCOPE1 turned it on for every native run).  It
    narrows the whole-package fallback generalist to
    :func:`evidence_scope_files` of its assigned nodes; every actor stays
    hard-limited to its roster ``files`` list either way.  ``scope_denials``
    is an optional caller-owned list that receives one dict per denied
    Write/Edit, plus one dict per file the hash backstop rolls back (the
    backstop is active exactly when a sink is wired).  There is no
    automatic widening inside an act: an actor that thinks the root cause
    is elsewhere must say so, and the runner decides between rounds.
    ``generalist_grant`` is the runner's grant mode for the fallback
    generalist: ``"per_assignment"`` narrows it (below) and offers the
    WIDEN-APPLY channel; ``"whole_package"`` never narrows. When the
    runner has already widened the grant it sets
    ``scope_state["widened"]``; the act reports the first grant it gave
    back through ``scope_state`` (``initial_files`` / ``narrowed``).
    """
    if roster is None:
        if profile_dir is None:
            raise ValueError(
                "agentscope_act 需要显式 roster 或 profile_dir"
                "（已无内置默认名册）"
            )
        _table = json.loads(
            (Path(profile_dir) / "dispatch_table.json").read_text(
                encoding="utf-8"
            )
        )
        roster = roster_from_table(_table)
    active_roster = roster
    workspace = Path(workspace).resolve()
    before = snapshot_roster_python(workspace, active_roster)
    allowed_files: tuple[str, ...] = ()
    # SCOPE1 hash backstop: when a denial sink is wired (fenced runs),
    # snapshot the whole tree plus a restorable copy, so changes that
    # bypass the tool layer can be rolled back after the act.
    backstop_before: dict[str, str] | None = None
    backstop_dir: Path | None = None
    if scope_denials is not None:
        backstop_before = _snapshot_tree(workspace)
        backstop_dir = Path(tempfile.mkdtemp(prefix="drbug-backstop-")) / "tree"
        shutil.copytree(
            workspace,
            backstop_dir,
            ignore=shutil.ignore_patterns(*_BACKSTOP_IGNORE),
        )

    started = time.monotonic()
    timed_out = False
    error: str | None = None
    reply_text = ""
    marker: str | None = None
    acting_id = actor_id

    try:
        effective_roster = active_roster
        if not active_roster.legacy_prompt:
            acting_id, acting_profile, marker = _resolve_actor(
                actor_id,
                list(assigned_nodes),
                active_roster,
                inventory,
                blocked_modules=_overlay_blocked_modules(profile_dir),
            )
            effective_roster = Roster(
                package=active_roster.package,
                specialists={
                    **active_roster.specialists,
                    acting_id: acting_profile,
                },
                symptoms=active_roster.symptoms,
                legacy_prompt=False,
            )
        acting_profile = effective_roster.profile(acting_id)
        narrowed_applied = False
        if (
            strict_scope
            and not acting_profile.specialist
            and generalist_grant == "per_assignment"
            and not (scope_state or {}).get("widened", False)
        ):
            narrowed = evidence_scope_files(
                assigned_nodes, node_files, module_files
            )
            if narrowed and tuple(narrowed) != acting_profile.files:
                acting_profile = replace(
                    acting_profile, files=narrowed
                )
                effective_roster = Roster(
                    package=effective_roster.package,
                    specialists={
                        **effective_roster.specialists,
                        acting_id: acting_profile,
                    },
                    symptoms=effective_roster.symptoms,
                    legacy_prompt=effective_roster.legacy_prompt,
                )
                marker = marker or (
                    f"[scope-narrowed] {acting_id} files="
                    + ",".join(narrowed)
                )
                narrowed_applied = True
        if (
            profile_dir is not None
            and marker
            and str(marker).startswith("[synthesized]")
        ):
            _file_synthesized_archive(
                Path(profile_dir), acting_id, acting_profile
            )
        if effective_roster.legacy_prompt:
            prompt = build_prompt(
                acting_id, list(assigned_nodes), list(failed_nodes)
            )
        else:
            prompt = build_roster_prompt(
                acting_id,
                list(assigned_nodes),
                list(failed_nodes),
                effective_roster,
            )
        allowed_files = effective_roster.allowed_files(acting_id)
        if (
            scope_state is not None
            and not acting_profile.specialist
            and generalist_grant == "per_assignment"
        ):
            scope_state.setdefault("initial_files", list(allowed_files))
            scope_state.setdefault("narrowed", narrowed_applied)
        python = prompt + "\n\n" + _tool_rules(workspace, allowed_files)
        archive_suffix = _archive_suffix(
            None if profile_dir is None else Path(profile_dir),
            acting_id,
            workspace,
        )
        if archive_suffix:
            python = python + "\n" + archive_suffix
        if strict_scope and not acting_profile.specialist:
            if generalist_grant == "per_assignment":
                if (scope_state or {}).get("widened", False):
                    python = python + "\n\n" + _WIDENED_GRANT_NOTE
                elif narrowed_applied:
                    python = python + "\n\n" + _NARROWED_GRANT_NOTE

        # VAR1：按失败分类选好的对症提醒段，有才拼（见 core.variants）。
        python = with_variant_note(python, variant_note)

        if review_feedback:
            python = (
                python
                + "\n\n\n"
                + review_feedback
            )

        active_model = model
        if active_model is None:
            from .model_config import build_model

            active_model = build_model(role="worker")

        denial_token = _SCOPE_DENIALS.set(scope_denials)
        actor_token = _SCOPE_ACTOR.set(acting_id)
        try:
            reply_text = asyncio.run(
                _run_with_deadline(
                    acting_id,
                    python,
                    workspace,
                    active_model,
                    timeout_s,
                    allowed_files,
                )
            )
        finally:
            _SCOPE_ACTOR.reset(actor_token)
            _SCOPE_DENIALS.reset(denial_token)
    except TimeoutError:
        timed_out = True
    except Exception as exc:  # noqa: BLE001 - must never break the loop
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started

    if backstop_before is not None and backstop_dir is not None:
        rolled_back = _rollback_out_of_scope(
            workspace, backstop_dir, backstop_before, allowed_files
        )
        if rolled_back and scope_denials is not None:
            for relative in rolled_back:
                scope_denials.append(
                    {
                        "actor_id": acting_id,
                        "tool": "hash_backstop",
                        "path": relative,
                        "reason": (
                            "changed outside the granted scope; "
                            "rolled back by the hash backstop"
                        ),
                    }
                )
        shutil.rmtree(backstop_dir.parent, ignore_errors=True)

    after = snapshot_roster_python(workspace, active_roster)
    changed = _changed_files(before, after)
    applied = not timed_out and error is None and bool(changed)

    if timed_out:
        status = f"timeout after {timeout_s}s"
    elif error is not None:
        status = f"error={error}"
    else:
        status = "completed"

    parts = [
        status,
        f"elapsed={elapsed:.2f}s",
        "changed=" + (", ".join(changed) if changed else "none"),
    ]
    tail = _tail(reply_text)
    if tail:
        parts.append("reply=" + tail)
    if marker:
        parts.insert(0, marker)
    note = "; ".join(parts)

    return FixResult(actor_id=actor_id, applied=applied, note=note)
