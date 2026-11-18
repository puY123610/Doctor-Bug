"""输入框扩展（TUID1）：草稿暂存槽 + 粘贴块表。

纯数据逻辑，不依赖 Textual；core.tui 负责接线与落行。

草稿暂存：按应用实例单槽位，只在内存里，退出即消失。
粘贴折叠：长粘贴全文进粘贴块表（块号自增），输入框只放占位符
「〖已粘贴 N 行 #k〗」；提交时逐个展开回全文。占位符被改坏
（与原样不一致）就当普通文本，不展开。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

PASTE_FOLD_LINES = 5  # 行数 > 5 折叠
PASTE_FOLD_CHARS = 400  # 长度 > 400 字折叠
PASTE_MAX_BYTES = 100 * 1024  # 单块全文上限 100 KB（按 UTF-8 字节计）

_PLACEHOLDER_RE = re.compile(r"〖已粘贴 (\d+) 行 #(\d+)〗")
# PST1：粘贴的图片/文件路径折成附件块占位，编号与文本块 #k 分开计数。
_ATTACHMENT_RE = re.compile(r"〖(Image|File) (\d+)〗")


@dataclass(frozen=True)
class StashedDraft:
    """暂存的草稿：正文 + 已附图片路径（原样 token）+ 光标位置。"""

    text: str
    attachments: tuple[str, ...]
    cursor: tuple[int, int]


def paste_line_count(text: str) -> int:
    return max(1, len(text.splitlines()))


def paste_too_big(text: str) -> bool:
    return len(text.encode("utf-8")) > PASTE_MAX_BYTES


def paste_should_fold(text: str) -> bool:
    return paste_line_count(text) > PASTE_FOLD_LINES or len(text) > PASTE_FOLD_CHARS


def placeholder(lines: int, block_id: int) -> str:
    return f"〖已粘贴 {lines} 行 #{block_id}〗"


@dataclass(frozen=True)
class PasteBlock:
    block_id: int
    lines: int
    text: str

    @property
    def placeholder(self) -> str:
        return placeholder(self.lines, self.block_id)


@dataclass(frozen=True)
class AttachmentBlock:
    """PST1：用户亲手粘贴的图片/文件。输入框只见编号，真实路径只在这里。"""

    kind: str  # "Image" | "File"
    number: int
    path: str

    @property
    def placeholder(self) -> str:
        return f"〖{self.kind} {self.number}〗"

    @property
    def is_image(self) -> bool:
        return self.kind == "Image"


class PasteTable:
    """应用内粘贴块表。多次粘贴各自成块，不合并。"""

    def __init__(self) -> None:
        self._blocks: dict[int, PasteBlock] = {}
        self._next_id = 1
        # PST1：附件块单独存放、单独计数；不参与文本块的展开与清理。
        self._attachments: dict[str, AttachmentBlock] = {}
        self._next_number = {"Image": 1, "File": 1}

    def add_attachment(self, kind: str, path: str) -> AttachmentBlock:
        if kind not in self._next_number:
            raise ValueError(f"未知附件类型 {kind!r}")
        block = AttachmentBlock(kind, self._next_number[kind], str(path))
        self._next_number[kind] += 1
        self._attachments[block.placeholder] = block
        return block

    def attachments_in(self, text: str) -> list[AttachmentBlock]:
        """text 里原样出现的附件占位（按出现顺序去重）；改坏的不算。"""
        found: list[AttachmentBlock] = []
        for match in _ATTACHMENT_RE.finditer(text):
            block = self._attachments.get(match.group(0))
            if block is not None and block not in found:
                found.append(block)
        return found

    def drop_attachments(self, blocks: Iterable[AttachmentBlock]) -> None:
        for block in blocks:
            self._attachments.pop(block.placeholder, None)

    def __len__(self) -> int:
        return len(self._blocks)

    def add(self, text: str) -> PasteBlock:
        block = PasteBlock(self._next_id, paste_line_count(text), text)
        self._blocks[block.block_id] = block
        self._next_id += 1
        return block

    def get(self, block_id: int) -> PasteBlock | None:
        return self._blocks.get(block_id)

    def ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._blocks))

    def _match(self, match: re.Match[str]) -> PasteBlock | None:
        block = self._blocks.get(int(match.group(2)))
        if block is None or block.placeholder != match.group(0):
            return None
        return block

    def referenced(self, text: str) -> list[PasteBlock]:
        """text 里原样出现的占位符对应的块（按出现顺序去重）。"""
        found: list[PasteBlock] = []
        seen: set[int] = set()
        for match in _PLACEHOLDER_RE.finditer(text):
            block = self._match(match)
            if block is None or block.block_id in seen:
                continue
            seen.add(block.block_id)
            found.append(block)
        return found

    def expand(self, text: str) -> str:
        """一遍替换：原样占位符展开回全文，改坏的原样保留。

        单遍 re.sub，全文里即便含有占位符样式的字也不会被二次展开。
        """

        def _repl(match: re.Match[str]) -> str:
            block = self._match(match)
            return match.group(0) if block is None else block.text

        return _PLACEHOLDER_RE.sub(_repl, text)

    def prune(self, keep_texts: Iterable[str]) -> None:
        """只留 keep_texts 里仍被原样引用的块，其余清理。"""
        keep: set[int] = set()
        for text in keep_texts:
            keep.update(block.block_id for block in self.referenced(text))
        for block_id in list(self._blocks):
            if block_id not in keep:
                del self._blocks[block_id]
