"""项目名册：只读现有 profile，并在同一份派单表上增删员工。

名册不是第二份存储。``list_roster`` 合并三处已经存在的数据：

* ``<profile>/dispatch_table.json`` 的 ``roster.specialists`` 与 ``specialists``
* ``<profile>/profile.json`` 的 ``specialists`` 名字列表
* ``<profile>/agents/<员工名>/`` 档案（不含 ``agents/_archived/``）

来源五种，前三种不另建文件：

* ``auto`` — 建档时生成。派单表条目没有 ``source`` 字段时就是这种
  （已提交的派单表因此保持原样）。
* ``synthesized`` — 运行期证据合成后写入的档案，人还不在派单表里。
* ``manual`` — ``add_worker`` 未注明渠道时的默认来源。
* ``secretary`` — 秘书经对话工具添加（名单显示「秘书提议」）。
* ``boss`` — 老板经 CLI / 界面命令添加（名单显示「老板添加」）。

老板的手动增删另存一份覆盖层 ``<profile>/roster_overlay.json``
（added / removed 两向记录）。它不是 build_profile 的任何产物、
build_profile 从不写它；重建档时 ``apply_overlay`` 把覆盖层应用
到新派单表上——删掉的人不复活、加的人保留。``list_roster``
同样按覆盖层滤掉已删的人。

派活规则不动。加人只按 GEN3 合成专家的同一规则补一条辖区（一个源文件）
和一条 ``module_defaults`` 证据键；删人只把该员工从这些表里拿掉，让既有
查找顺序（节点覆盖 → 模块默认 → fallback）自己回落。
"""

from __future__ import annotations

import ast
import json
import shutil
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from .agent_archive import (
    ROLE_GENERALIST,
    ROLE_SPECIALIST,
    ensure_archives,
)
from .autoprofile import default_profile_name, state_root_for

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

SOURCES = ("auto", "synthesized", "manual", "secretary", "boss")
SOURCE_LABELS = {
    "auto": "自动建档",
    "synthesized": "合成",
    "manual": "手动添加",
    "secretary": "秘书提议",
    "boss": "老板添加",
}
CHANNELS = ("manual", "secretary", "boss")
OVERLAY_FILENAME = "roster_overlay.json"
ZERO_EXPERT_WARNING = (
    "警告: 名册已无模块专家，此后失败证据按既有规则全部回落通用修理工。"
)
_SECRETARY = "drbug"
_EXCLUDED_DIRS = {
    ".drbug",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}


class RosterError(ValueError):
    """名册增删被拒绝。``args[0]`` 是可以直接展示的说明。"""


def _resolve_workspace(workspace: str | Path) -> Path:
    """与 dispatcher 相同：绝对路径原样，相对路径先看平台目录。"""
    path = Path(workspace).expanduser()
    if path.is_absolute():
        return path.resolve()
    platform_candidate = (PLATFORM_ROOT / path).resolve()
    if platform_candidate.exists():
        return platform_candidate
    return (Path.cwd() / path).resolve()


def _profile_dir(
    workspace: str | Path,
    profiles_root: str | Path | None = None,
) -> tuple[Path, Path]:
    workspace_path = _resolve_workspace(workspace)
    if not workspace_path.is_dir():
        raise FileNotFoundError(f"工作区不存在: {workspace_path}")
    if profiles_root is None:
        root = state_root_for(workspace_path) / "profiles"
    else:
        root = Path(profiles_root).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
    return workspace_path, root / default_profile_name(workspace_path)


def _read_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} 不是合法 JSON: {path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 格式不正确: {path} 不是 JSON 对象")
    return payload


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_table(profile_dir: Path) -> dict | None:
    path = profile_dir / "dispatch_table.json"
    if not path.is_file():
        return None
    return _read_json(path, "派单表")


def _load_metadata(profile_dir: Path) -> dict:
    path = profile_dir / "profile.json"
    if not path.is_file():
        return {}
    return _read_json(path, "profile.json")


def _clean_name(name: str) -> str:
    cleaned = str(name).strip()
    if (
        not cleaned
        or cleaned in {".", "..", "_archived"}
        or "/" in cleaned
        or "\\" in cleaned
        or "\n" in cleaned
        or "\x00" in cleaned
    ):
        raise RosterError(f"员工名不合法: {name!r}")
    return cleaned


# 秘书的内部 id 与对外称呼都算秘书本人：增删名册时一律拦下。
_SECRETARY_ALIASES = {_SECRETARY, "secretary", "主治医生", "调度员"}


def _is_secretary(name: str) -> bool:
    return name.casefold() in _SECRETARY_ALIASES


def _source_of(spec: dict) -> tuple[str, bool]:
    raw = str(spec.get("source") or "").strip()
    if raw in SOURCES:
        return raw, True
    return "auto", False


def _files_of(spec: dict) -> list[str]:
    raw = spec.get("files") or []
    if isinstance(raw, str):
        raw = [raw]
    files: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in files:
            files.append(text)
    return files


def _module_of(spec: dict, files: list[str]) -> str:
    explicit = spec.get("module")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    for path in files:
        stem = PurePosixPath(path).stem
        if stem and stem != "__init__":
            return stem
    return ""


def _role_of(name: str, spec: dict) -> str:
    if name == "generalist":
        return ROLE_GENERALIST
    if "specialist" in spec and not bool(spec.get("specialist")):
        return ROLE_GENERALIST
    return ROLE_SPECIALIST


def _has_archive(profile_dir: Path, name: str) -> bool:
    directory = profile_dir / "agents" / name
    return (directory / "IDENTITY.md").is_file() or (
        directory / "MEMORY.md"
    ).is_file()


def _blank_person(name: str) -> dict:
    return {
        "name": name,
        "role": ROLE_GENERALIST if name == "generalist" else ROLE_SPECIALIST,
        "module": "",
        "scope_files": [],
        "source": "auto",
        "explicit": False,
    }


def _absorb(people: dict[str, dict], order: list[str], name: str, spec: dict) -> None:
    if not name or _is_secretary(name) or name == "_archived":
        return
    source, explicit = _source_of(spec)
    files = _files_of(spec)
    if name not in people:
        people[name] = {
            "name": name,
            "role": _role_of(name, spec),
            "module": _module_of(spec, files),
            "scope_files": files,
            "source": source,
            "explicit": explicit,
        }
        order.append(name)
        return
    current = people[name]
    if explicit and not current["explicit"]:
        current["source"] = source
        current["explicit"] = True
    if not current["scope_files"] and files:
        current["scope_files"] = files
        if not current["module"]:
            current["module"] = _module_of(spec, files)
    if "specialist" in spec or name == "generalist":
        current["role"] = _role_of(name, spec)


def _parse_identity(text: str) -> dict:
    role = ROLE_SPECIALIST
    module = ""
    source = "synthesized"
    files: list[str] = []
    in_files = False
    for raw in text.splitlines():
        if in_files and raw.startswith("  - "):
            item = raw.strip()[2:].strip()
            if item and item != "（无）" and item not in files:
                files.append(item)
            continue
        stripped = raw.strip()
        if stripped.startswith("- 角色:"):
            in_files = False
            value = stripped.split(":", 1)[1].strip()
            if value in {ROLE_SPECIALIST, ROLE_GENERALIST}:
                role = value
        elif stripped.startswith("- 专长模块:"):
            in_files = False
            value = stripped.split(":", 1)[1].strip()
            module = "" if value == "（未标明）" else value
        elif stripped.startswith("- 辖区文件:"):
            in_files = True
        elif stripped.startswith("- profile 来源:"):
            in_files = False
            value = stripped.split(":", 1)[1].strip()
            if value in SOURCES:
                source = value
        elif stripped.startswith("- "):
            in_files = False
    return {
        "role": role,
        "module": module,
        "source": source,
        "files": files,
    }


def _rows_from_profile(table: dict, metadata: dict, profile_dir: Path) -> list[dict]:
    people: dict[str, dict] = {}
    order: list[str] = []

    roster = table.get("roster")
    if isinstance(roster, dict):
        specs = roster.get("specialists")
        if isinstance(specs, dict):
            for key, spec in specs.items():
                if isinstance(spec, dict):
                    _absorb(people, order, str(key), spec)

    listed = table.get("specialists")
    if isinstance(listed, list):
        for item in listed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id") or "").strip()
            _absorb(people, order, name, item)

    agents = profile_dir / "agents"
    if agents.is_dir():
        for directory in sorted(agents.iterdir(), key=lambda item: item.name):
            if not directory.is_dir() or directory.name in {".", "..", "_archived"}:
                continue
            if directory.name in people or _is_secretary(directory.name):
                continue
            identity_path = directory / "IDENTITY.md"
            memory_path = directory / "MEMORY.md"
            if not identity_path.is_file() and not memory_path.is_file():
                continue
            parsed = {"role": ROLE_SPECIALIST, "module": "", "source": "synthesized", "files": []}
            if identity_path.is_file():
                parsed = _parse_identity(
                    identity_path.read_text(encoding="utf-8")
                )
            people[directory.name] = {
                "name": directory.name,
                "role": parsed["role"],
                "module": parsed["module"],
                "scope_files": list(parsed["files"]),
                "source": parsed["source"],
                "explicit": True,
            }
            order.append(directory.name)

    names = metadata.get("specialists")
    if isinstance(names, list):
        for item in names:
            name = str(item).strip()
            if not name or name in people or _is_secretary(name):
                continue
            people[name] = _blank_person(name)
            order.append(name)

    rows: list[dict] = []
    for name in order:
        person = people[name]
        rows.append(
            {
                "name": name,
                "role": person["role"],
                "module": person["module"],
                "scope_files": list(person["scope_files"]),
                "source": person["source"],
                "source_label": SOURCE_LABELS.get(
                    person["source"], person["source"]
                ),
                "has_archive": _has_archive(profile_dir, name),
            }
        )
    return rows


def _known_names(table: dict, metadata: dict, profile_dir: Path) -> set[str]:
    return {
        row["name"] for row in _rows_from_profile(table, metadata, profile_dir)
    }


def list_roster(
    workspace: str | Path,
    *,
    profiles_root: str | Path | None = None,
) -> list[dict]:
    """读取当前 profile 的名册。没有派单表时返回空列表，不创建任何文件。"""
    _workspace, profile_dir = _profile_dir(workspace, profiles_root)
    table = _load_table(profile_dir) or {}
    metadata = _load_metadata(profile_dir) if profile_dir.is_dir() else {}
    rows = _rows_from_profile(table, metadata, profile_dir)
    removed = _overlay_ids(load_overlay(profile_dir), "removed")
    if removed:
        rows = [row for row in rows if row["name"] not in removed]
    return rows


def expert_count(rows: list[dict]) -> int:
    return sum(1 for row in rows if row.get("role") == ROLE_SPECIALIST)


def format_team_line(rows: list[dict]) -> str:
    """开工汇报用的一行：专家数、名单、来源标记。"""
    experts = [row for row in rows if row.get("role") == ROLE_SPECIALIST]
    generalists = [row for row in rows if row.get("role") == ROLE_GENERALIST]
    expert_bits = "、".join(
        f"{row['name']}[{row['source']}]" for row in experts
    )
    general_bits = "、".join(
        f"{row['name']}[{row['source']}]" for row in generalists
    )
    text = f"本次队伍构成: 专家 {len(experts)} 人"
    if expert_bits:
        text += f"（{expert_bits}）"
    if general_bits:
        text += f"；通用修理工 {general_bits}"
    text += "。"
    if not experts:
        text += ZERO_EXPERT_WARNING
    return text


def format_roster_listing(rows: list[dict]) -> str:
    if not rows:
        return "名册为空（还没建档，或派活规则表里还没有人）。"
    lines = ["== 名册 =="]
    for row in rows:
        files = "、".join(row["scope_files"]) or "没有负责的文件"
        archive = "有档案" if row["has_archive"] else "无档案"
        module = row["module"] or "—"
        source = row.get("source_label") or row["source"]
        lines.append(
            f"- {row['name']} | {row['role']} | {source} | "
            f"模块 {module} | 负责的文件 {files} | {archive}"
        )
    lines.append(f"专家 {expert_count(rows)} 人，名册共 {len(rows)} 名。")
    lines.append(format_team_line(rows))
    return "\n".join(lines)


def _module_docstring(path: Path) -> str:
    """与 autoprofile 相同：取源文件 docstring 的第一行非空文本。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ""
    docstring = ast.get_docstring(tree) or ""
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _neutral_blurb(module: str) -> str:
    return f"负责 {module} 模块的修复。"


def _is_test_relative(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    name = parts[-1]
    if name in {"conftest.py", "pytest.ini"}:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return "tests" in parts


def _find_by_stem(workspace: Path, stem: str) -> list[str]:
    matches: list[str] = []
    for path in sorted(workspace.rglob("*.py")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        parts = PurePosixPath(relative).parts
        if any(part in _EXCLUDED_DIRS or part.startswith(".") for part in parts):
            continue
        if _is_test_relative(relative):
            continue
        if path.stem == stem or path.name == stem:
            matches.append(relative)
    return matches


def _resolve_module(workspace: Path, module: str, package: str) -> tuple[str, str]:
    raw = str(module).strip().replace("\\", "/")
    if not raw or raw in {".", ".."}:
        raise RosterError(f"模块不存在: {module}。工作区内没有这个源文件。")

    workspace = workspace.resolve()
    chosen: Path | None = None
    if Path(raw).is_absolute():
        candidate = Path(raw).expanduser().resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise RosterError(
                f"模块不存在: {module}。该路径不在工作区内。"
            ) from exc
        if candidate.is_file():
            chosen = candidate
        elif candidate.is_dir():
            raise RosterError(
                f"模块不存在: {module}。请指定具体的源文件，不能只给目录。"
            )
        else:
            raise RosterError(
                f"模块不存在: {module}。工作区内没有这个源文件。"
            )
    else:
        rel = raw[2:] if raw.startswith("./") else raw
        if ".." in PurePosixPath(rel).parts:
            raise RosterError(f"模块不存在: {module}。路径不能包含 ..。")
        direct = (workspace / rel).resolve()
        try:
            direct.relative_to(workspace)
        except ValueError as exc:
            raise RosterError(
                f"模块不存在: {module}。该路径不在工作区内。"
            ) from exc
        if direct.is_file():
            chosen = direct
        elif direct.is_dir():
            raise RosterError(
                f"模块不存在: {module}。请指定具体的源文件，不能只给目录。"
            )
        else:
            dotted_rel = rel.replace(".", "/") + ".py" if not rel.endswith(".py") else rel
            dotted = (workspace / dotted_rel).resolve()
            try:
                inside = dotted.relative_to(workspace)
            except ValueError:
                inside = None
            if inside is not None and dotted.is_file():
                chosen = dotted
            else:
                matches = _find_by_stem(workspace, rel)
                if len(matches) > 1:
                    raise RosterError(
                        f"模块 {module} 命中多个源文件: {'、'.join(matches)}。"
                        "请改用相对路径。"
                    )
                if len(matches) == 1:
                    chosen = workspace / matches[0]
    if chosen is None:
        raise RosterError(f"模块不存在: {module}。工作区内没有这个源文件。")

    relative = chosen.resolve().relative_to(workspace).as_posix()
    if not relative.endswith(".py"):
        raise RosterError(f"拒绝添加: {relative} 不是 Python 源文件。")
    if _is_test_relative(relative):
        raise RosterError(
            f"拒绝添加: {relative} 是测试文件，不能作为员工辖区。"
        )
    if PurePosixPath(relative).stem == "__init__":
        raise RosterError(
            f"拒绝添加: {relative} 是包初始化文件。"
            "自动建档不为 __init__.py 单设专家，手动添加同样拒绝。"
        )
    prefix = package.rstrip("/") + "/"
    if not relative.startswith(prefix):
        raise RosterError(
            f"拒绝添加: {relative} 不在当前名册包 {package} 内。"
            "roster 要求辖区文件位于该包下。"
        )
    return relative, PurePosixPath(relative).stem


def _require_roster(table: dict) -> dict:
    roster = table.get("roster")
    if not isinstance(roster, dict):
        raise RosterError(
            "当前 profile 的派单表没有 roster 段，无法登记辖区。请先建档。"
        )
    specs = roster.get("specialists")
    if not isinstance(specs, dict):
        raise RosterError("roster.specialists 不是对象，无法登记员工。")
    package = str(roster.get("package") or "").strip()
    if not package:
        raise RosterError("roster.package 为空，无法登记辖区。")
    return roster


def _touch_generalist_files(table: dict, relative: str) -> None:
    """把新源文件并进通用修理工的辖区，删人后兜底仍能碰到这个文件。"""
    roster = table.get("roster")
    if isinstance(roster, dict):
        specs = roster.get("specialists")
        if isinstance(specs, dict):
            general = specs.get("generalist")
            if isinstance(general, dict):
                files = _files_of(general)
                if relative not in files:
                    general["files"] = sorted([*files, relative])
    listed = table.get("specialists")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and item.get("id") == "generalist":
                files = _files_of(item)
                if relative not in files:
                    item["files"] = sorted([*files, relative])


def _append_specialist_record(table: dict, record: dict) -> None:
    listed = table.get("specialists")
    if not isinstance(listed, list):
        listed = []
        table["specialists"] = listed
    listed.append(record)
    general = [
        item
        for item in listed
        if isinstance(item, dict) and item.get("id") == "generalist"
    ]
    others = [
        item
        for item in listed
        if not (isinstance(item, dict) and item.get("id") == "generalist")
    ]
    table["specialists"] = others + general


def _overlay_path(profile_dir: Path) -> Path:
    return profile_dir / OVERLAY_FILENAME


def load_overlay(profile_dir: Path) -> dict:
    """读覆盖层。文件不存在时返回空结构；内容损坏按本文件惯例报错。"""
    path = _overlay_path(profile_dir)
    if not path.is_file():
        return {"added": [], "removed": []}
    payload = _read_json(path, "名册覆盖层")
    added = payload.get("added")
    removed = payload.get("removed")
    return {
        "added": list(added) if isinstance(added, list) else [],
        "removed": list(removed) if isinstance(removed, list) else [],
    }


def save_overlay(profile_dir: Path, overlay: dict) -> None:
    _write_json(
        _overlay_path(profile_dir),
        {
            "added": list(overlay.get("added") or []),
            "removed": list(overlay.get("removed") or []),
        },
    )


def _overlay_ids(overlay: dict, key: str) -> set[str]:
    ids: set[str] = set()
    for entry in overlay.get(key) or []:
        if isinstance(entry, dict):
            name = str(entry.get("id") or "").strip()
            if name:
                ids.add(name)
    return ids


def _overlay_without(overlay: dict, key: str, name: str) -> list:
    return [
        entry
        for entry in overlay.get(key) or []
        if not (isinstance(entry, dict) and str(entry.get("id") or "") == name)
    ]


def _now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _in_table(table: dict, metadata: dict, name: str) -> bool:
    roster = table.get("roster")
    if isinstance(roster, dict) and isinstance(roster.get("specialists"), dict):
        if name in roster["specialists"]:
            return True
    listed = table.get("specialists")
    if isinstance(listed, list) and any(
        isinstance(item, dict) and str(item.get("id") or "") == name
        for item in listed
    ):
        return True
    names = metadata.get("specialists")
    return isinstance(names, list) and name in names


def _insert_worker(
    table: dict,
    name: str,
    relative: str,
    stem: str,
    blurb: str,
    source: str,
) -> None:
    """把一名员工写进派单表（roster 段 + 名单 + module_defaults）。

    ``add_worker`` 与 ``apply_overlay`` 共用：两处写出的条目形状
    必须一致，重建档保留下来的人和刚加的人在表里没有区别。
    """
    roster = _require_roster(table)
    roster["specialists"][name] = {
        "files": [relative],
        "blurb": blurb,
        "specialist": True,
        "module": stem,
        "source": source,
    }
    general = roster["specialists"].pop("generalist", None)
    if general is not None:
        roster["specialists"]["generalist"] = general

    defaults = table.get("module_defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        table["module_defaults"] = defaults
    current = defaults.get(stem)
    if current is None:
        defaults[stem] = [name]
    elif isinstance(current, list):
        if name not in current:
            current.append(name)
    else:
        raise RosterError(f"module_defaults.{stem} 不是列表，拒绝写入。")

    _append_specialist_record(
        table,
        {
            "id": name,
            "name": name,
            "scope": f"{relative}：{blurb}",
            "files": [relative],
            "blurb": blurb,
            "module": stem,
            "source": source,
        },
    )
    _touch_generalist_files(table, relative)


def apply_overlay(profile_dir: Path, table: dict) -> list[dict]:
    """把覆盖层应用到一份刚生成的派单表上，返回说明条目列表。

    供 build_profile 在写盘前调用：removed 的人从表里摘掉，
    added 的人按记录插回。覆盖层文件不存在或两向皆空时原样
    返回空列表、不动 table 一个字段——没有覆盖层的档案重建
    产物因此与从前逐字节一致。同一名字同时出现在两向时以
    removed 为准（删除是安全方向的决定），并在说明里点明。
    """
    overlay = load_overlay(profile_dir)
    added = [
        entry
        for entry in overlay["added"]
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    ]
    removed = [
        entry
        for entry in overlay["removed"]
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    ]
    if not added and not removed:
        return []
    notes: list[dict] = []
    removed_ids = _overlay_ids(overlay, "removed")
    for entry in removed:
        name = str(entry["id"]).strip()
        _strip_worker(table, name)
        notes.append(
            {
                "kind": "roster_overlay",
                "detail": (
                    f"按老板的删除记录，重建档时未恢复员工 {name}"
                    f"（模块 {str(entry.get('module') or '—')}）。"
                ),
            }
        )
    for entry in added:
        name = str(entry["id"]).strip()
        if name in removed_ids:
            notes.append(
                {
                    "kind": "roster_overlay",
                    "detail": (
                        f"员工 {name} 同时有添加与删除记录，"
                        "以删除为准，未恢复。"
                    ),
                }
            )
            continue
        relative = str(entry.get("path") or "").strip()
        stem = str(entry.get("module") or "").strip()
        if not relative or not stem:
            notes.append(
                {
                    "kind": "roster_overlay",
                    "detail": f"员工 {name} 的添加记录缺 path/module，已跳过。",
                }
            )
            continue
        blurb = str(entry.get("blurb") or "").strip() or _neutral_blurb(stem)
        source = str(entry.get("source") or "manual")
        if source not in SOURCES:
            source = "manual"
        _insert_worker(table, name, relative, stem, blurb, source)
        notes.append(
            {
                "kind": "roster_overlay",
                "detail": (
                    f"按老板的添加记录，重建档时保留员工 {name}"
                    f"（模块 {stem}，来源 {source}）。"
                ),
            }
        )
    return notes


def add_worker(
    workspace: str | Path,
    name: str,
    module: str,
    *,
    profiles_root: str | Path | None = None,
    channel: str = "manual",
) -> dict:
    """按 GEN3 合成规则把一名员工写回 profile 派单表并建档。

    模块必须是工作区内真实存在的源文件。同名已在名册（含只存在于档案的
    合成员工）时拒绝。不修改查找顺序本身。``channel`` 记来源渠道：
    ``secretary`` 是秘书经对话工具添加、``boss`` 是老板经命令添加，
    默认 ``manual``。添加成功后同步维护覆盖层（见模块 docstring）。
    """
    if channel not in CHANNELS:
        raise RosterError(
            f"未知添加渠道: {channel}。只许 manual / secretary / boss。"
        )
    cleaned = _clean_name(name)
    if _is_secretary(cleaned):
        raise RosterError("主治医生是秘书，不进名册，不能添加。")
    if cleaned == "generalist":
        raise RosterError(
            "generalist 是兜底修理工的保留名，不能当作新员工添加。"
        )

    workspace_path, profile_dir = _profile_dir(workspace, profiles_root)
    table_path = profile_dir / "dispatch_table.json"
    if not table_path.is_file():
        raise FileNotFoundError(
            f"该工作区还没有 profile 派单表: {table_path}。请先建档后再加人。"
        )
    table = _load_table(profile_dir)
    assert table is not None
    metadata = _load_metadata(profile_dir)
    if cleaned in _known_names(table, metadata, profile_dir):
        # 例外：覆盖层里有删除记录、且人已不在表里（只剩档案目录
        # 让名字「已知」）时，这是老板把删掉的人加回来，放行，
        # 并在成功后销掉删除记录。
        overlay = load_overlay(profile_dir)
        resurrecting = cleaned in _overlay_ids(
            overlay, "removed"
        ) and not _in_table(table, metadata, cleaned)
        if not resurrecting:
            raise RosterError(f"名册里已有员工 {cleaned}，拒绝重复添加。")

    roster = _require_roster(table)
    if "generalist" not in roster["specialists"]:
        raise RosterError(
            "名册缺少通用修理工 generalist，拒绝添加。证据表兜底不能缺。"
        )
    relative, stem = _resolve_module(
        workspace_path, module, str(roster["package"])
    )
    blurb = _module_docstring(workspace_path / relative) or _neutral_blurb(stem)

    inventory = metadata.get("inventory")
    if isinstance(inventory, dict):
        existing = inventory.get(stem)
        if (
            isinstance(existing, dict)
            and str(existing.get("path") or "")
            and str(existing.get("path")) != relative
        ):
            raise RosterError(
                f"证据模块 {stem} 已映射到 {existing.get('path')}，"
                f"与 {relative} 不是同一个文件，拒绝添加。"
            )

    # VER1：定义变更统一经版本入口落账（旧定义快照 → 改 → 逐人比对记版）。
    from .agent_versions import begin as _begin_versions

    versioning = _begin_versions(profile_dir)
    _insert_worker(table, cleaned, relative, stem, blurb, channel)

    if metadata:
        names = metadata.get("specialists")
        if not isinstance(names, list):
            names = []
        if cleaned not in names:
            names.append(cleaned)
        metadata["specialists"] = names
        inventory = metadata.get("inventory")
        if not isinstance(inventory, dict):
            inventory = {}
        if stem not in inventory:
            inventory[stem] = {"path": relative, "blurb": blurb}
        metadata["inventory"] = inventory

    _write_json(table_path, table)
    if metadata:
        _write_json(profile_dir / "profile.json", metadata)
    ensure_archives(
        profile_dir,
        {
            cleaned: {
                "files": [relative],
                "specialist": True,
                "module": stem,
                "source": channel,
            }
        },
    )
    versioning.commit(f"加人 {cleaned}（模块 {stem}，渠道 {channel}）带来的定义变化")
    overlay = load_overlay(profile_dir)
    overlay["removed"] = _overlay_without(overlay, "removed", cleaned)
    overlay["added"] = _overlay_without(overlay, "added", cleaned)
    overlay["added"].append(
        {
            "id": cleaned,
            "module": stem,
            "path": relative,
            "blurb": blurb,
            "source": channel,
            "time": _now_stamp(),
        }
    )
    save_overlay(profile_dir, overlay)
    return {
        "ok": True,
        "name": cleaned,
        "role": ROLE_SPECIALIST,
        "module": stem,
        "scope_files": [relative],
        "source": channel,
        "evidence_module": stem,
        "archive": f"agents/{cleaned}",
        "has_archive": True,
    }


def format_add_result(result: dict) -> str:
    files = "、".join(result.get("scope_files") or []) or "（无）"
    return (
        f"已添加员工 {result['name']}（source={result.get('source') or 'manual'}），派单表已落盘。\n"
        f"模块: {result.get('module') or '—'}\n"
        f"辖区: {files}\n"
        f"证据键: module_defaults.{result.get('evidence_module')} "
        f"已按合成专家的单文件规则写入。\n"
        f"档案: {result.get('archive')}"
    )


def _strip_worker(table: dict, name: str) -> dict:
    released_modules: list[str] = []
    released_nodes: list[str] = []
    defaults = table.get("module_defaults")
    if isinstance(defaults, dict):
        for key in list(defaults):
            values = defaults.get(key)
            if not isinstance(values, list) or name not in values:
                continue
            defaults[key] = [item for item in values if item != name]
            if not defaults[key]:
                released_modules.append(str(key))
                del defaults[key]
    overrides = table.get("node_overrides")
    if isinstance(overrides, dict):
        for node in list(overrides):
            entry = overrides.get(node)
            if not isinstance(entry, dict):
                continue
            specs = entry.get("specialists")
            if not isinstance(specs, list) or name not in specs:
                continue
            entry["specialists"] = [item for item in specs if item != name]
            if not entry["specialists"]:
                released_nodes.append(str(node))
                del overrides[node]
    roster = table.get("roster")
    if isinstance(roster, dict) and isinstance(roster.get("specialists"), dict):
        roster["specialists"].pop(name, None)
    listed = table.get("specialists")
    if isinstance(listed, list):
        table["specialists"] = [
            item
            for item in listed
            if not (isinstance(item, dict) and str(item.get("id") or "") == name)
        ]
    return {
        "released_module_defaults": released_modules,
        "released_node_overrides": released_nodes,
    }


def _archive_directory(profile_dir: Path, name: str) -> str:
    source = profile_dir / "agents" / name
    if not source.is_dir():
        return ""
    dest_root = profile_dir / "agents" / "_archived"
    dest_root.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    dest = dest_root / f"{name}-{stamp}"
    suffix = 2
    while dest.exists():
        dest = dest_root / f"{name}-{stamp}-{suffix}"
        suffix += 1
    shutil.move(str(source), str(dest))
    return f"agents/_archived/{dest.name}"


def remove_worker(
    workspace: str | Path,
    name: str,
    *,
    profiles_root: str | Path | None = None,
) -> dict:
    """从 profile 名册和派单表移除员工，档案归档到 ``agents/_archived/``。

    不物理销毁档案。证据键按既有查找顺序回落，不新造运行记录字段。
    名册里没有这个人时抛 :class:`RosterError`。
    """
    cleaned = _clean_name(name)
    if _is_secretary(cleaned):
        raise RosterError("主治医生是秘书，不在名册里，不能删除。")
    if cleaned == "generalist":
        raise RosterError("通用修理工 generalist 是证据表兜底，不能删除。")

    _workspace, profile_dir = _profile_dir(workspace, profiles_root)
    table_path = profile_dir / "dispatch_table.json"
    if not table_path.is_file():
        raise FileNotFoundError(
            f"该工作区还没有 profile 派单表: {table_path}。无法删除员工。"
        )
    table = _load_table(profile_dir)
    assert table is not None
    metadata = _load_metadata(profile_dir)
    if cleaned not in _known_names(table, metadata, profile_dir):
        raise RosterError(
            f"名册里没有这个员工: {cleaned}。拒绝删除（不会静默成功）。"
        )
    module_of_removed = ""
    for row in _rows_from_profile(table, metadata, profile_dir):
        if row["name"] == cleaned:
            module_of_removed = row["module"]
            break

    from .agent_versions import begin as _begin_versions

    versioning = _begin_versions(profile_dir)
    released = _strip_worker(table, cleaned)
    names = metadata.get("specialists")
    if isinstance(names, list):
        metadata["specialists"] = [item for item in names if str(item) != cleaned]
    _write_json(table_path, table)
    if (profile_dir / "profile.json").is_file():
        _write_json(profile_dir / "profile.json", metadata)
    # 先落账再归档：「移除」这一行写进员工自己的 VERSION.json，随档案一起归档。
    versioning.commit(f"移除 {cleaned}")
    archived_to = _archive_directory(profile_dir, cleaned)
    overlay = load_overlay(profile_dir)
    overlay["added"] = _overlay_without(overlay, "added", cleaned)
    overlay["removed"] = _overlay_without(overlay, "removed", cleaned)
    overlay["removed"].append(
        {"id": cleaned, "module": module_of_removed, "time": _now_stamp()}
    )
    save_overlay(profile_dir, overlay)
    rows = _rows_from_profile(table, metadata, profile_dir)
    experts = expert_count(rows)
    warning = ZERO_EXPERT_WARNING if experts == 0 else ""
    return {
        "ok": True,
        "name": cleaned,
        "archived_to": archived_to,
        "released_module_defaults": released["released_module_defaults"],
        "released_node_overrides": released["released_node_overrides"],
        "expert_count": experts,
        "warning": warning,
    }


def format_remove_result(result: dict) -> str:
    lines = [f"已从名册移除 {result['name']}，派单表已落盘。"]
    archived = result.get("archived_to") or ""
    if archived:
        lines.append(f"档案已归档（未销毁）: {archived}")
    else:
        lines.append("该员工原本没有档案，无需归档。")
    modules = result.get("released_module_defaults") or []
    nodes = result.get("released_node_overrides") or []
    if modules:
        lines.append("卸下的模块证据键: " + "、".join(modules))
    if nodes:
        lines.append("卸下的节点覆盖: " + "、".join(nodes))
    lines.append(
        "这些证据键之后按既有查找顺序回落"
        "（节点覆盖 → 模块默认 → fallback 通用修理工）。"
        "后续运行看 RunRecord.rounds[].assignments，"
        "dispatcher 汇报里的「派单」行会显示接到这些失败的人"
        "（没有更具体规则时是 generalist）。没有新增记录字段。"
    )
    if result.get("warning"):
        lines.append(str(result["warning"]))
    return "\n".join(lines)


__all__ = [
    "CHANNELS",
    "OVERLAY_FILENAME",
    "SOURCES",
    "SOURCE_LABELS",
    "ZERO_EXPERT_WARNING",
    "RosterError",
    "add_worker",
    "apply_overlay",
    "load_overlay",
    "save_overlay",
    "expert_count",
    "format_add_result",
    "format_remove_result",
    "format_roster_listing",
    "format_team_line",
    "list_roster",
    "remove_worker",
]
