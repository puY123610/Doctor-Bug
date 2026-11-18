"""会话管理（F1）：/sessions 的列表 / 恢复准备 / 重命名 / 删除。

后端只用 H20 的 core/session_store.py，不另造存档层。本模块只做纯逻辑，
TUI 接线在 core/tui.py。

写死规则：
- 列表：损坏或版本不符的条目标「已损坏」，工作区目录已不存在的标
  「工作区不存在」，都不崩列表。
- 恢复：只读 load；running / unknown 工具记录原样带回、绝不重跑；
  AgentState 只回填对话内容（session_id / summary / context），
  权限上下文换成按工作区新建的默认值，等待中的工具调用与回复上下文
  清空——恢复不扩大权限，旧授权不复活。
- 重命名：只改标题（session.json 内 + manifest 条目），id 与其他字段
  （含 updated_at）不动；空标题或超过 TITLE_MAX 字拒绝。
- 删除：删 session.json + tool_runs.json + manifest 条目，不可恢复；
  确认行由程序按真实参数（标题 / id / 消息数）生成。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import session_store as ss

TITLE_MAX = 100

STATUS_OK = "ok"
STATUS_CORRUPT = "已损坏"
STATUS_NO_WORKSPACE = "工作区不存在"


@dataclass(frozen=True)
class ListedSession:
    session_id: str
    status: str  # STATUS_OK | STATUS_CORRUPT | STATUS_NO_WORKSPACE
    entry: ss.SessionEntry | None
    reason: str = ""  # 已损坏时的原因：「版本不符」/「校验失败或无法读取」

    @property
    def restorable(self) -> bool:
        return self.status == STATUS_OK


def _candidate_ids(store: ss.SessionStore) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        sid = str(value)
        if ss.valid_session_id(sid) and sid not in seen:
            seen.add(sid)
            ids.append(sid)

    # manifest 只作线索（不校验也读），条目以各自 session.json 为准
    try:
        raw = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        for item in (raw.get("payload") or {}).get("sessions") or []:
            if isinstance(item, dict):
                add(item.get("id"))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    if store.root.is_dir():
        for child in sorted(store.root.iterdir()):
            if child.is_dir():
                add(child.name)
    return ids


def list_sessions(store: ss.SessionStore) -> list[ListedSession]:
    """本工作区存档里的全部会话；可恢复的按更新时间倒序在前，坏条目在后。"""
    good: list[ListedSession] = []
    bad: list[ListedSession] = []
    for sid in _candidate_ids(store):
        try:
            payload, digest = store._read_session_payload(sid)
            entry = ss.SessionEntry.from_payload(payload, digest)
        except ss.UnsupportedVersionError:
            bad.append(ListedSession(sid, STATUS_CORRUPT, None, "版本不符"))
            continue
        except (ss.SessionStoreError, KeyError, TypeError, ValueError):
            bad.append(ListedSession(sid, STATUS_CORRUPT, None, "校验失败或无法读取"))
            continue
        status = STATUS_OK if _workspace_exists(entry.workspace) else STATUS_NO_WORKSPACE
        good.append(ListedSession(sid, status, entry))
    good.sort(key=lambda s: (s.entry.updated_at, s.session_id), reverse=True)
    return good + bad


def _workspace_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).expanduser().is_dir()
    except OSError:
        return False


def format_row(item: ListedSession) -> str:
    """列表一行：id / 标题 / 时间 / 消息数 / 最后摘要；坏条目只标状态。"""
    if item.entry is None:
        return f"{item.session_id}  [{STATUS_CORRUPT}]  {item.reason}"
    e = item.entry
    when = (e.updated_at or "").replace("T", " ")[:16]
    mark = f"  [{item.status}]" if item.status != STATUS_OK else ""
    summary = f"  {e.last_summary}" if e.last_summary else ""
    return f"{e.session_id}  {e.title or '（无标题）'}  {when}  {e.message_count} 条{mark}{summary}"


def find(store: ss.SessionStore, session_id: str) -> ListedSession | None:
    for item in list_sessions(store):
        if item.session_id == session_id:
            return item
    return None


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------
def sanitize_agent_state(state: Any, workspace: str | Path) -> Any:
    """只回填对话内容；权限 / 工具等待 / 回复上下文一律按新会话重建。"""
    from .agentscope_actors import build_state

    fresh = build_state(Path(workspace))
    if state is None:
        return fresh
    return fresh.model_copy(
        update={
            "session_id": state.session_id,
            "summary": state.summary,
            "context": list(state.context),
        }
    )


class RestoreRefused(Exception):
    """恢复被拒：会话已损坏 / 工作区不存在 / 找不到。message 即界面文案。"""


def prepare_restore(store: ss.SessionStore, session_id: str) -> ss.LoadedSession:
    """恢复前检查 + 只读 load。工作区不存在先报并拒绝，不猜新路径。"""
    if not ss.valid_session_id(session_id):
        raise RestoreRefused(f"会话 id 不合法：{session_id}")
    item = find(store, session_id)
    if item is None:
        raise RestoreRefused(f"找不到会话：{session_id}")
    if item.status == STATUS_CORRUPT:
        raise RestoreRefused(f"会话 {session_id} 已损坏（{item.reason}），无法恢复")
    if item.status == STATUS_NO_WORKSPACE:
        raise RestoreRefused(
            f"会话 {session_id} 的工作区不存在：{item.entry.workspace}。"
            "不会猜测新路径，也不会自动改绑，恢复已取消"
        )
    loaded = store.load(session_id)
    loaded.session.agent_state = sanitize_agent_state(
        loaded.session.agent_state, loaded.session.workspace
    )
    return loaded


def tool_run_lines(runs: list[ss.ToolRun]) -> list[str]:
    """需要注意的工具记录（running / unknown）：只展示，不重跑。"""
    lines = []
    for run in runs:
        if run.needs_attention:
            lines.append(
                f"工具 {run.tool}（{run.call_id}）存档时状态为 {run.state}，"
                "不会自动重跑；如需要请手动重新发起"
            )
    return lines


# ---------------------------------------------------------------------------
# 重命名
# ---------------------------------------------------------------------------
def validate_title(title: str) -> str | None:
    """返回拒绝原因；合法返回 None。"""
    text = (title or "").strip()
    if not text:
        return "标题不能为空"
    if "\n" in text or "\r" in text:
        return "标题不能换行"
    if len(text) > TITLE_MAX:
        return f"标题超长：{len(text)} 字，最多 {TITLE_MAX} 字"
    return None


def rename(store: ss.SessionStore, session_id: str, title: str) -> ss.SessionEntry:
    """只改标题；id、updated_at 与其他字段不动。失败抛 ValueError（文案）。"""
    problem = validate_title(title)
    if problem:
        raise ValueError(problem)
    if not ss.valid_session_id(session_id):
        raise ValueError(f"会话 id 不合法：{session_id}")
    try:
        payload, _ = store._read_session_payload(session_id)
    except ss.UnsupportedVersionError:
        raise ValueError(f"会话 {session_id} 版本不符，不能改名") from None
    except ss.SessionStoreError:
        if not (store.root / session_id).is_dir():
            raise ValueError(f"找不到会话：{session_id}") from None
        raise ValueError(f"会话 {session_id} 已损坏，不能改名") from None
    payload = dict(payload)
    payload["title"] = title.strip()
    digest = ss.write_wrapped(
        store.session_dir(session_id) / ss.SESSION_FILE, ss.KIND_SESSION, payload
    )
    entry = ss.SessionEntry.from_payload(payload, digest)
    entries = [e for e in store._manifest_entries_for_update() if e.session_id != session_id]
    entries.append(entry)
    store._write_manifest(entries)
    return entry


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------
def delete_impact_lines(item: ListedSession, store: ss.SessionStore) -> list[str]:
    """确认前先说清影响范围（程序按真实路径生成）。"""
    directory = store.root / item.session_id
    return [
        "删除会话会移除以下内容，删除后不可恢复：",
        f"- {directory / ss.SESSION_FILE}（对话、历史记录、模型覆盖、用量快照）",
        f"- {directory / ss.TOOL_RUNS_FILE}（工具执行记录）",
        f"- {store.manifest_path} 里该会话的索引条目",
        "不会碰工作区文件，也不会碰其他会话",
    ]


def delete_confirm_line(item: ListedSession) -> str:
    """确认行：只用真实参数（标题 / id / 消息数），不接受任何外来描述。"""
    if item.entry is None:
        return (
            f"确认删除已损坏的会话 {item.session_id}（标题与消息数无法读取）？"
            "不可恢复。Y 删除 / N 取消"
        )
    e = item.entry
    return (
        f"确认删除会话「{e.title or '（无标题）'}」（id {e.session_id}，"
        f"{e.message_count} 条消息）？不可恢复。Y 删除 / N 取消"
    )


def delete(store: ss.SessionStore, session_id: str) -> list[str]:
    """删 session.json + tool_runs.json + manifest 条目；返回已删文件。

    目录删空后一并移除；目录里若还有别的文件则保留目录、不碰那些文件。
    """
    directory = store.session_dir(session_id)
    removed: list[str] = []
    for name in (ss.SESSION_FILE, ss.TOOL_RUNS_FILE):
        path = directory / name
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass
    entries = [e for e in store._manifest_entries_for_update() if e.session_id != session_id]
    store._write_manifest(entries)
    return removed
