"""VER1：员工整套定义的版本号、变更记录、快照与回滚。

一个员工的「定义」由三处载体合成，缺一不可：

1. 档案：``agents/<名>/IDENTITY.md`` 的全文；
2. 名册：派单表 ``roster.specialists[<名>]``、``specialists`` 列表里
   ``id == <名>`` 的条目，以及 ``profile.json`` 的 ``specialists`` 成员资格；
3. 派活对照表：``module_defaults`` / ``node_overrides`` 里列着这个人的键
   （``kb.lookup`` 实际读的就是这两张表）。

员工记忆 ``MEMORY.md`` 不是定义：本模块从不读写它。

落账位置（不碰 IDENTITY.md 一个字节）：

* ``agents/<名>/VERSION.json``：当前版本号、当前定义的标准内容哈希、变更行；
* ``agents/<名>/versions/v<N>.json``：第 N 版的完整快照。

规矩：

* 没有 VERSION.json 的员工（存量、或从未变更过）一律视为 v1；第一次经入口
  发生变更时，先把变更前的定义存成 v1，再记 v2。
* 只在标准内容真有变化时写盘；内容没变一个字节都不写。
* 外部修改（老板手改文件、绕过入口）在下一次入口调用或运行开始时比对哈希
  发现，补记一版「外部修改」。
* 删除员工时记一行「移除」，历史随档案目录一起归档；回滚拒绝复活。

本模块不改查表逻辑、不改派活规则，只让表的内容可版本化。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

VERSION_FILENAME = "VERSION.json"
SNAPSHOT_DIRNAME = "versions"


class VersionError(ValueError):
    """版本操作被拒绝（无记录、版本号非法、员工已删除等）。"""


# ---------------------------------------------------------------- 读取


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _table(profile_dir: Path) -> dict:
    return _read_json(profile_dir / "dispatch_table.json") or {}


def _metadata(profile_dir: Path) -> dict:
    return _read_json(profile_dir / "profile.json") or {}


def _agent_dir(profile_dir: Path, name: str) -> Path:
    return Path(profile_dir) / "agents" / name


def employee_names(profile_dir: str | Path, table: dict | None = None,
                   metadata: dict | None = None) -> list[str]:
    """三处载体里出现过的全部员工名（不含 _archived 下已删除的）。"""
    base = Path(profile_dir)
    table = _table(base) if table is None else table
    metadata = _metadata(base) if metadata is None else metadata
    names: set[str] = set()
    roster = table.get("roster")
    if isinstance(roster, dict) and isinstance(roster.get("specialists"), dict):
        names.update(str(key) for key in roster["specialists"])
    listed = table.get("specialists")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and item.get("id"):
                names.add(str(item["id"]))
    members = metadata.get("specialists")
    if isinstance(members, list):
        names.update(str(item) for item in members)
    defaults = table.get("module_defaults")
    if isinstance(defaults, dict):
        for values in defaults.values():
            if isinstance(values, list):
                names.update(str(item) for item in values)
    overrides = table.get("node_overrides")
    if isinstance(overrides, dict):
        for entry in overrides.values():
            if isinstance(entry, dict) and isinstance(entry.get("specialists"), list):
                names.update(str(item) for item in entry["specialists"])
    agents = base / "agents"
    if agents.is_dir():
        for child in agents.iterdir():
            if child.is_dir() and not child.name.startswith("_"):
                if any((child / f).is_file() for f in
                       ("IDENTITY.md", "MEMORY.md", VERSION_FILENAME)):
                    names.add(child.name)
    return sorted(names)


def definition_of(profile_dir: str | Path, name: str, table: dict | None = None,
                  metadata: dict | None = None) -> tuple[dict, dict]:
    """返回 (标准内容, 位置提示)。标准内容决定哈希；位置提示只用于回滚还原顺序。"""
    base = Path(profile_dir)
    table = _table(base) if table is None else table
    metadata = _metadata(base) if metadata is None else metadata
    hints: dict = {"record_index": None, "module_defaults": {}, "node_overrides": {}}

    identity_path = _agent_dir(base, name) / "IDENTITY.md"
    identity = identity_path.read_text(encoding="utf-8") if identity_path.is_file() else None

    roster_entry = None
    roster = table.get("roster")
    if isinstance(roster, dict) and isinstance(roster.get("specialists"), dict):
        found = roster["specialists"].get(name)
        roster_entry = copy.deepcopy(found) if found is not None else None

    record = None
    listed = table.get("specialists")
    if isinstance(listed, list):
        for index, item in enumerate(listed):
            if isinstance(item, dict) and str(item.get("id") or "") == name:
                record = copy.deepcopy(item)
                hints["record_index"] = index
                break

    member = None
    members = metadata.get("specialists")
    if isinstance(members, list):
        listed_names = [str(item) for item in members]
        member = name in listed_names
        if member:
            hints["member_index"] = listed_names.index(name)

    defaults_keys: list[str] = []
    defaults = table.get("module_defaults")
    if isinstance(defaults, dict):
        for key, values in defaults.items():
            if isinstance(values, list) and name in values:
                defaults_keys.append(str(key))
                hints["module_defaults"][str(key)] = values.index(name)

    overrides_map: dict[str, dict] = {}
    overrides = table.get("node_overrides")
    if isinstance(overrides, dict):
        for node, entry in overrides.items():
            if not isinstance(entry, dict):
                continue
            specs = entry.get("specialists")
            if isinstance(specs, list) and name in specs:
                meta = {k: copy.deepcopy(v) for k, v in entry.items() if k != "specialists"}
                overrides_map[str(node)] = meta
                hints["node_overrides"][str(node)] = specs.index(name)

    canon = {
        "identity": identity,
        "roster": roster_entry,
        "record": record,
        "member": member,
        "module_defaults": sorted(defaults_keys),
        "node_overrides": overrides_map,
    }
    return canon, hints


def digest(canon: dict) -> str:
    text = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_absent(canon: dict | None) -> bool:
    if canon is None:
        return True
    return (
        canon.get("identity") is None
        and canon.get("roster") is None
        and canon.get("record") is None
        and not canon.get("member")
        and not canon.get("module_defaults")
        and not canon.get("node_overrides")
    )


def _all_definitions(profile_dir: Path) -> dict[str, tuple[dict, dict]]:
    table = _table(profile_dir)
    metadata = _metadata(profile_dir)
    return {
        name: definition_of(profile_dir, name, table, metadata)
        for name in employee_names(profile_dir, table, metadata)
    }


# ---------------------------------------------------------------- 落账


def load_state(profile_dir: str | Path, name: str) -> dict | None:
    return _read_json(_agent_dir(Path(profile_dir), name) / VERSION_FILENAME)


def current_version(profile_dir: str | Path, name: str) -> int:
    state = load_state(profile_dir, name)
    return int(state["version"]) if state else 1


def load_snapshot(profile_dir: str | Path, name: str, version: int) -> dict | None:
    return _read_json(
        _agent_dir(Path(profile_dir), name) / SNAPSHOT_DIRNAME / f"v{int(version)}.json"
    )


def _append(profile_dir: Path, name: str, state: dict | None, canon: dict,
            hints: dict, kind: str, detail: str) -> dict:
    version = 1 if state is None else int(state["version"]) + 1
    directory = _agent_dir(profile_dir, name)
    _write_json(
        directory / SNAPSHOT_DIRNAME / f"v{version}.json",
        {"version": version, "definition": canon, "hints": hints},
    )
    log = list(state["log"]) if state else []
    line = {"version": version, "time": _now(), "kind": kind, "detail": detail}
    log.append(line)
    new_state = {"name": name, "version": version, "hash": digest(canon), "log": log}
    _write_json(directory / VERSION_FILENAME, new_state)
    return line


def _record_change(profile_dir: Path, name: str, before: tuple[dict, dict] | None,
                   after: tuple[dict, dict], kind: str, detail: str) -> list[dict]:
    """把 before→after 记一版。新出生的员工（之前不存在且无记录）不写盘：隐含 v1。"""
    state = load_state(profile_dir, name)
    if state is None and (before is None or _is_absent(before[0])):
        return []
    lines: list[dict] = []
    if state is None:
        assert before is not None
        lines.append(_append(profile_dir, name, None, before[0], before[1],
                             "baseline", "首次变更前的定义（存量从 v1 起算）"))
        state = load_state(profile_dir, name)
    elif before is not None and digest(before[0]) != state["hash"]:
        lines.append(_append(profile_dir, name, state, before[0], before[1],
                             "external", "检测到未经版本入口的修改（外部修改）"))
        state = load_state(profile_dir, name)
    lines.append(_append(profile_dir, name, state, after[0], after[1], kind, detail))
    return [{"name": name, **line} for line in lines]


class DefinitionChange:
    """版本管理入口：begin() 记下旧定义 → 调用方改三处 → commit() 逐人比对落账。"""

    def __init__(self, profile_dir: str | Path) -> None:
        self.profile_dir = Path(profile_dir)
        self.before = _all_definitions(self.profile_dir)

    def commit(self, detail: str, kind: str = "change") -> list[dict]:
        after = _all_definitions(self.profile_dir)
        events: list[dict] = []
        for name in sorted(set(self.before) | set(after)):
            old = self.before.get(name)
            new = after.get(name)
            if new is None:
                # 名字从三处全部消失且目录已不在：没有地方落账，跳过。
                continue
            if old is not None and digest(old[0]) == digest(new[0]):
                continue
            if not _agent_dir(self.profile_dir, name).is_dir():
                continue
            events.extend(
                _record_change(self.profile_dir, name, old, new, kind, detail)
            )
        return events


def begin(profile_dir: str | Path) -> DefinitionChange:
    return DefinitionChange(profile_dir)


def check_drift(profile_dir: str | Path,
                detail: str = "检测到未经版本入口的修改（外部修改）") -> list[dict]:
    """已有版本记录的员工逐个比对哈希，不一致补记一版。一致时不写盘。"""
    base = Path(profile_dir)
    if not base.is_dir():
        return []
    events: list[dict] = []
    table = _table(base)
    metadata = _metadata(base)
    for name in employee_names(base, table, metadata):
        state = load_state(base, name)
        if state is None:
            continue
        canon, hints = definition_of(base, name, table, metadata)
        if digest(canon) == state["hash"]:
            continue
        line = _append(base, name, state, canon, hints, "external", detail)
        events.append({"name": name, **line})
    return events


# ---------------------------------------------------------------- 回滚


def _generalist_last_dict(specs: dict) -> None:
    if "generalist" in specs:
        specs["generalist"] = specs.pop("generalist")


def _restore(table: dict, metadata: dict, name: str, canon: dict, hints: dict) -> None:
    roster = table.get("roster")
    if isinstance(roster, dict) and isinstance(roster.get("specialists"), dict):
        specs = roster["specialists"]
        if canon.get("roster") is None:
            specs.pop(name, None)
        else:
            specs[name] = copy.deepcopy(canon["roster"])
            if name != "generalist":
                _generalist_last_dict(specs)

    listed = table.get("specialists")
    if isinstance(listed, list) or canon.get("record") is not None:
        listed = [
            item for item in (listed or [])
            if not (isinstance(item, dict) and str(item.get("id") or "") == name)
        ]
        if canon.get("record") is not None:
            index = hints.get("record_index")
            index = len(listed) if index is None else min(int(index), len(listed))
            listed.insert(index, copy.deepcopy(canon["record"]))
            if name != "generalist":
                general = [i for i in listed if isinstance(i, dict) and i.get("id") == "generalist"]
                others = [i for i in listed if not (isinstance(i, dict) and i.get("id") == "generalist")]
                listed = others + general
        table["specialists"] = listed

    members = metadata.get("specialists")
    if isinstance(members, list) and canon.get("member") is not None:
        members = [item for item in members if str(item) != name]
        if canon["member"]:
            index = hints.get("member_index")
            index = len(members) if index is None else min(int(index), len(members))
            members.insert(index, name)
        metadata["specialists"] = members

    defaults = table.get("module_defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    for key in list(defaults):
        values = defaults[key]
        if isinstance(values, list) and name in values:
            defaults[key] = [item for item in values if item != name]
            if not defaults[key]:
                del defaults[key]
    for key in canon.get("module_defaults") or []:
        values = defaults.setdefault(key, [])
        index = int(hints.get("module_defaults", {}).get(key, len(values)))
        values.insert(min(index, len(values)), name)
    if defaults or "module_defaults" in table:
        table["module_defaults"] = defaults

    overrides = table.get("node_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    for node in list(overrides):
        entry = overrides[node]
        if not isinstance(entry, dict):
            continue
        specs = entry.get("specialists")
        if isinstance(specs, list) and name in specs:
            entry["specialists"] = [item for item in specs if item != name]
            if not entry["specialists"]:
                del overrides[node]
    for node, meta in (canon.get("node_overrides") or {}).items():
        entry = overrides.get(node)
        if not isinstance(entry, dict):
            entry = {"specialists": []}
            overrides[node] = entry
        for key in [k for k in entry if k != "specialists"]:
            del entry[key]
        entry.update(copy.deepcopy(meta))
        specs = entry["specialists"]
        index = int(hints.get("node_overrides", {}).get(node, len(specs)))
        specs.insert(min(index, len(specs)), name)
    if overrides or "node_overrides" in table:
        table["node_overrides"] = overrides


def rollback(profile_dir: str | Path, name: str, target: int) -> dict:
    """把员工整套定义（三处）恢复成第 target 版快照，并记一行回滚事件。"""
    base = Path(profile_dir)
    directory = _agent_dir(base, name)
    if not directory.is_dir():
        archived = base / "agents" / "_archived"
        if archived.is_dir() and any(
            child.name == name or child.name.startswith(f"{name}-")
            for child in archived.iterdir()
        ):
            raise VersionError(
                f"员工 {name} 已删除，版本历史随档案归档；回滚不复活，"
                "要加回来请走 /team add。"
            )
        raise VersionError(f"没有这个员工: {name}")
    state = load_state(base, name)
    if state is None:
        raise VersionError(f"员工 {name} 还没有任何变更记录（当前视为 v1），无可回滚。")
    current = int(state["version"])
    try:
        target = int(target)
    except (TypeError, ValueError) as exc:
        raise VersionError(f"版本号必须是整数: {target!r}") from exc
    if target < 1 or target > current:
        raise VersionError(f"版本号超出范围：{name} 只有 v1–v{current}。")
    snapshot = load_snapshot(base, name, target)
    if snapshot is None:
        raise VersionError(f"找不到 {name} 的 v{target} 快照。")

    events = check_drift(base)
    state = load_state(base, name)
    current = int(state["version"])
    canon = snapshot["definition"]
    hints = snapshot.get("hints") or {}

    table_path = base / "dispatch_table.json"
    meta_path = base / "profile.json"
    table = _table(base)
    metadata = _metadata(base)
    _restore(table, metadata, name, canon, hints)
    if table_path.is_file():
        _write_json(table_path, table)
    if meta_path.is_file():
        _write_json(meta_path, metadata)
    identity_path = directory / "IDENTITY.md"
    wanted = canon.get("identity")
    if wanted is not None:
        existing = identity_path.read_text(encoding="utf-8") if identity_path.is_file() else None
        if existing != wanted:
            identity_path.write_text(wanted, encoding="utf-8")

    now_canon, now_hints = definition_of(base, name)
    line = _append(base, name, state, now_canon, now_hints, "rollback",
                   f"从 v{current} 回滚到 v{target}")
    return {
        "name": name,
        "from": current,
        "to": target,
        "version": line["version"],
        "external": events,
        "restored": digest(now_canon) == digest(canon),
    }


# ---------------------------------------------------------------- 展示


def version_header(profile_dir: str | Path, name: str) -> str:
    """档案头两行：版本号 + 最近一行变更。无记录时返回空串（存量逐字节不变）。"""
    state = load_state(profile_dir, name)
    if not state:
        return ""
    last = state["log"][-1] if state.get("log") else {}
    return (
        f"- 版本: v{state['version']}\n"
        f"- 最近变更: {last.get('time', '—')} {last.get('detail', '—')}\n"
    )


def with_version_header(identity: str, header: str) -> str:
    """把版本头插在 IDENTITY 标题行之后；只用于展示，不写回文件。"""
    if not header or not identity:
        return identity
    first, sep, rest = identity.partition("\n")
    if not first.startswith("# "):
        return header + identity
    return f"{first}\n\n{header}{rest.lstrip(chr(10))}" if sep else f"{first}\n\n{header}"


def format_versions(profile_dir: str | Path, name: str) -> str:
    state = load_state(profile_dir, name)
    if not state:
        if _agent_dir(Path(profile_dir), name).is_dir() or name in employee_names(profile_dir):
            return f"{name} 当前 v1，尚无变更记录。"
        return f"没有这个员工: {name}"
    lines = [f"{name} 当前 v{state['version']}。变更记录："]
    for entry in state.get("log") or []:
        lines.append(f"  v{entry['version']}  {entry['time']}  {entry['detail']}")
    return "\n".join(lines)


def format_rollback(result: dict) -> str:
    text = (
        f"已把 {result['name']} 从 v{result['from']} 回滚到 v{result['to']} 的定义"
        f"（档案/名册/派活对照表三处），记为 v{result['version']}。"
    )
    if result.get("external"):
        text += f" 回滚前补记了 {len(result['external'])} 条外部修改。"
    return text


__all__ = [
    "DefinitionChange",
    "VersionError",
    "begin",
    "check_drift",
    "current_version",
    "definition_of",
    "digest",
    "employee_names",
    "format_rollback",
    "format_versions",
    "load_snapshot",
    "load_state",
    "rollback",
    "version_header",
    "with_version_header",
]
