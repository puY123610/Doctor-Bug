"""Per-employee IDENTITY.md / MEMORY.md archives.

Plain files only. This module never calls a model and never decides whether
a run passed. Callers create archives when a roster is built or a specialist
is synthesized, and append long-term memory only after they have already
decided the run passed.

Files live at ``<profile_dir>/agents/<name>/IDENTITY.md`` and ``MEMORY.md``.
An existing file is never overwritten: the operator may have edited it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


FORBIDDEN_ZONE = (
    "辖区以外的一切文件，以及一切测试文件"
    "（tests/、test_*.py、*_test.py、conftest.py、pytest.ini）"
)
REPORTING_LINE = "向主治医生汇报"
ROLE_SPECIALIST = "模块专家"
ROLE_GENERALIST = "通用修理工"

_ENTRY_RE = re.compile(
    r"^- 测试 `([^`]*)`；文件 `([^`]*)`"
    r"(；根因: .*?)?(；来源哈希: ([0-9a-f]+))?$"
)
_CAUSE_LIMIT = 180


@dataclass(frozen=True)
class AgentArchive:
    """One employee's two markdown files, read verbatim."""

    name: str
    identity_path: Path
    memory_path: Path
    identity: str
    memory: str


def _safe_name(name: str) -> str:
    cleaned = str(name).strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or "\n" in cleaned
        or "\x00" in cleaned
    ):
        raise ValueError(f"invalid employee name: {name!r}")
    return cleaned


def _agent_dir(profile_dir: str | Path, name: str) -> Path:
    return Path(profile_dir) / "agents" / _safe_name(name)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _expertise(files: list[str], explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    stems: list[str] = []
    for path in files:
        stem = PurePosixPath(path).stem
        if stem in {"", "__init__"} or stem in stems:
            continue
        stems.append(stem)
    return "、".join(stems) if stems else "（未标明）"


def _profile_source(profile_dir: Path, explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    label = profile_dir.name or "profile"
    try:
        metadata = json.loads(
            (profile_dir / "profile.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return label
    if not isinstance(metadata, dict):
        return label
    origin = metadata.get("origin")
    if isinstance(origin, str) and origin.strip():
        return f"{label}（origin={origin.strip()}）"
    return label


def _person_from_spec(spec: Mapping[str, object], name: str) -> dict:
    raw_files = spec.get("files") or ()
    if isinstance(raw_files, str):
        files = _dedupe([raw_files])
    else:
        files = _dedupe(str(item) for item in raw_files)
    if "specialist" in spec:
        specialist = bool(spec.get("specialist"))
    else:
        specialist = name != "generalist"
    module = spec.get("module") or spec.get("modules") or ""
    source = spec.get("source") or ""
    return {
        "name": name,
        "files": files,
        "specialist": specialist,
        "module": str(module),
        "source": str(source),
    }


def _people_from_mapping(mapping: Mapping[str, object]) -> list[dict]:
    people: list[dict] = []
    for key, spec in mapping.items():
        if not isinstance(spec, Mapping):
            continue
        name = str(spec.get("id") or key).strip()
        if not name:
            continue
        people.append(_person_from_spec(spec, name))
    return people


def _people_from_list(items: Iterable[object]) -> list[dict]:
    people: list[dict] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("id") or item.get("name") or "").strip()
        if not name:
            continue
        people.append(_person_from_spec(item, name))
    return people


def _people(roster: object) -> list[dict]:
    """Normalise a Roster, a dispatch table, a name map, or a specialist list."""
    specialists = getattr(roster, "specialists", None)
    if isinstance(specialists, dict):
        people: list[dict] = []
        for key, profile in specialists.items():
            files = getattr(profile, "files", None)
            if files is None and isinstance(profile, Mapping):
                people.extend(_people_from_mapping({str(key): profile}))
                continue
            people.append(
                {
                    "name": str(key),
                    "files": _dedupe(str(item) for item in (files or ())),
                    "specialist": bool(getattr(profile, "specialist", True)),
                    "module": str(getattr(profile, "module", "") or ""),
                    "source": str(getattr(profile, "source", "") or ""),
                }
            )
        return people

    if isinstance(roster, Mapping):
        nested = roster.get("roster")
        if isinstance(nested, Mapping):
            nested_specs = nested.get("specialists")
            if isinstance(nested_specs, Mapping):
                return _people_from_mapping(nested_specs)
        listed = roster.get("specialists")
        if isinstance(listed, list):
            return _people_from_list(listed)
        if roster and all(isinstance(value, Mapping) for value in roster.values()):
            return _people_from_mapping(roster)

    if isinstance(roster, (list, tuple)):
        return _people_from_list(roster)

    raise TypeError(
        "roster must be a Roster, a dispatch table, a name mapping, "
        "or a list of specialist entries"
    )


def _render_identity(
    name: str,
    *,
    specialist: bool,
    files: list[str],
    modules: str,
    source: str,
    today: str,
) -> str:
    role = ROLE_SPECIALIST if specialist else ROLE_GENERALIST
    if files:
        file_lines = "\n".join(f"  - {path}" for path in files)
    else:
        file_lines = "  - （无）"
    return (
        f"# IDENTITY — {name}\n"
        "\n"
        f"- 员工名: {name}\n"
        f"- 角色: {role}\n"
        f"- 专长模块: {modules}\n"
        "- 辖区文件:\n"
        f"{file_lines}\n"
        f"- 禁区: {FORBIDDEN_ZONE}\n"
        f"- 汇报线: {REPORTING_LINE}\n"
        f"- 建档日期: {today}\n"
        f"- profile 来源: {source}\n"
    )


def _render_memory(name: str) -> str:
    return f"# MEMORY — {name}\n\n## 经验\n"


def ensure_archives(profile_dir: str | Path, roster: object) -> list[str]:
    """Create missing IDENTITY.md / MEMORY.md for every roster member.

    A file that already exists is left byte-for-byte alone. Returns the
    employee names in roster order (created and pre-existing).
    """
    base = Path(profile_dir)
    today = date.today().isoformat()
    names: list[str] = []
    for person in _people(roster):
        name = _safe_name(person["name"])
        names.append(name)
        directory = base / "agents" / name
        identity_path = directory / "IDENTITY.md"
        memory_path = directory / "MEMORY.md"
        missing_identity = not identity_path.is_file()
        missing_memory = not memory_path.is_file()
        if not missing_identity and not missing_memory:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        if missing_identity:
            identity_path.write_text(
                _render_identity(
                    name,
                    specialist=bool(person["specialist"]),
                    files=list(person["files"]),
                    modules=_expertise(
                        list(person["files"]), str(person["module"])
                    ),
                    source=_profile_source(base, str(person["source"])),
                    today=today,
                ),
                encoding="utf-8",
            )
        if missing_memory:
            memory_path.write_text(_render_memory(name), encoding="utf-8")
    return names


def read_archive(profile_dir: str | Path, name: str) -> AgentArchive | None:
    """Return both files, or None when this employee has no archive."""
    try:
        cleaned = _safe_name(name)
    except ValueError:
        return None
    directory = Path(profile_dir) / "agents" / cleaned
    identity_path = directory / "IDENTITY.md"
    memory_path = directory / "MEMORY.md"
    if not identity_path.is_file() and not memory_path.is_file():
        return None
    identity = (
        identity_path.read_text(encoding="utf-8")
        if identity_path.is_file()
        else ""
    )
    memory = (
        memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
    )
    return AgentArchive(
        name=cleaned,
        identity_path=identity_path,
        memory_path=memory_path,
        identity=identity,
        memory=memory,
    )


def list_archives(profile_dir: str | Path) -> list[dict]:
    """Sorted archive inventory. Does not create directories or files."""
    agents = Path(profile_dir) / "agents"
    if not agents.is_dir():
        return []
    rows: list[dict] = []
    for directory in sorted(agents.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        identity_path = directory / "IDENTITY.md"
        memory_path = directory / "MEMORY.md"
        if not identity_path.is_file() and not memory_path.is_file():
            continue
        rows.append(
            {
                "name": directory.name,
                "identity_path": str(identity_path),
                "memory_path": str(memory_path),
            }
        )
    return rows


def _one_line(value: str) -> str:
    text = " ".join(str(value).replace("`", "'").split())
    if len(text) > _CAUSE_LIMIT:
        return text[:_CAUSE_LIMIT] + "…"
    return text


def _render_entry(entry: Mapping[str, object]) -> str:
    test = _one_line(str(entry.get("test", "")))
    path = _one_line(str(entry.get("file", "")))
    if not test or not path:
        return ""
    line = f"- 测试 `{test}`；文件 `{path}`"
    cause = _one_line(str(entry.get("cause", "") or ""))
    if cause:
        line += f"；根因: {cause}"
    digest = str(entry.get("source_hash", "") or "").strip()
    if digest:
        line += f"；来源哈希: {digest[:16]}"
    return line


def _entry_key(line: str) -> tuple[str, str] | None:
    match = _ENTRY_RE.match(line.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _existing_keys(text: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for line in text.splitlines():
        key = _entry_key(line)
        if key is not None:
            keys.add(key)
    return keys


def _insert_entries(text: str, today: str, lines: list[str]) -> str:
    rows = text.splitlines()
    header = f"### {today}"
    try:
        start = rows.index(header)
    except ValueError:
        start = -1
    if start < 0:
        if "## 经验" not in rows:
            if rows and rows[-1] != "":
                rows.append("")
            rows.append("## 经验")
        if rows and rows[-1] != "":
            rows.append("")
        rows.append(header)
        rows.append("")
        rows.extend(lines)
        return "\n".join(rows) + "\n"

    end = len(rows)
    for index in range(start + 1, len(rows)):
        if rows[index].startswith("### "):
            end = index
            break
    insert_at = end
    while insert_at > start + 1 and rows[insert_at - 1] == "":
        insert_at -= 1
    for offset, line in enumerate(lines):
        rows.insert(insert_at + offset, line)
    return "\n".join(rows) + "\n"


def append_memory_entries(
    profile_dir: str | Path,
    name: str,
    entries: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Append dated memory lines. Skip a test+file pair already on file.

    Returns the entries that were actually written. Does not call a model
    and does not rewrite IDENTITY.md. An empty or fully-duplicate batch
    leaves the file untouched.
    """
    cleaned = _safe_name(name)
    directory = Path(profile_dir) / "agents" / cleaned
    memory_path = directory / "MEMORY.md"
    if memory_path.is_file():
        text = memory_path.read_text(encoding="utf-8")
    else:
        text = _render_memory(cleaned)

    known = _existing_keys(text)
    fresh_lines: list[str] = []
    written: list[dict] = []
    for entry in entries:
        line = _render_entry(entry)
        key = _entry_key(line) if line else None
        if key is None or key in known:
            continue
        known.add(key)
        fresh_lines.append(line)
        payload = {"test": key[0], "file": key[1]}
        cause = _one_line(str(entry.get("cause", "") or ""))
        if cause:
            payload["cause"] = cause
        digest = str(entry.get("source_hash", "") or "").strip()
        if digest:
            payload["source_hash"] = digest
        written.append(payload)
    if not fresh_lines:
        return []

    directory.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(
        _insert_entries(text, date.today().isoformat(), fresh_lines),
        encoding="utf-8",
    )
    return written


_STALE_MARK = "（来源文件已变，本条可能过期）"


def mark_stale_entries(memory_text: str, workspace: str | Path) -> str:
    """给来源已变的记忆条目在**注入副本**里加过期标记。

    带 `；来源哈希: <16 位>` 的条目（BRIEF1b 起写入）逐条比对
    workspace 里该文件的当前 sha256：对不上或文件已删，行尾
    加标记。无哈希的旧条目原样通过。磁盘文件不回写。
    """
    from .brief import sha256_file

    root = Path(workspace)
    out: list[str] = []
    for line in memory_text.splitlines():
        match = _ENTRY_RE.match(line.strip())
        recorded = match.group(5) if match is not None else None
        if match is None or not recorded:
            out.append(line)
            continue
        current = sha256_file(root / match.group(2))
        if current is not None and current.startswith(recorded):
            out.append(line)
        else:
            out.append(line + _STALE_MARK)
    text = "\n".join(out)
    if memory_text.endswith("\n"):
        text += "\n"
    return text


__all__ = [
    "FORBIDDEN_ZONE",
    "REPORTING_LINE",
    "ROLE_GENERALIST",
    "ROLE_SPECIALIST",
    "AgentArchive",
    "append_memory_entries",
    "mark_stale_entries",
    "ensure_archives",
    "list_archives",
    "read_archive",
]
