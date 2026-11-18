# -*- coding: utf-8 -*-
"""SHELL1: the worker's read-only diagnostic shell.

Three layers, in order:

1. A deterministic whitelist (this module): only ``pytest`` family
commands and ``git status`` / ``git diff`` may run. Redirects,
pipes through other tools, command substitution and bare
interpreters are refused here, before anything executes.
2. The PolicyEngine verdict for ``shell_exec`` (PERM1 machinery:
compound commands are split and judged per segment, high-risk
verbs and out-of-workspace paths are denied, the verdict is
audited to ``.drbug/audit.log``). Ask has no channel on the
worker face, so anything but allow is a refusal.
3. Seatbelt (``sandbox-exec``): the child runs with a write set of
exactly the work copy plus ``/private/tmp`` and ``/dev``, and
explicit read/write denies for credential paths. Step-0
experiments (2026-11-16, probes kept with the task sheet)
verified this profile shape blocks direct, child-process,
symlink, ``sed -i``, ``..`` and rename escapes, while a strict
read whitelist makes the interpreter SIGABRT - so reads stay
open except the deny list.

The worker's own test runs are diagnostics only: nothing in this
module feeds the runner's verdict.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk

from . import model_config
from .policy import (
    Decision,
    PolicyContext,
    PolicyEngine,
    discover_vaults,
    format_denial,
    split_command,
)

SHELL_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 8000

_SECRET_DIR_NAMES = (".ssh", ".gnupg", ".aws")


def _api_key_env_names() -> set[str]:
    """Env var names that hold provider keys, per the model catalog."""

    names: set[str] = set()
    try:
        catalog = model_config.load_profile_catalog()
    except Exception:
        return names
    profiles = getattr(catalog, "profiles", {}) or {}
    values = profiles.values() if isinstance(profiles, dict) else profiles
    for profile in values:
        env_name = getattr(profile, "api_key_env", None)
        if isinstance(env_name, str) and env_name:
            names.add(env_name)
    return names


def _scrubbed_env() -> dict[str, str]:
    """The child environment: inherited minus every provider key."""

    dropped = _api_key_env_names()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in dropped
    }
    venv_bin = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    env["PATH"] = venv_bin + (os.pathsep + path if path else "")
    return env


def _segment_refusal(text: str) -> str | None:
    if any(char in text for char in "|<>"):
        return "命令含管道或重定向，不在只读白名单内"
    try:
        argv = shlex.split(text)
    except ValueError:
        return "命令无法解析"
    if not argv:
        return "空命令段"
    if any(token in ("&", ";") for token in argv):
        return "命令含后台或分隔符，不在只读白名单内"
    head = Path(argv[0]).name
    if head == "pytest":
        return None
    if head in ("python", "python3"):
        if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "pytest":
            return None
        return "只许 python -m pytest，不许任意解释器执行"
    if head == "git":
        if len(argv) >= 2 and argv[1] in ("status", "diff"):
            for arg in argv[2:]:
                if arg == "--output" or arg.startswith("--output="):
                    return "git diff --output 会写文件，已拒绝"
            return None
        return "只许 git status / git diff"
    return f"不在只读白名单内: {head}"


def _whitelist_refusal(command: str) -> str | None:
    """Deterministic shape check; ``None`` means the shape is allowed."""

    if not command or not command.strip():
        return "空命令"
    if "$(" in command or "`" in command or "${" in command:
        return "命令含命令替换，不在只读白名单内"
    pieces = split_command(command)
    if not pieces:
        return "空命令"
    for piece in pieces:
        if piece.kind != "command":
            return "命令含重定向，不在只读白名单内"
        refusal = _segment_refusal(piece.text)
        if refusal is not None:
            return refusal
    return None


def command_verdict(
    command: str,
    *,
    workspace: Path,
    actor: str = "worker",
) -> Decision:
    """Whitelist first, then the engine; exactly one audit line.

    The whitelist is a pure shape check with no side effects, so it
    runs first; the engine's ``decide`` then writes the single audit
    record carrying the final verdict. A whitelist refusal is
    audited directly with its deny decision.
    """

    workspace = Path(workspace).resolve()
    engine = PolicyEngine(workspace=workspace)
    refusal = _whitelist_refusal(command)
    if refusal is not None:
        decision = Decision(
            "deny", 1, refusal, normalized_target=command
        )
        engine.audit(
            actor, "shell_exec", command, decision, workspace=workspace
        )
        return decision
    context = PolicyContext(
        mode="ask",
        surface="runtime",
        workspace=workspace,
        vault_paths=discover_vaults(workspace),
        role="worker",
        is_ai=True,
        actor=actor,
    )
    return engine.decide(actor, "shell_exec", command, context)


def _seatbelt_profile(workspace: Path) -> str:
    home = str(Path.home()).replace("\\", "\\\\").replace('"', '\\"')
    root = str(workspace).replace("\\", "\\\\").replace('"', '\\"')
    secret_dirs = "\n".join(
        f' (subpath "{home}/{name}")' for name in _SECRET_DIR_NAMES
    )
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        "(allow file-write*\n"
        f' (subpath "{root}")\n'
        ' (subpath "/private/tmp")\n'
        ' (subpath "/dev"))\n'
        "(deny file-read*\n"
        f"{secret_dirs}\n"
        f' (subpath "{home}/.config/drbug")\n'
        ' (regex #"\\\\.env[^/]*$"))\n'
        "(deny file-write*\n"
        ' (regex #"\\\\.env[^/]*$"))\n'
    )


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n……输出已截断"


def _execute(
    command: str,
    *,
    workspace: Path,
    timeout_s: int = SHELL_TIMEOUT_S,
) -> str:
    """Run *command* sandboxed. No verdict here - callers gate first."""

    workspace = Path(workspace).resolve()
    sandbox = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not Path(sandbox).exists():
        return "Shell 工具不可用：本机没有 sandbox-exec，未执行。"
    home_dir = workspace / ".drbug-home"
    tmp_dir = workspace / ".drbug-tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = workspace / ".drbug"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "shell_profile.sb"
    profile_path.write_text(_seatbelt_profile(workspace), encoding="utf-8")
    env = _scrubbed_env()
    env.update(
        {
            "HOME": str(home_dir),
            "TMPDIR": str(tmp_dir),
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "TERM": "dumb",
        }
    )
    proc = subprocess.Popen(
        [sandbox, "-f", str(profile_path), "--", "/bin/sh", "-c", command],
        cwd=str(workspace),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return _truncate(f"命令超时（{timeout_s} 秒）已被终止。\n{out}")
    return _truncate(f"退出码 {proc.returncode}\n{out}")


def run_shell(
    command: str,
    *,
    workspace: Path,
    actor: str = "worker",
    timeout_s: int = SHELL_TIMEOUT_S,
) -> str:
    """Gate, then run. This is the safe entry for non-agent callers."""

    decision = command_verdict(command, workspace=workspace, actor=actor)
    if decision.verdict != "allow":
        return f"命令被拒绝：{decision.reason}"
    return _execute(command, workspace=workspace, timeout_s=timeout_s)


class ShellTool(ToolBase):
    """The worker face of :func:`run_shell`.

    The protocol surface (``name`` / ``description`` /
    ``input_schema`` / ``__call__`` chunk shape) mirrors the native
    ``Bash`` tool in the installed framework; the execution body is
    this module's whitelist + Seatbelt path, never a general shell.
    ``check_permissions`` runs the same verdict the file tools use:
    engine first-class, ask becomes deny, denials are reported back
    through ``on_denial`` so the runner records them.
    """

    name: str = "Shell"
    # ToolBase 只声明不给默认值的两个类属性，子类必须自带
    # （原生 Bash 同为 False：命令共享工作副本、不并发；
    # 本工具白名单只读，is_read_only 如实 True）。
    # 缺了它们，框架在并行分发检查时会读到不存在的属性、
    # 整个工人回合以 AttributeError 崩掉（SHELL1-fix2）。
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    description: str = (
        "只读诊断命令（自查用）：只许 pytest 家族（如 pytest -q、"
        "python -m pytest --collect-only）与 git status、git diff；"
        "其他命令一律拒绝。工作目录锁定在本次运行的工作副本，"
        "结果只作诊断，判卷不认自跑结果。"
    )
    input_schema: dict = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的只读诊断命令",
            }
        },
        "required": ["command"],
    }

    def __init__(
        self,
        *,
        workspace: Path,
        actor: str = "worker",
        on_denial=None,
    ) -> None:
        super().__init__()
        self._workspace = Path(workspace).resolve()
        self._actor = actor
        self._on_denial = on_denial

    async def check_permissions(self, tool_input: dict, context):
        command = str(tool_input.get("command", "")).strip()
        decision = command_verdict(
            command, workspace=self._workspace, actor=self._actor
        )
        if decision.verdict == "allow":
            return await super().check_permissions(tool_input, context)
        if self._on_denial is not None:
            self._on_denial(command, decision.reason)
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=format_denial(decision),
            decision_reason=decision.reason,
        )

    async def call(self, *args, **kwargs):
        # 协议形态照原生 Bash.call（_bash.py 776-779）：async generator，
        # ToolChunk 包 TextBlock，带 ToolResultState 与 is_last。
        # 基类 __call__ 会转发到 call（_base.py 220-222）；覆盖 __call__
        # 会跳过中间件。执行体只走 run_shell（内含裁决），
        # check_permissions 已裁过时这里会再裁一次——审计多一行可接受，
        # 绕过 check_permissions 的直调也必须过裁决。
        command = str(kwargs.get("command", "")).strip()
        text = run_shell(
            command, workspace=self._workspace, actor=self._actor
        )
        if text.startswith("命令被拒绝") or text.startswith("命令超时") or (
            text.startswith("退出码 ") and not text.startswith("退出码 0")
        ):
            state = ToolResultState.ERROR
        else:
            state = ToolResultState.RUNNING
        yield ToolChunk(
            content=[TextBlock(text=text)],
            state=state,
            is_last=True,
        )
