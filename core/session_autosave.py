"""F2：存档入口的纯逻辑（标题规则、组装存档、工具态映射）。

只用 H20 的 core/session_store.py 写入口，不改格式。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from . import session_store as ss

TITLE_CHARS = 30
UNTITLED = "未命名会话"

# agentscope ToolResultState → H20 六态。denied 视为未执行（cancelled）。
_TOOL_STATE_MAP = {
    "success": ss.TOOL_SUCCEEDED,
    "error": ss.TOOL_FAILED,
    "interrupted": ss.TOOL_CANCELLED,
    "denied": ss.TOOL_CANCELLED,
    "running": ss.TOOL_RUNNING,
}


def tool_state(raw: Any) -> str:
    """框架给的结果态 → 六态；认不出的记 unknown（恢复时只提示不重跑）。"""
    key = str(getattr(raw, "value", raw) or "").lower()
    return _TOOL_STATE_MAP.get(key, ss.TOOL_UNKNOWN)


def default_title(history: Iterable[Any], now: float | None = None) -> str:
    """首条用户消息前 30 字；没有用户消息取「未命名会话」+ 时间。

    斜杠命令不算用户消息。
    """
    for record in history:
        if record[0] != "user":
            continue
        text = " ".join(str(record[1]).split())
        if not text or text.startswith("/"):
            continue
        return text[:TITLE_CHARS]
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    return f"{UNTITLED} {stamp}"


def _existing(store: ss.SessionStore, session_id: str) -> ss.SessionData | None:
    """已有存档（读不出就当没有）。用来保留标题与创建时间。"""
    try:
        return store.load(session_id).session
    except Exception:  # noqa: BLE001 - 坏档 / 不存在都按新建处理
        return None


def build_session(
    store: ss.SessionStore,
    *,
    session_id: str,
    workspace: str | Path,
    history: list,
    agent_state: Any,
    model_override: Any,
    usage: list,
) -> ss.SessionData:
    """组装一份 SessionData。已有存档的标题（含 F1 重命名的）原样保留。"""
    existing = _existing(store, session_id)
    if existing is not None and existing.title:
        title = existing.title
    else:
        title = default_title(history)
    return ss.SessionData(
        session_id=session_id,
        title=title,
        workspace=str(workspace),
        agent_state=agent_state,
        history=list(history),
        model_override=model_override,
        usage=list(usage),
        created_at=existing.created_at if existing is not None else None,
    )
