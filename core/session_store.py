"""会话存档格式与读写层（H20）。

只定格式、只做读写：不做恢复入口，load 只读数据，不执行任何工具、
不碰工作区文件。

目录结构::

    <项目>/.drbug/sessions/
        manifest.json               入口索引（format_version: 1）
        <session-id>/session.json   AgentState + 历史记录 + 会话模型覆盖 + 用量快照
        <session-id>/tool_runs.json 工具执行记录（六态）

每个 JSON 文件都是同一个外壳::

    {"format_version": 1, "kind": "...", "sha256": "<payload 规范化 JSON 的 sha256>",
     "payload": {...}}

写死规则：
- 原子写：同目录临时文件 → fsync → os.replace；失败时删临时文件并
  原样抛出（磁盘不足不吞），旧文件保持完整。
- 校验：读取时重算 sha256，不符当损坏（跳过 + 计数）。
- 版本：format_version 不是已知版本 → 拒绝并计数，不猜测升级。
- 损坏 / 版本不符的 session 跳过计数，列表其余照常（照 runs.py 口径）；
  manifest 损坏或丢失时扫描 sessions/*/session.json 列出会话，
  rebuild_index() 按扫描结果重写 manifest。
- 思考全文只在应用内存，不在 HistoryRecord 里，存档不写。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import error_log
from .usage import UsageRecord

FORMAT_VERSION = 1
SUPPORTED_VERSIONS = (1,)

SESSIONS_DIRNAME = "sessions"
MANIFEST_NAME = "manifest.json"
SESSION_FILE = "session.json"
TOOL_RUNS_FILE = "tool_runs.json"

KIND_MANIFEST = "manifest"
KIND_SESSION = "session"
KIND_TOOL_RUNS = "tool_runs"

# 工具执行六态（数据结构定死）。
TOOL_PENDING = "pending"
TOOL_RUNNING = "running"
TOOL_SUCCEEDED = "succeeded"
TOOL_FAILED = "failed"
TOOL_CANCELLED = "cancelled"
TOOL_UNKNOWN = "unknown"
TOOL_STATES = (
    TOOL_PENDING,
    TOOL_RUNNING,
    TOOL_SUCCEEDED,
    TOOL_FAILED,
    TOOL_CANCELLED,
    TOOL_UNKNOWN,
)
# 恢复时这两态只原样返回，绝不自动重跑。
NEVER_RERUN_STATES = (TOOL_RUNNING, TOOL_UNKNOWN)

SUMMARY_MAX = 60
ARGS_SUMMARY_MAX = 200
RESULT_SUMMARY_MAX = 200

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_SECRET_KEY_RE = re.compile(
    r"(key|token|secret|password|passwd|credential|auth|cookie)", re.I
)
_ABS_PATH_RE = re.compile(r"(?<![\w.])(/[^\s\"',;:]+)")


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------
class SessionStoreError(Exception):
    """存档读写错误的基类。"""


class CorruptArchiveError(SessionStoreError):
    """文件读不出、不是合法 JSON、外壳不全或 sha256 不符。"""


class UnsupportedVersionError(SessionStoreError):
    """format_version 不在已知版本里；拒绝，不猜测升级。"""

    def __init__(self, version: Any) -> None:
        self.version = version
        super().__init__(f"unsupported format_version: {version!r}")


# ---------------------------------------------------------------------------
# 存档根
# ---------------------------------------------------------------------------
def sessions_root_for(workspace: str | Path) -> Path:
    """``<项目>/.drbug/sessions``。

    项目判定沿用 autoprofile.state_root_for：外部项目落在自己的
    ``<项目>/.drbug``；平台树（及其 tests/fixtures）的状态根是平台根本身，
    这里仍放到平台根下的 ``.drbug/sessions``，不与 runs/ 等平铺目录混放。
    """
    from .autoprofile import PLATFORM_ROOT, state_root_for

    state = state_root_for(workspace)
    base = state / ".drbug" if state == PLATFORM_ROOT else state
    return base / SESSIONS_DIRNAME


def valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ""))


def new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 外壳：规范化 JSON + sha256
# ---------------------------------------------------------------------------
def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def wrap(kind: str, payload: dict) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "sha256": payload_sha256(payload),
        "payload": payload,
    }


def migrate(doc: dict) -> dict:
    """版本迁移占位：v1 只有 v1；未知版本拒绝，不猜测升级。"""
    version = doc.get("format_version")
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedVersionError(version)
    return doc


def unwrap(text: str, kind: str) -> dict:
    """解析外壳并校验；返回 payload。版本不符先于校验报出。"""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise CorruptArchiveError(f"invalid json: {exc}") from None
    if not isinstance(doc, dict):
        raise CorruptArchiveError("top level is not an object")
    if "format_version" not in doc:
        raise CorruptArchiveError("missing format_version")
    doc = migrate(doc)
    if doc.get("kind") != kind:
        raise CorruptArchiveError(f"kind mismatch: {doc.get('kind')!r} != {kind!r}")
    payload = doc.get("payload")
    digest = doc.get("sha256")
    if not isinstance(payload, dict) or not isinstance(digest, str):
        raise CorruptArchiveError("missing payload or sha256")
    if payload_sha256(payload) != digest:
        raise CorruptArchiveError("sha256 mismatch")
    return payload


def read_wrapped(path: Path, kind: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptArchiveError(f"unreadable: {exc}") from None
    return unwrap(text, kind)


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------
def atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 → fsync → os.replace。

    任一步失败：删除临时文件，异常原样抛出（磁盘不足等不吞）；
    目标文件要么是旧内容、要么是完整新内容，不会出现半截。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    data = text.encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def write_wrapped(path: Path, kind: str, payload: dict) -> str:
    doc = wrap(kind, payload)
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return doc["sha256"]


# ---------------------------------------------------------------------------
# 历史记录 / 用量 / 模型覆盖 的序列化
# ---------------------------------------------------------------------------
def record_to_dict(record: Any) -> dict:
    """HistoryRecord (kind, text, tone, actual, seq, ts) 全字段。

    diff kind 的 text 就是 patch.diff 原文；思考全文不在记录里，不写。
    """
    kind, text, tone = record[0], record[1], record[2]
    actual = getattr(record, "actual", None)
    data = {
        "kind": kind,
        "text": text,
        "tone": tone,
        "actual": list(actual) if actual is not None else None,
        "seq": getattr(record, "seq", None),
        "ts": getattr(record, "ts", None),
    }
    attachments = getattr(record, "attachments", None)
    if attachments:
        # PST1：只在有附件时写，旧存档与无附件记录的字节不变。
        data["attachments"] = [list(item) for item in attachments]
    tool = getattr(record, "tool", None)
    if tool:
        # BLK2：工具块只落折叠行所需的元数据；参数/结果摘录不落盘。
        data["tool"] = {
            "call_id": str(tool.get("call_id", "")),
            "name": str(tool.get("name", "")),
            "state": str(tool.get("state", "unknown")),
        }
    return data


def record_from_dict(data: dict) -> Any:
    from .tui import HistoryRecord  # 延迟导入：存档层不在导入期拉起 TUI

    actual = data.get("actual")
    record = HistoryRecord(
        str(data["kind"]),
        str(data["text"]),
        str(data.get("tone", "plain")),
        actual=tuple(actual) if actual is not None else None,
    )
    record.seq = data.get("seq")
    record.ts = data.get("ts")
    attachments = data.get("attachments")
    if attachments:
        record.attachments = tuple(tuple(item) for item in attachments)
    tool = data.get("tool")
    if isinstance(tool, dict):
        record.tool = dict(tool)
    return record


def usage_to_list(records: Iterable[UsageRecord]) -> list[dict]:
    """按 TUIM1b 契约原样存：未知字段保持 None，不记 0；source 带 estimate 标记。"""
    return [asdict(r) for r in records]


def usage_from_list(items: Iterable[dict]) -> list[UsageRecord]:
    return [UsageRecord(**item) for item in items]


def override_to_dict(override: Any) -> dict | None:
    if override is None:
        return None
    if isinstance(override, (tuple, list)):
        provider, model, effort = override
    else:
        provider, model, effort = override.provider, override.model, override.effort
    return {"provider": provider, "model": model, "effort": effort}


def override_from_dict(data: dict | None) -> Any:
    if data is None:
        return None
    from .model_state import ModelTriple

    return ModelTriple(
        provider=data["provider"], model=data["model"], effort=data.get("effort")
    )


def last_summary(history: list[dict]) -> str:
    for item in reversed(history):
        text = (item.get("text") or "").strip()
        if text:
            first = text.splitlines()[0].strip()
            return first[:SUMMARY_MAX]
    return ""


# ---------------------------------------------------------------------------
# 工具执行记录
# ---------------------------------------------------------------------------
def summarize_args(args: Any, workspace: str | Path | None = None) -> str:
    """参数摘要（脱敏）：不存密钥，不存绝对路径原文。

    - 键名像密钥的值 → ‹已脱敏›；文本里的 sk-xxx / token=xxx 同 error_log 口径；
    - 工作区内绝对路径 → 相对路径；其余绝对路径只留文件名（前缀 …/）；
    - 截断到 ARGS_SUMMARY_MAX 字。
    """
    ws = None
    if workspace is not None:
        try:
            ws = str(Path(workspace).expanduser().resolve())
        except OSError:
            ws = str(workspace)

    def scrub(value: Any, key: str | None = None) -> Any:
        if key is not None and _SECRET_KEY_RE.search(key):
            return error_log.REDACTED
        if isinstance(value, dict):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            return scrub_text(value, ws)
        return value

    text = json.dumps(scrub(args), ensure_ascii=False, sort_keys=True)
    return text[:ARGS_SUMMARY_MAX]


def _scrub_paths(text: str, ws: str | None) -> str:
    def repl(match: re.Match) -> str:
        raw = match.group(1)
        if ws and (raw == ws or raw.startswith(ws + "/")):
            rel = raw[len(ws):].lstrip("/")
            return rel or "."
        name = raw.rstrip("/").rsplit("/", 1)[-1]
        return f"…/{name}" if name else "…"

    return _ABS_PATH_RE.sub(repl, text)


def scrub_text(text: str, ws: str | None) -> str:
    """BLK2：工具记录与工具块共用的唯一脱敏实现（不截断）。

    = error_log.redact（密钥打码、家目录 ~）+ _scrub_paths（工作区内绝对路径
    → 相对，其余绝对路径只留 …/文件名）。``ws`` 须是已解析的工作区绝对路径
    字符串或 None。summarize_args / summarize_result 在它之后各自截断。
    """
    return _scrub_paths(error_log.redact(text), ws)


def summarize_result(text: str | None, workspace: str | Path | None = None) -> str | None:
    if text is None:
        return None
    ws = str(Path(workspace).expanduser().resolve()) if workspace else None
    return scrub_text(str(text), ws)[:RESULT_SUMMARY_MAX]


@dataclass
class ToolRun:
    """一条工具调用记录。参数 / 结果只存脱敏摘要。"""

    call_id: str
    tool: str
    args_summary: str
    state: str = TOOL_PENDING
    started_at: str | None = None
    ended_at: str | None = None
    result_summary: str | None = None

    def __post_init__(self) -> None:
        if self.state not in TOOL_STATES:
            raise ValueError(f"unknown tool state: {self.state!r}")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ToolRun":
        return cls(
            call_id=str(data["call_id"]),
            tool=str(data["tool"]),
            args_summary=str(data.get("args_summary", "")),
            state=str(data["state"]),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            result_summary=data.get("result_summary"),
        )

    @property
    def needs_attention(self) -> bool:
        """running / unknown：恢复后只提示，绝不自动重跑。"""
        return self.state in NEVER_RERUN_STATES


# ---------------------------------------------------------------------------
# 会话数据
# ---------------------------------------------------------------------------
@dataclass
class SessionData:
    """一次会话的存档内容（读写层的输入 / 输出）。"""

    session_id: str
    title: str
    workspace: str
    agent_state: Any = None  # agentscope AgentState；None 表示无
    history: list = field(default_factory=list)  # HistoryRecord 列表
    model_override: Any = None  # ModelTriple / (p, m, e) / None
    usage: list = field(default_factory=list)  # UsageRecord 列表
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class LoadedSession:
    session: SessionData
    tool_runs: list[ToolRun]


@dataclass(frozen=True)
class SessionEntry:
    """manifest 一条：id / 标题 / 工作区 / 创建 / 更新 / 消息数 / 最后摘要。"""

    session_id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    message_count: int
    last_summary: str
    sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "last_summary": self.last_summary,
            "sha256": self.sha256,
        }

    @classmethod
    def from_payload(cls, payload: dict, digest: str | None) -> "SessionEntry":
        history = payload.get("history") or []
        return cls(
            session_id=str(payload["id"]),
            title=str(payload.get("title") or ""),
            workspace=str(payload.get("workspace") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            message_count=len(history),
            last_summary=last_summary(history),
            sha256=digest,
        )


@dataclass
class ListResult:
    entries: list[SessionEntry]
    corrupt: int = 0  # 读不出 / 非法 JSON / 校验失败 / 缺文件
    rejected_version: int = 0  # 未知 format_version
    manifest_status: str = "ok"  # ok | missing | corrupt | rejected_version

    @property
    def skipped(self) -> int:
        return self.corrupt + self.rejected_version


def _agent_state_to_json(state: Any) -> Any:
    if state is None:
        return None
    return state.model_dump(mode="json")


def _agent_state_from_json(data: Any) -> Any:
    if data is None:
        return None
    from agentscope.state import AgentState

    return AgentState.model_validate(data)


def session_payload(session: SessionData) -> dict:
    history = [record_to_dict(r) for r in session.history]
    return {
        "id": session.session_id,
        "title": session.title,
        "workspace": session.workspace,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "agent_state": _agent_state_to_json(session.agent_state),
        "history": history,
        "model_override": override_to_dict(session.model_override),
        "usage": usage_to_list(session.usage),
    }


def session_from_payload(payload: dict) -> SessionData:
    return SessionData(
        session_id=str(payload["id"]),
        title=str(payload.get("title") or ""),
        workspace=str(payload.get("workspace") or ""),
        agent_state=_agent_state_from_json(payload.get("agent_state")),
        history=[record_from_dict(d) for d in payload.get("history") or []],
        model_override=override_from_dict(payload.get("model_override")),
        usage=usage_from_list(payload.get("usage") or []),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# 存档读写
# ---------------------------------------------------------------------------
class SessionStore:
    """``<root>`` 即 sessions 目录。只读写存档，不执行任何东西。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "SessionStore":
        return cls(sessions_root_for(workspace))

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def session_dir(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.root / session_id

    # -- 写 --------------------------------------------------------------
    def save(self, session: SessionData, tool_runs: Iterable[ToolRun] = ()) -> SessionEntry:
        """写 session.json + tool_runs.json，再更新 manifest；每个文件都原子写。"""
        directory = self.session_dir(session.session_id)
        stamp = now_iso()
        if not session.created_at:
            session.created_at = stamp
        session.updated_at = stamp
        payload = session_payload(session)
        runs = [r.to_dict() for r in tool_runs]
        write_wrapped(directory / TOOL_RUNS_FILE, KIND_TOOL_RUNS, {"runs": runs})
        digest = write_wrapped(directory / SESSION_FILE, KIND_SESSION, payload)
        entry = SessionEntry.from_payload(payload, digest)
        entries = self._manifest_entries_for_update()
        entries = [e for e in entries if e.session_id != entry.session_id]
        entries.append(entry)
        self._write_manifest(entries)
        return entry

    def _write_manifest(self, entries: list[SessionEntry]) -> None:
        ordered = sorted(entries, key=lambda e: (e.updated_at, e.session_id), reverse=True)
        write_wrapped(
            self.manifest_path,
            KIND_MANIFEST,
            {"sessions": [e.to_dict() for e in ordered]},
        )

    def _manifest_entries_for_update(self) -> list[SessionEntry]:
        try:
            return self._read_manifest()
        except (FileNotFoundError, SessionStoreError):
            # manifest 坏了 / 丢了：用扫描结果作底，保存时顺带重建
            return self._scan().entries

    # -- 读 --------------------------------------------------------------
    def _read_manifest(self) -> list[SessionEntry]:
        payload = read_wrapped(self.manifest_path, KIND_MANIFEST)
        items = payload.get("sessions")
        if not isinstance(items, list):
            raise CorruptArchiveError("manifest sessions is not a list")
        entries = []
        for item in items:
            try:
                entries.append(
                    SessionEntry(
                        session_id=str(item["id"]),
                        title=str(item.get("title") or ""),
                        workspace=str(item.get("workspace") or ""),
                        created_at=str(item.get("created_at") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                        message_count=int(item.get("message_count") or 0),
                        last_summary=str(item.get("last_summary") or ""),
                        sha256=item.get("sha256"),
                    )
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                raise CorruptArchiveError("bad manifest entry") from None
        return entries

    def _read_session_payload(self, session_id: str) -> tuple[dict, str]:
        path = self.session_dir(session_id) / SESSION_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CorruptArchiveError("session.json missing") from None
        except (OSError, UnicodeDecodeError) as exc:
            raise CorruptArchiveError(f"unreadable: {exc}") from None
        payload = unwrap(text, KIND_SESSION)
        if str(payload.get("id")) != session_id:
            raise CorruptArchiveError("session id does not match directory")
        return payload, payload_sha256(payload)

    def _scan(self) -> ListResult:
        result = ListResult(entries=[])
        if not self.root.is_dir():
            return result
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or not valid_session_id(child.name):
                continue
            try:
                payload, digest = self._read_session_payload(child.name)
                result.entries.append(SessionEntry.from_payload(payload, digest))
            except UnsupportedVersionError:
                result.rejected_version += 1
            except (SessionStoreError, KeyError, TypeError, ValueError):
                result.corrupt += 1
        result.entries.sort(key=lambda e: (e.updated_at, e.session_id), reverse=True)
        return result

    def list_sessions(self) -> ListResult:
        """列会话：损坏 / 版本不符的条目跳过计数，其余照常。

        manifest 正常时以它为索引，并逐条校验 session.json；manifest 丢失、
        损坏或版本不符时改为扫描目录（不写盘；要重写 manifest 用
        rebuild_index）。
        """
        try:
            indexed = self._read_manifest()
        except FileNotFoundError:
            result = self._scan()
            result.manifest_status = "missing"
            return result
        except UnsupportedVersionError:
            result = self._scan()
            result.manifest_status = "rejected_version"
            return result
        except SessionStoreError:
            result = self._scan()
            result.manifest_status = "corrupt"
            return result
        result = ListResult(entries=[])
        for entry in indexed:
            try:
                payload, digest = self._read_session_payload(entry.session_id)
                result.entries.append(SessionEntry.from_payload(payload, digest))
            except UnsupportedVersionError:
                result.rejected_version += 1
            except (SessionStoreError, KeyError, TypeError, ValueError):
                result.corrupt += 1
        return result

    def rebuild_index(self) -> ListResult:
        """从 sessions/*/session.json 扫描重建 manifest（字段全从各文件读）。"""
        result = self._scan()
        self._write_manifest(result.entries)
        return result

    def load(self, session_id: str) -> LoadedSession:
        """只读：解析 session.json 与 tool_runs.json。

        不执行任何工具、不写任何文件。running / unknown 的工具记录原样
        返回，由调用方决定如何提示，本层绝不重跑。tool_runs.json 缺失
        视为没有工具记录；损坏或版本不符照常抛错。
        """
        payload, _digest = self._read_session_payload(session_id)
        try:
            session = session_from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptArchiveError(f"bad session payload: {exc}") from None
        runs_path = self.session_dir(session_id) / TOOL_RUNS_FILE
        try:
            runs_payload = read_wrapped(runs_path, KIND_TOOL_RUNS)
        except FileNotFoundError:
            runs_payload = {"runs": []}
        try:
            runs = [ToolRun.from_dict(d) for d in runs_payload.get("runs") or []]
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptArchiveError(f"bad tool_runs: {exc}") from None
        return LoadedSession(session=session, tool_runs=runs)
