"""@ 提及文件（TUID2）：补全只发现、读取过门禁、发送前复验。

三段红线（逐段有测试）：

1. 补全只发现：``discover_files`` 只用 ``os.scandir`` 列目录项与
   ``lstat``/``resolve`` 判越界，从不打开任何文件、不读内容。
2. 读取过门禁：``read_attachment`` 先校验路径，再走
   ``PolicyEngine.decide("secretary", "read_file", …)``；不是 allow
   一律抛 ``MentionError``（明确原因），调用方不得发送。
3. 发送前复验：``recheck_attachment`` 再验一次路径在工作区内、文件
   仍在、大小与 mtime 未变、门禁仍放行；任何一项不过都报错，不重读。

本模块不依赖 TUI，便于单测。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .prompt_extras import PASTE_MAX_BYTES

# 与粘贴单块上限同一常量（100 KB，按字节计）。
MENTION_MAX_BYTES = PASTE_MAX_BYTES

# 补全最多列出的条目数、单次最多扫描的目录项数（防大仓库卡顿）。
MENTION_LIST_LIMIT = 8
MENTION_SCAN_LIMIT = 5000

# 不进入的目录：版本库、虚拟环境、缓存与 drbug 自身存档。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".drbug",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)

NO_MATCH_TEXT = "没有匹配的文件（只列工作区内文件）"
UNBOUND_TEXT = "还没绑定工作区，@ 提及文件不可用"

# 输入末尾的 @ 片段：行首或空白之后的 @，后面直到末尾都不含空白。
_MENTION_TAIL_RE = re.compile(r"(?:^|(?<=\s))@([^\s@]*)$")


class MentionError(Exception):
    """@ 附件被拒或读不到。消息就是给老板看的原因。"""


@dataclass(frozen=True)
class FileAttachment:
    """一份已过门禁的文本附件快照。"""

    rel: str  # 工作区内相对路径（POSIX 风格）
    resolved: Path
    size: int
    mtime_ns: int
    text: str

    @property
    def marker(self) -> str:
        return "@" + self.rel


def mention_query(text: str) -> str | None:
    """输入末尾正在打的 @ 片段（不含 @）；不在打 @ 时返回 None。"""
    match = _MENTION_TAIL_RE.search(text)
    if match is None:
        return None
    return match.group(1)


def replace_mention_tail(text: str, rel: str) -> str:
    """把末尾的 @片段 换成完整提及标记，并补一个空格。"""
    match = _MENTION_TAIL_RE.search(text)
    if match is None:
        return text
    return text[: match.start()] + "@" + rel + " "


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def discover_files(
    workspace: str | Path,
    query: str = "",
    *,
    limit: int = MENTION_LIST_LIMIT,
    scan_limit: int = MENTION_SCAN_LIMIT,
) -> list[str]:
    """列工作区内文件的相对路径（按 query 过滤）。只发现，不读内容。

    - 只用 scandir 与 stat 元数据，从不打开文件；
    - 不跟随符号链接目录；指向工作区外的符号链接文件不列出；
    - 过滤：query 的每个字符按顺序出现在相对路径里（大小写不敏感），
      其中路径或文件名以 query 开头的排前面。
    """
    root = Path(workspace).expanduser().resolve()
    needle = query.lower()
    hits: list[tuple[int, int, str]] = []
    scanned = 0
    stack: list[Path] = [root]
    while stack and scanned < scan_limit:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        subdirs: list[Path] = []
        for entry in entries:
            scanned += 1
            if scanned > scan_limit:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS:
                        subdirs.append(Path(entry.path))
                    continue
                if entry.is_symlink():
                    target = Path(entry.path).resolve()
                    if not _is_under(target, root) or not target.is_file():
                        continue
                elif not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            rel = Path(entry.path).relative_to(root).as_posix()
            rank = _match_rank(rel, needle)
            if rank is None:
                continue
            hits.append((rank, len(rel), rel))
        stack.extend(reversed(subdirs))
    hits.sort()
    return [rel for _, _, rel in hits[:limit]]


def _match_rank(rel: str, needle: str) -> int | None:
    if not needle:
        return 1
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    if low.startswith(needle) or name.startswith(needle):
        return 0
    if needle in low:
        return 1
    pos = 0
    for char in needle:
        pos = low.find(char, pos)
        if pos < 0:
            return None
        pos += 1
    return 2


def check_mention_path(workspace: str | Path, rel: str) -> Path:
    """校验提及路径，返回 resolve 后的真实路径。越界直接拒绝并说明。"""
    raw = str(rel).strip()
    if not raw:
        raise MentionError("附件路径为空")
    if raw.startswith("~") or os.path.isabs(raw) or PurePosixPath(raw).is_absolute():
        raise MentionError(f"附件 {raw} 是绝对路径，只接受工作区内相对路径，已拒绝")
    parts = re.split(r"[\\/]+", raw)
    if ".." in parts:
        raise MentionError(f"附件 {raw} 含 ../，不许跳出工作区，已拒绝")
    root = Path(workspace).expanduser().resolve()
    resolved = (root / raw).resolve()
    if not _is_under(resolved, root) or resolved == root:
        raise MentionError(
            f"附件 {raw} 解析后落在工作区外（符号链接越界），已拒绝"
        )
    if not resolved.exists():
        raise MentionError(f"附件 {raw} 不存在，未发送")
    if not resolved.is_file():
        raise MentionError(f"附件 {raw} 不是普通文件，未发送")
    return resolved


def _gate(engine: Any, resolved: Path, rel: str) -> None:
    decision = engine.decide("secretary", "read_file", str(resolved))
    verdict = getattr(decision, "verdict", "deny")
    if verdict == "allow":
        return
    tier = getattr(decision, "tier", "?")
    reason = getattr(decision, "reason", "")
    if verdict == "ask":
        raise MentionError(
            f"附件 {rel} 读取需要确认（tier {tier}：{reason}），"
            "@ 附件不走确认通道，已拒绝、未发送"
        )
    raise MentionError(f"附件 {rel} 被门禁拒绝（tier {tier}：{reason}），未发送")


def _default_engine(workspace: str | Path) -> Any:
    from .policy import PolicyEngine

    return PolicyEngine(workspace=Path(workspace).expanduser().resolve())


def read_attachment(
    workspace: str | Path,
    rel: str,
    engine: Any = None,
) -> FileAttachment:
    """选中后读取：路径校验 → 门禁 → 大小上限 → 读字节 → 二进制识别。"""
    resolved = check_mention_path(workspace, rel)
    _gate(engine or _default_engine(workspace), resolved, rel)
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise MentionError(f"附件 {rel} 读不到：{exc}") from exc
    if stat.st_size > MENTION_MAX_BYTES:
        raise MentionError(
            f"附件 {rel} 大小 {format_size(stat.st_size)}，超过上限 100 KB，已拒绝"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise MentionError(f"附件 {rel} 读不到：{exc}") from exc
    if len(data) > MENTION_MAX_BYTES:
        raise MentionError(
            f"附件 {rel} 大小 {format_size(len(data))}，超过上限 100 KB，已拒绝"
        )
    if b"\x00" in data:
        raise MentionError(f"附件 {rel} 是二进制文件（含 NUL 字节），不能作文本附件，已拒绝")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MentionError(
            f"附件 {rel} 不是 UTF-8 文本（疑似二进制），不能作文本附件，已拒绝"
        ) from exc
    return FileAttachment(
        rel=Path(rel).as_posix(),
        resolved=resolved,
        size=len(data),
        mtime_ns=stat.st_mtime_ns,
        text=text,
    )


def recheck_attachment(
    workspace: str | Path,
    attachment: FileAttachment,
    engine: Any = None,
) -> FileAttachment:
    """发送前复验。定版行为：状态变了就报错，不重读（老板重新 @ 一次）。"""
    rel = attachment.rel
    resolved = check_mention_path(workspace, rel)
    if resolved != attachment.resolved:
        raise MentionError(f"附件 {rel} 指向的文件变了，未发送；请重新 @ 一次")
    _gate(engine or _default_engine(workspace), resolved, rel)
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise MentionError(f"附件 {rel} 读不到：{exc}") from exc
    if stat.st_size != attachment.size or stat.st_mtime_ns != attachment.mtime_ns:
        raise MentionError(
            f"附件 {rel} 在选中后被改过（大小或修改时间变了），未发送；"
            "请重新 @ 一次"
        )
    return attachment


def attachment_note(attachment: FileAttachment) -> str:
    """消息里的明示行，与「已附图：…」同一口径。"""
    return f"已附文件：{attachment.rel}（{format_size(attachment.size)}）"


def attachment_block_text(attachment: FileAttachment) -> str:
    """发给主治医生的附件块：头行写明文件名 + 大小，正文原样。"""
    return (
        f"[附件 {attachment.rel}（{format_size(attachment.size)}）]\n"
        f"{attachment.text}\n"
        f"[附件结束 {attachment.rel}]"
    )
