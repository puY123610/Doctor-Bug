"""PolicyEngine：四层权限裁决、复合命令拆解与审计。

交互面的暂停/恢复不在这里实现。本模块只产出裁决和可编译进
AgentScope 的规则；ASK 由框架的 ToolCallState.ASKING /
confirm_results 承担。运行面使用框架 DONT_ASK（ASK 转 DENY）。

审计写入失败必须吞掉：裁决已经做出，落账失败不能把放行或拒绝
变成异常，否则门禁本身会挡住主流程。
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
import sysconfig
import unicodedata

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

ACTIONS = (
    "read_file",
    "write_file",
    "edit_file",
    "shell_exec",
    "run_platform_tool",
    "mcp_call",
    "cross_dir_read",
    "cross_dir_write",
)

_READ_ACTIONS = {"read_file", "cross_dir_read"}
_WRITE_ACTIONS = {"write_file", "edit_file", "cross_dir_write"}
_FILE_ACTIONS = _READ_ACTIONS | _WRITE_ACTIONS

_SECRET_DIR_NAMES = (".ssh", ".aws", ".gnupg")
_SECRET_BASENAMES = {
    "auth.json",
    "service.json",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
}
_HIGH_RISK_ARGV0 = {"rm", "mv", "chmod", "chown", "sudo", "kill", "curl", "wget"}
_SECRETARY_NAMES = {"secretary", "drbug", "主治医生", "调度员"}
_PLATFORM_NAMES = {"platform", "system", "human", "user", "老板"}

_VERDICTS = {"allow", "deny", "ask"}


class PolicyConfigError(ValueError):
    """策略文件损坏或字段不合法。调用方不得把这种情况当成放行。"""


@dataclass(frozen=True)
class Rule:
    """一条 Tier 2 / Tier 3 规则。``actor`` 与 ``action`` 可用 ``*``。"""

    actor: str
    action: str
    glob: str
    decision: str

    def __post_init__(self) -> None:
        if self.decision not in _VERDICTS:
            raise PolicyConfigError(
                f"decision 只能是 allow/deny/ask，收到 {self.decision!r}"
            )
        if not self.actor or not self.action or not self.glob:
            raise PolicyConfigError("规则的 actor、action、glob 都不能为空")


@dataclass(frozen=True)
class SegmentDecision:
    """复合命令里的一段。``never_permanent`` 留给 PERM2 封顶，本单不授予。"""

    text: str
    verdict: str
    tier: int
    reason: str
    never_permanent: bool = False
    kind: str = "command"


@dataclass(frozen=True)
class Decision:
    """一次裁决。``tier`` 为 1–4，数字越小越硬。"""

    verdict: str
    tier: int
    reason: str
    never_permanent: bool = False
    segments: tuple[SegmentDecision, ...] = ()
    normalized_target: str = ""


@dataclass
class PolicyContext:
    """一次裁决的环境。``mode`` 本单只实现 ``ask``。"""

    mode: str = "ask"
    surface: str = "interactive"
    workspace: Path | None = None
    vault_paths: tuple[Path, ...] = ()
    role: str = "secretary"
    is_ai: bool = True
    actor: str = "secretary"


@dataclass
class _Piece:
    text: str
    kind: str  # command | redirect_write | redirect_read


def role_for_actor(actor: str) -> tuple[str, bool]:
    """由名字推断角色与是否 AI。秘书名走交互面，其余具名 actor 视为工人。"""

    cleaned = str(actor or "").strip()
    lowered = cleaned.lower()
    if lowered in _SECRETARY_NAMES or cleaned in _SECRETARY_NAMES:
        return "secretary", True
    if lowered in _PLATFORM_NAMES or cleaned in _PLATFORM_NAMES:
        return "platform", False
    return "worker", True


def normalize_path(target: str, *, base: Path | None = None) -> Path:
    """展开 ``~``、相对路径、``..`` 与符号链接，得到真实路径。

    文件尚不存在时也规范化已存在的父级，避免软链目录把红线藏起来。
    """

    expanded = os.path.expanduser(str(target).strip())
    path = Path(expanded)
    if not path.is_absolute():
        root = base if base is not None else Path.cwd()
        path = Path(root) / path
    return path.resolve(strict=False)


def format_denial(decision: Decision) -> str:
    """工具结果里的拒绝原因。层级必须可见，方便验收和排障。"""

    return f"拒绝（tier {decision.tier}）: {decision.reason}"


def confirm_line(actor: str, action: str, target: str) -> str:
    """TUI 行内确认的一行。不是弹窗。"""

    return f"确认 {actor} {action} {target} ? 输入 Y 放行 / N 拒绝"


def glob_match(path: str, pattern: str) -> bool:
    """路径 glob。``*`` 不跨目录，``**`` 跨目录；无斜杠的模式匹配文件名。"""

    raw_path = os.path.expanduser(str(path)).replace("\\", "/")
    raw_pattern = os.path.expanduser(str(pattern)).replace("\\", "/")
    if raw_pattern in {"*", "**", "**/*"}:
        return True
    if "/" not in raw_pattern:
        name = raw_path.rstrip("/").rsplit("/", 1)[-1]
        return fnmatchcase(name, raw_pattern)
    return _parts_match(_glob_parts(raw_path), _glob_parts(raw_pattern))


def _glob_parts(value: str) -> list[str]:
    text = value.rstrip("/") or "/"
    parts = list(PurePosixPath(text).parts)
    return [part for part in parts if part not in {"", "/"}]


def _parts_match(path_parts: Sequence[str], pattern_parts: Sequence[str]) -> bool:
    if not pattern_parts:
        return not path_parts
    head, *rest = pattern_parts
    if head == "**":
        if _parts_match(path_parts, rest):
            return True
        if path_parts and _parts_match(path_parts[1:], pattern_parts):
            return True
        return False
    if not path_parts:
        return False
    if fnmatchcase(path_parts[0], head):
        return _parts_match(path_parts[1:], rest)
    return False


def _toml_string(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def dump_policies(path: Path, rules: Iterable[Rule]) -> None:
    """最小 TOML 写出。不引入第三方序列化库。"""

    lines: list[str] = []
    for rule in rules:
        if not isinstance(rule, Rule):
            raise PolicyConfigError(f"不是规则: {rule!r}")
        lines.extend(
            [
                "[[rule]]",
                f"actor = {_toml_string(rule.actor)}",
                f"action = {_toml_string(rule.action)}",
                f"glob = {_toml_string(rule.glob)}",
                f"decision = {_toml_string(rule.decision)}",
                "",
            ]
        )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def load_policies(path: Path) -> list[Rule]:
    """读取 policies.toml。文件不存在视为没有规则；损坏则报错，不放行。"""

    source = Path(path)
    if not source.exists():
        return []
    if not source.is_file():
        raise PolicyConfigError(f"策略路径不是文件: {source}")
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyConfigError(f"无法读取策略文件 {source}: {exc}") from exc
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyConfigError(f"策略文件损坏 {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyConfigError(f"策略文件损坏 {source}: 根节点不是表")
    raw_rules = data.get("rule", [])
    if raw_rules is None:
        return []
    if not isinstance(raw_rules, list):
        raise PolicyConfigError(f"策略文件损坏 {source}: rule 不是数组")
    parsed: list[Rule] = []
    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise PolicyConfigError(f"策略文件损坏 {source}: 第 {index} 条不是表")
        missing = [key for key in ("actor", "action", "glob", "decision") if key not in item]
        if missing:
            raise PolicyConfigError(
                f"策略文件损坏 {source}: 第 {index} 条缺少 {', '.join(missing)}"
            )
        try:
            parsed.append(
                Rule(
                    actor=str(item["actor"]),
                    action=str(item["action"]),
                    glob=str(item["glob"]),
                    decision=str(item["decision"]),
                )
            )
        except PolicyConfigError as exc:
            raise PolicyConfigError(f"策略文件损坏 {source}: 第 {index} 条 {exc}") from exc
    return parsed


def format_rule_toml(rule: Rule) -> str:
    """One ``[[rule]]`` block, exactly as it lands in policies.toml.

    PERM2's approval modal previews this text before anything is
    written; the preview and the file content come from this one
    function so they can never drift apart.
    """

    def _quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return (
        "[[rule]]\n"
        f"actor = {_quoted(rule.actor)}\n"
        f"action = {_quoted(rule.action)}\n"
        f"glob = {_quoted(rule.glob)}\n"
        f"decision = {_quoted(rule.decision)}\n"
    )


def append_global_rule(
    rule: Rule, *, path: Path | None = None
) -> Path:
    """Append *rule* to the global policies.toml (Tier 3).

    The existing file must parse first - a corrupt policy file is
    never "repaired" by appending to it. Returns the written path.
    """

    target = Path(path) if path is not None else default_global_policy_path()
    existing = ""
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing.strip():
            load_policies(target)
    text = existing
    if text and not text.endswith("\n"):
        text += "\n"
    text += format_rule_toml(rule)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def remove_policy_rule(path: Path, index: int) -> Rule:
    """从策略文件删第 ``index`` 条规则并整文件重写，返回被删规则。

    文件解析失败原样抛 PolicyConfigError——坏文件不许借删除
    之名被「修好」；index 越界抛 IndexError。
    """

    rules = load_policies(path)
    removed = rules.pop(index)
    text = "".join(format_rule_toml(rule) for rule in rules)
    Path(path).write_text(text, encoding="utf-8")
    return removed


def discover_vaults(
    workspace: Path | None = None,
    profile_dir: Path | None = None,
) -> tuple[Path, ...]:
    """当前可能被写到的卷宗目录。平台 acceptor/vault 始终在列。"""

    found: list[Path] = []
    for relative in ("acceptor/vault", "acceptor/vault2"):
        candidate = PLATFORM_ROOT / relative
        if candidate.is_dir():
            found.append(candidate.resolve())
    extras: list[Path] = []
    if profile_dir is not None:
        extras.append(Path(profile_dir) / "vault")
    roots: list[Path] = []
    if workspace is not None:
        resolved = Path(workspace).expanduser().resolve()
        if resolved == PLATFORM_ROOT or PLATFORM_ROOT in resolved.parents:
            roots.append(PLATFORM_ROOT / "profiles")
        else:
            roots.append(resolved / ".drbug" / "profiles")
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            extras.append(child / "vault")
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in found + extras:
        try:
            if not path.is_dir():
                continue
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(resolved)
    return tuple(ordered)


def is_test_path(path: Path) -> bool:
    """与名册同一口径：tests 目录、test_*.py、*_test.py、conftest、pytest.ini。"""

    name = path.name
    if name in {"conftest.py", "pytest.ini"}:
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    return "tests" in path.parts


_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")


def _fold(text: str) -> str:
    """大小写折叠 + Unicode NFC 归一（APFS 绕法防线）。"""

    return unicodedata.normalize("NFC", text).casefold()


_SECRET_BASENAMES_FOLDED = frozenset(_fold(name) for name in _SECRET_BASENAMES)
_SECRET_DIR_NAMES_FOLDED = frozenset(_fold(name) for name in _SECRET_DIR_NAMES)


def is_credential_file(path: Path) -> bool:
    """凭据类文件名（任意目录命中）；示例后缀放行。"""

    name = _fold(path.name)
    if name.endswith(_EXAMPLE_SUFFIXES):
        return False
    if name in _SECRET_BASENAMES_FOLDED:
        return True
    return name == ".env" or name.startswith(".env.")


def library_read_roots() -> tuple[Path, ...]:
    """只读白名单根：当前解释器的标准库与 site-packages。

    用 sysconfig 的解释器路径（stdlib/platstdlib/
    purelib/platlib），不收任意 sys.path 插入项。
    """

    roots: list[Path] = []
    seen: set[Path] = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        raw = sysconfig.get_paths().get(key)
        if not raw:
            continue
        root = Path(raw).resolve()
        if root.is_dir() and root not in seen:
            seen.add(root)
            roots.append(root)
    return tuple(roots)


def _under_library_root(path: Path, workspace: Path) -> bool:
    for root in library_read_roots():
        if root == workspace or _is_under(root, workspace) or _is_under(workspace, root):
            continue
        if path == root or _is_under(path, root):
            return True
    return False

def is_secret_path(path: Path) -> bool:
    """密钥目录与密钥文件名。只看路径，不打开文件。

    匹配在折叠后的名字上做：大小写变体与 NFD 拼写
    都绕不过去；符号链接由 normalize_path 在裁决前
    已解析成真实路径。
    """

    resolved = path
    home = Path.home().resolve()
    for dirname in _SECRET_DIR_NAMES:
        root = (home / dirname).resolve()
        if resolved == root or _is_under(resolved, root):
            return True
    config_root = (home / ".config" / "drbug").resolve()
    if resolved == config_root or _is_under(resolved, config_root):
        return True
    if any(_fold(part) in _SECRET_DIR_NAMES_FOLDED for part in resolved.parts):
        return True
    if is_credential_file(resolved):
        return True
    lowered = _fold(resolved.name)
    if lowered.endswith(".pem") or lowered.endswith(".key"):
        return True
    if "credential" in lowered or "secret" in lowered:
        return True
    return False


def is_policies_file(path: Path) -> bool:
    """策略文件本身。秘书工具不能改它来给自己放权。"""

    if path.name != "policies.toml":
        return False
    parent = path.parent
    if parent.name == ".drbug":
        return True
    if parent.name == "drbug" and parent.parent.name == ".config":
        return True
    return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _argv(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _command_name(token: str) -> str:
    return Path(token).name if token else ""


def is_high_risk_command(text: str) -> bool:
    """高危动词。永久授予的封顶标记与硬拒绝都认这份表。"""

    argv = _argv(text)
    if not argv:
        return False
    verb = _command_name(argv[0])
    if verb in _HIGH_RISK_ARGV0:
        return True
    if verb == "pip" and "install" in argv[1:]:
        return True
    if verb in {"python", "python3"} and "pip" in argv and "install" in argv:
        return True
    return False


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.startswith(("~", "/", ".")) or "/" in token or token in {".", ".."}:
        return True
    return False


def split_command(command: str) -> list[_Piece]:
    """按 ``&&`` ``||`` ``;`` ``|`` ``$()`` 反引号和重定向拆段。引号内不拆。"""

    pieces: list[_Piece] = []
    buffer: list[str] = []
    index = 0
    length = len(command)

    def flush() -> None:
        text = "".join(buffer).strip()
        buffer.clear()
        if text:
            pieces.append(_Piece(text, "command"))

    while index < length:
        char = command[index]
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            while index < length and command[index] != quote:
                if command[index] == "\\" and quote == '"' and index + 1 < length:
                    buffer.append(command[index])
                    buffer.append(command[index + 1])
                    index += 2
                    continue
                buffer.append(command[index])
                index += 1
            if index < length:
                buffer.append(command[index])
                index += 1
            continue
        if char == "`":
            end = command.find("`", index + 1)
            if end < 0:
                buffer.append(char)
                index += 1
                continue
            pieces.extend(split_command(command[index + 1 : end]))
            index = end + 1
            continue
        if char == "$" and index + 1 < length and command[index + 1] == "(":
            end = _matching_paren(command, index + 1)
            pieces.extend(split_command(command[index + 2 : end]))
            index = end + 1
            continue
        if command.startswith("&&", index) or command.startswith("||", index):
            flush()
            index += 2
            continue
        if char in {";", "|"}:
            flush()
            index += 1
            continue
        if char in {">", "<"} or (
            char.isdigit() and _redirection_at(command, index)
        ):
            flush()
            kind, index, target = _read_redirection(command, index)
            if target:
                pieces.append(_Piece(target, kind))
            continue
        buffer.append(char)
        index += 1
    flush()
    return pieces


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char in {"'", '"'}:
            quote = char
            index += 1
            while index < len(text) and text[index] != quote:
                index += 1
            index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text) - 1 if text else 0


def _redirection_at(text: str, index: int) -> bool:
    cursor = index
    while cursor < len(text) and text[cursor].isdigit():
        cursor += 1
    return cursor < len(text) and text[cursor] in {">", "<"}


def _read_redirection(text: str, index: int) -> tuple[str, int, str]:
    while index < len(text) and text[index].isdigit():
        index += 1
    if index >= len(text):
        return "redirect_write", index, ""
    if text[index] == "<":
        kind = "redirect_read"
        index += 1
    else:
        kind = "redirect_write"
        index += 1
        if index < len(text) and text[index] in {">", "&", "|"}:
            index += 1
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return kind, index, ""
    if text[index] in {"'", '"'}:
        quote = text[index]
        index += 1
        start = index
        while index < len(text) and text[index] != quote:
            index += 1
        target = text[start:index]
        if index < len(text):
            index += 1
        return kind, index, target
    start = index
    while index < len(text) and not text[index].isspace() and text[index] not in {";", "|", "&", ">", "<"}:
        index += 1
    return kind, index, text[start:index]


def _is_compound(command: str) -> bool:
    pieces = split_command(command)
    if len(pieces) != 1:
        return True
    only = pieces[0]
    return only.kind != "command"


def default_global_policy_path() -> Path:
    return Path.home() / ".config" / "drbug" / "policies.toml"


def project_policy_path(workspace: Path) -> Path:
    return Path(workspace) / ".drbug" / "policies.toml"


@dataclass
class PolicyEngine:
    """四层裁决。Tier 1 是代码常量，任何规则文件都不能覆盖。"""

    workspace: Path | None = None
    global_policy_path: Path | None = None
    session_rules: list[Rule] = field(default_factory=list)
    risk_assessor: Any = field(default=None, repr=False)
    _cache: dict[str, tuple[tuple[int, int], list[Rule] | PolicyConfigError]] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.global_policy_path is None:
            self.global_policy_path = default_global_policy_path()

    def add_session_rule(self, rule: Rule) -> None:
        """Tier 2。只活在本进程，退出即失效。"""

        if not isinstance(rule, Rule):
            raise PolicyConfigError(f"不是规则: {rule!r}")
        self.session_rules.append(rule)

    def clear_session_rules(self) -> None:
        self.session_rules.clear()

    def remove_session_rule(self, rule: Rule) -> bool:
        """撤销一条完全相同的会话规则（Tier 2）；没有返回 False。"""

        try:
            self.session_rules.remove(rule)
        except ValueError:
            return False
        return True

    def evaluate(
        self,
        actor: str,
        action: str,
        target: str,
        ctx: PolicyContext | None = None,
    ) -> Decision:
        """按 Tier 1 → 2 → 3 → 4 裁决。损坏的策略文件抛 PolicyConfigError。"""

        context = ctx or self._default_context(actor)
        if action == "shell_exec" and _is_compound(target):
            return self.evaluate_command(target, context)
        return self._evaluate_one(actor, action, target, context)

    def evaluate_command(
        self,
        command: str,
        ctx: PolicyContext | None = None,
    ) -> Decision:
        """逐段裁决。任一段 deny 则整条 deny，任一段 ask 则整条 ask。"""

        context = ctx or self._default_context("secretary")
        actor = context.actor or "secretary"
        pieces = split_command(command)
        if not pieces:
            return Decision(
                "allow",
                4,
                "空命令",
                normalized_target=command,
            )
        segments: list[SegmentDecision] = []
        for piece in pieces:
            segments.append(self._evaluate_piece(actor, piece, context))
        return _aggregate(command, segments)

    def decide(
        self,
        actor: str,
        action: str,
        target: str,
        ctx: PolicyContext | None = None,
    ) -> Decision:
        """裁决并落审计。策略损坏时返回 deny，绝不放行。"""

        context = ctx or self._default_context(actor)
        try:
            decision = self.evaluate(actor, action, target, context)
        except PolicyConfigError as exc:
            decision = Decision(
                "deny",
                3,
                f"策略文件损坏，拒绝放行: {exc}",
                normalized_target=target,
            )
        self.audit(actor, action, target, decision, workspace=context.workspace)
        return decision

    def audit(
        self,
        actor: str,
        action: str,
        target: str,
        decision: Decision,
        *,
        workspace: Path | None = None,
    ) -> None:
        """追加一条 JSONL。写失败只丢这一条，不影响裁决。

        审计写失败不得影响主流程：目录不可写、磁盘满、路径被文件占住
        都在这里吞掉。门禁该拒绝仍然拒绝，该放行仍然放行。
        """

        root = workspace if workspace is not None else self.workspace
        if root is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "verdict": decision.verdict,
            "tier": decision.tier,
            "reason": decision.reason,
        }
        path = Path(root) / ".drbug" / "audit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # 审计写失败不得影响主流程。上面的异常包括权限、磁盘和
            # 「.drbug 已经是个文件」这类路径冲突；丢掉这一条日志，
            # 不能把已经做出的裁决变成抛给工具调用方的异常。
            return

    def framework_deny_globs(self, ctx: PolicyContext) -> list[tuple[str, str]]:
        """Tier 1 编译成 (glob, action) 。action 是本引擎词表，不是工具名。"""

        globs: list[tuple[str, str]] = []
        for vault in ctx.vault_paths:
            resolved = str(Path(vault).resolve())
            for action in ("write_file", "edit_file", "cross_dir_write"):
                globs.append((resolved, action))
                globs.append((resolved.rstrip("/") + "/**", action))
        home = Path.home().resolve()
        for dirname in _SECRET_DIR_NAMES:
            root = str((home / dirname).resolve())
            for action in _FILE_ACTIONS:
                globs.append((root, action))
                globs.append((root.rstrip("/") + "/**", action))
        for pattern in ("*.pem", "*.key", ".env", ".env.*", "auth.json", "service.json"):
            for action in _FILE_ACTIONS:
                globs.append((pattern, action))
        for pattern in ("**/.drbug/policies.toml",):
            for action in _WRITE_ACTIONS:
                globs.append((pattern, action))
        global_policy = str(self.global_policy_path or default_global_policy_path())
        for action in _WRITE_ACTIONS:
            globs.append((global_policy, action))
        for pattern in ("**/test_*.py", "**/*_test.py", "**/conftest.py", "**/pytest.ini", "**/tests/**"):
            for action in ("write_file", "edit_file"):
                globs.append((pattern, action))
        return globs

    def _default_context(self, actor: str) -> PolicyContext:
        role, is_ai = role_for_actor(actor)
        workspace = self.workspace
        return PolicyContext(
            mode="ask",
            surface="runtime" if role == "worker" else "interactive",
            workspace=workspace,
            vault_paths=discover_vaults(workspace),
            role=role,
            is_ai=is_ai,
            actor=actor,
        )

    def _evaluate_piece(
        self,
        actor: str,
        piece: _Piece,
        ctx: PolicyContext,
    ) -> SegmentDecision:
        if piece.kind == "redirect_write":
            decision = self._evaluate_one(actor, "write_file", piece.text, ctx)
        elif piece.kind == "redirect_read":
            decision = self._evaluate_one(actor, "read_file", piece.text, ctx)
        else:
            decision = self._evaluate_shell_segment(actor, piece.text, ctx)
        return SegmentDecision(
            text=piece.text,
            verdict=decision.verdict,
            tier=decision.tier,
            reason=decision.reason,
            never_permanent=decision.never_permanent or is_high_risk_command(piece.text),
            kind=piece.kind,
        )

    def _evaluate_shell_segment(
        self,
        actor: str,
        text: str,
        ctx: PolicyContext,
    ) -> Decision:
        high_risk = is_high_risk_command(text)
        if high_risk:
            verb = _command_name(_argv(text)[0]) if _argv(text) else text
            return Decision(
                "deny",
                1,
                f"高危命令禁止执行: {verb}",
                never_permanent=True,
                normalized_target=text,
            )
        decision = self._evaluate_one(actor, "shell_exec", text, ctx)
        worst = decision
        for token in _argv(text):
            if not _looks_like_path(token):
                continue
            path_decision = self._evaluate_one(actor, "read_file", token, ctx)
            if path_decision.verdict == "allow":
                continue
            if path_decision.verdict == "deny" or worst.verdict == "allow":
                worst = path_decision
            if worst.verdict == "deny":
                break
        if high_risk:
            worst = Decision(
                worst.verdict,
                worst.tier,
                worst.reason,
                True,
                worst.segments,
                worst.normalized_target,
            )
        return worst

    def _evaluate_one(
        self,
        actor: str,
        action: str,
        target: str,
        ctx: PolicyContext,
    ) -> Decision:
        if action not in ACTIONS:
            return Decision(
                "deny",
                4,
                f"未知动作 {action}，拒绝放行",
                normalized_target=target,
            )
        normalized = ""
        path: Path | None = None
        if action in _FILE_ACTIONS:
            base = ctx.workspace
            path = normalize_path(target, base=base)
            normalized = str(path)

        tier1 = self._tier1(actor, action, target, path, ctx)
        if tier1 is not None:
            return tier1

        tier2 = self._match_rules(self.session_rules, actor, action, target, normalized, ctx, tier=2)
        if tier2 is not None:
            decision = tier2
        else:
            tier3 = self._tier3(actor, action, target, normalized, ctx)
            if tier3 is not None:
                decision = tier3
            else:
                decision = self._tier4(action, target, path, normalized, ctx)
        if (
            decision.verdict == "ask"
            and ctx.surface == "interactive"
            and ctx.mode == "auto-review"
            and _auto_review_ok(action, path, ctx)
        ):
            if self.risk_assessor is not None:
                try:
                    escalate = bool(
                        self.risk_assessor(actor, action, target)
                    )
                except Exception:  # noqa: BLE001 - 评估器坏了按升级算
                    escalate = True
                if escalate:
                    return Decision(
                        "ask",
                        decision.tier,
                        decision.reason + "（模型风险评估升级人工）",
                        never_permanent=decision.never_permanent,
                        normalized_target=decision.normalized_target,
                    )
            return Decision(
                "allow",
                decision.tier,
                f"[自动审核放行] {action}（规则清单·工作区内）",
                normalized_target=decision.normalized_target,
            )
        return decision

    def _tier1(
        self,
        actor: str,
        action: str,
        target: str,
        path: Path | None,
        ctx: PolicyContext,
    ) -> Decision | None:
        if action == "shell_exec" and is_high_risk_command(target):
            verb = _command_name(_argv(target)[0]) if _argv(target) else target
            return Decision(
                "deny",
                1,
                f"高危命令禁止执行: {verb}",
                never_permanent=True,
                normalized_target=target,
            )
        if path is None:
            return None
        if ctx.is_ai and action in _FILE_ACTIONS and self._hits_vault(path, ctx):
            return Decision(
                "deny",
                1,
                f"卷宗对 AI 禁读禁写: {path}",
                normalized_target=str(path),
            )

        if action in _WRITE_ACTIONS and self._hits_vault(path, ctx):
            return Decision(
                "deny",
                1,
                f"卷宗禁止写入: {path}",
                normalized_target=str(path),
            )
        if action in _WRITE_ACTIONS and ctx.role == "worker" and is_test_path(path):
            return Decision(
                "deny",
                1,
                f"测试文件对工人禁写: {path}",
                normalized_target=str(path),
            )
        if ctx.is_ai and action in _FILE_ACTIONS and is_secret_path(path):
            return Decision(
                "deny",
                1,
                f"密钥路径对 AI 禁读禁写: {path}",
                normalized_target=str(path),
            )
        if (
            ctx.is_ai
            and ctx.role == "secretary"
            and action in _WRITE_ACTIONS
            and is_policies_file(path)
        ):
            return Decision(
                "deny",
                1,
                f"秘书工具不能改写策略文件: {path}",
                normalized_target=str(path),
            )
        return None

    def _hits_vault(self, path: Path, ctx: PolicyContext) -> bool:
        for vault in ctx.vault_paths:
            root = Path(vault).resolve()
            if path == root or _is_under(path, root):
                return True
        return False

    def _tier3(
        self,
        actor: str,
        action: str,
        target: str,
        normalized: str,
        ctx: PolicyContext,
    ) -> Decision | None:
        rules: list[Rule] = []
        rules.extend(self._read_policy(Path(self.global_policy_path or default_global_policy_path())))
        if ctx.workspace is not None:
            rules.extend(self._read_policy(project_policy_path(ctx.workspace)))
        elif self.workspace is not None:
            rules.extend(self._read_policy(project_policy_path(self.workspace)))
        return self._match_rules(rules, actor, action, target, normalized, ctx, tier=3)

    def _read_policy(self, path: Path) -> list[Rule]:
        source = Path(path)
        if not source.exists():
            return []
        try:
            stat = source.stat()
        except OSError as exc:
            raise PolicyConfigError(f"无法读取策略文件 {source}: {exc}") from exc
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(str(source))
        if cached is not None and cached[0] == key:
            loaded = cached[1]
            if isinstance(loaded, PolicyConfigError):
                raise loaded
            return loaded
        try:
            loaded_rules = load_policies(source)
        except PolicyConfigError as exc:
            self._cache[str(source)] = (key, exc)
            raise
        self._cache[str(source)] = (key, loaded_rules)
        return loaded_rules

    def _match_rules(
        self,
        rules: Sequence[Rule],
        actor: str,
        action: str,
        target: str,
        normalized: str,
        ctx: PolicyContext,
        *,
        tier: int,
    ) -> Decision | None:
        matched = [
            rule
            for rule in rules
            if _rule_matches(rule, actor, action, target, normalized)
        ]
        if not matched:
            return None
        denied = [rule for rule in matched if rule.decision == "deny"]
        if denied:
            rule = denied[0]
            return Decision(
                "deny",
                tier,
                f"Tier {tier} 规则拒绝 {rule.actor} {rule.action} {rule.glob}",
                normalized_target=normalized or target,
            )
        asked = [rule for rule in matched if rule.decision == "ask"]
        if asked:
            if ctx.surface == "runtime":
                # 运行面不弹窗。ask 不是开工授权的拦截条件，显式 deny 已经处理过。
                return None
            rule = asked[0]
            return Decision(
                "ask",
                tier,
                f"Tier {tier} 规则要求确认 {rule.actor} {rule.action} {rule.glob}",
                normalized_target=normalized or target,
            )
        allowed = [rule for rule in matched if rule.decision == "allow"]
        if allowed:
            rule = allowed[0]
            return Decision(
                "allow",
                tier,
                f"Tier {tier} 规则放行 {rule.actor} {rule.action} {rule.glob}",
                normalized_target=normalized or target,
            )
        return None

    def _tier4(
        self,
        action: str,
        target: str,
        path: Path | None,
        normalized: str,
        ctx: PolicyContext,
    ) -> Decision:
        if ctx.mode not in {"ask", "auto-edit", "auto-review"}:
            return Decision(
                "deny",
                4,
                f"模式 {ctx.mode} 尚未实现，拒绝放行",
                normalized_target=normalized or target,
            )
        if action == "mcp_call":
            if ctx.surface == "interactive":
                return Decision(
                    "ask",
                    4,
                    "MCP 外接工具调用需老板确认"
                    "（默认询问，可用规则放行）",
                    normalized_target=normalized or target,
                )
            return Decision(
                "deny",
                4,
                "MCP 外接工具默认拒绝：需策略规则明确放行"
                "（如在 .drbug/policies.toml 写 allow 规则）",
                normalized_target=normalized or target,
            )
        if ctx.surface == "runtime":
            if ctx.mode != "ask":
                return Decision(
                    "deny",
                    4,
                    f"模式 {ctx.mode} 只作用于交互面，运行面拒绝放行",
                    normalized_target=normalized or target,
                )
            if (
                ctx.role == "worker"
                and action in _FILE_ACTIONS
                and path is not None
                and ctx.workspace is not None
            ):
                root = Path(ctx.workspace).expanduser().resolve()
                if path == root or _is_under(path, root):
                    return Decision(
                        "allow",
                        4,
                        "运行面：工作区内访问放行（红线与显式 deny 未命中）",
                        normalized_target=normalized or target,
                    )
                if action in _READ_ACTIONS and _under_library_root(path, root):
                    return Decision(
                        "allow",
                        4,
                        "运行面：标准库/site-packages 只读放行",
                        normalized_target=normalized or target,
                    )
                return Decision(
                    "deny",
                    4,
                    "运行面：工作区外访问拒绝（工人面无确认通道）",
                    normalized_target=normalized or target,
                )
            return Decision(
                "allow",
                4,
                "运行面开工授权放行（红线与显式 deny 未命中）",
                normalized_target=normalized or target,
            )
        inside = False
        if path is not None and ctx.workspace is not None:
            inside = _is_under(path, Path(ctx.workspace).expanduser().resolve()) or path == Path(ctx.workspace).expanduser().resolve()
        if (
            ctx.mode == "auto-edit"
            and action in {"write_file", "edit_file"}
            and inside
        ):
            return Decision(
                "allow",
                4,
                "auto-edit 模式：工作区内普通文件编辑放行（删除、跨目录与敏感文件仍需确认）",
                normalized_target=normalized or target,
            )
        if action == "read_file" and inside:
            return Decision(
                "allow",
                4,
                "ask 模式：工作区内读取默认放行",
                normalized_target=normalized or target,
            )
        if action in _WRITE_ACTIONS or action in {"cross_dir_read", "cross_dir_write"}:
            return Decision(
                "ask",
                4,
                "ask 模式：写入或跨工作区访问需要确认",
                normalized_target=normalized or target,
            )
        if action == "shell_exec":
            return Decision(
                "allow",
                4,
                "未命中规则的命令按诊断放行",
                normalized_target=target,
            )
        if action == "run_platform_tool":
            return Decision(
                "allow",
                4,
                "工作区内平台工具沿用前台放行",
                normalized_target=target,
            )
        return Decision(
            "ask",
            4,
            "ask 模式：未命中规则，默认询问",
            normalized_target=normalized or target,
        )


def _auto_review_ok(
    action: str,
    path: Path | None,
    ctx: PolicyContext,
) -> bool:
    """auto-review 的确定性安全清单（v1）：只读 + 工作区内 +
    非敏感非凭据。三条全过才许把 ask 转 allow；deny 永不转。
    """

    if action not in _READ_ACTIONS:
        return False
    if path is None or ctx.workspace is None:
        return False
    root = Path(ctx.workspace).expanduser().resolve()
    if not (path == root or _is_under(path, root)):
        return False
    if is_secret_path(path) or is_credential_file(path):
        return False
    return True


def _rule_matches(
    rule: Rule,
    actor: str,
    action: str,
    target: str,
    normalized: str,
) -> bool:
    if rule.actor not in {"*", actor}:
        return False
    if rule.action not in {"*", action}:
        return False
    if action == "shell_exec":
        return fnmatchcase(target, rule.glob) or fnmatchcase(target.strip(), rule.glob)
    candidate = normalized or target
    return glob_match(candidate, rule.glob)


def _aggregate(command: str, segments: list[SegmentDecision]) -> Decision:
    denied = [segment for segment in segments if segment.verdict == "deny"]
    asked = [segment for segment in segments if segment.verdict == "ask"]
    never = any(segment.never_permanent for segment in segments)
    if denied:
        chosen = denied[0]
        verdict, tier, reason = "deny", chosen.tier, chosen.reason
    elif asked:
        chosen = asked[0]
        verdict, tier, reason = "ask", chosen.tier, chosen.reason
    else:
        verdict, tier, reason = "allow", 4, "各段均放行"
    return Decision(
        verdict,
        tier,
        reason,
        never_permanent=never,
        segments=tuple(segments),
        normalized_target=command,
    )


def install_permission_hook(agent: object) -> None:
    """在框架权限检查之后补上引擎理由，并把漏网的 Tier 1 改成 DENY。

    不另写暂停循环。ASK 仍由 AgentScope 的 check_permission 返回值驱动。
    """

    from agentscope.permission import PermissionBehavior

    engine = getattr(agent, "_engine", None)
    if engine is None or getattr(engine, "_drbug_policy_hook", False):
        return
    original = engine.check_permission

    async def wrapped(tool: object, tool_input: dict) -> object:
        decision = await original(tool, tool_input)
        decider = getattr(tool, "policy_decision", None)
        if not callable(decider):
            return decision
        try:
            ours = decider(tool_input)
        except PolicyConfigError as exc:
            ours = Decision("deny", 3, f"策略文件损坏，拒绝放行: {exc}")
        if ours is None:
            return decision
        auditor = getattr(tool, "policy_audit", None)
        if callable(auditor):
            try:
                auditor(tool_input, ours)
            except Exception:
                # 审计失败已在 PolicyEngine.audit 里吞掉；这里再挡一层，
                # 防止钩子自身的记录逻辑把框架裁决变成异常。
                pass
        if ours.verdict == "deny":
            decision.behavior = PermissionBehavior.DENY
            decision.message = format_denial(ours)
            decision.decision_reason = ours.reason
        return decision

    engine.check_permission = wrapped
    engine._drbug_policy_hook = True


_ACTION_TO_TOOL_KIND = {
    "read_file": "read",
    "cross_dir_read": "read",
    "write_file": "write",
    "edit_file": "write",
    "cross_dir_write": "write",
}


def compile_deny_rules(ctx: PolicyContext, *, tool_names_for_kind: dict[str, tuple[str, ...]]):
    """把 Tier 1 红线编译成 AgentScope deny_rules 需要的 PermissionRule 列表。"""

    from agentscope.permission import PermissionBehavior, PermissionRule

    engine = PolicyEngine()
    rules = []
    for pattern, action in engine.framework_deny_globs(ctx):
        kind = _ACTION_TO_TOOL_KIND.get(action)
        if kind is None:
            continue
        for tool_name in tool_names_for_kind.get(kind, ()):
            rules.append(
                PermissionRule(
                    tool_name=tool_name,
                    rule_content=pattern,
                    behavior=PermissionBehavior.DENY,
                    source="policy-engine",
                )
            )
    return rules


def install_deny_rules(permission_context: object, rules: Sequence[object]) -> None:
    """替换本引擎上次编译的 deny 规则，保留辖区等其它来源的规则。"""

    deny_rules = getattr(permission_context, "deny_rules", None)
    if deny_rules is None:
        return
    for tool_name, items in list(deny_rules.items()):
        deny_rules[tool_name] = [
            item for item in items if getattr(item, "source", "") != "policy-engine"
        ]
        if not deny_rules[tool_name]:
            deny_rules.pop(tool_name, None)
    for rule in rules:
        tool_name = getattr(rule, "tool_name", "")
        deny_rules.setdefault(tool_name, []).append(rule)
