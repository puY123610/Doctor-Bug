"""TUIC2：结果包 patch.diff 的只读呈现（diff 补丁块）。

数据契约（写死）：
- 文件头 ``---``/``+++`` 加粗；``@@`` 块头置灰；``+`` 行绿底、``-`` 行红底；
  上下文原样；纯文本原样，不走 Markdown。
- 重命名（``rename from``/``rename to``，或 a/ 与 b/ 路径不同）显示「旧 → 新」。
- 二进制（``Binary files ... differ`` / ``GIT binary patch``）只显示一行
  「二进制文件，不展开」，其后的二进制数据行一律不渲染。
- 超大：超 ``MAX_LINES`` 行或超 ``MAX_BYTES`` 字节时只渲染前 ``KEEP_LINES``
  行，并加一行「已截断，共 N 行」。
- 解析失败：整块回落纯文本，并加一行「diff 解析失败，显示原文」。
- 无色终端：不加任何颜色样式，只靠行首 ``+``/``-`` 区分；着色只用 Text
  样式，记录文本里不写 ANSI 转义。

本模块只读，不改结果包、不调 apply_bundle。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

MAX_LINES = 2000
MAX_BYTES = 200 * 1024
KEEP_LINES = 500
BINARY_TEXT = "二进制文件，不展开"
PARSE_FAILED_TEXT = "diff 解析失败，显示原文"
EMPTY_TEXT = "暂无可展示的改动"
TITLE = "补丁（最近一轮运行，只读）"

COLOR_ADD_BG = "#16361f"
COLOR_DEL_BG = "#3d1b1b"
COLOR_HUNK = "#808080"

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


class DiffParseError(ValueError):
    """patch 文本不符合统一 diff 结构。"""


@dataclass(frozen=True)
class DiffLine:
    kind: str  # header / hunk / add / del / ctx / rename / binary / meta
    text: str


def no_color_requested() -> bool:
    """NO_COLOR 只要设了（非空）就视为无色（https://no-color.org）。"""
    return bool(os.environ.get("NO_COLOR", ""))


def read_patch(bundle: str | Path | None) -> str | None:
    """读结果包的 patch.diff；没有包或读不到返回 None（不编内容）。"""
    if bundle is None:
        return None
    try:
        return (Path(bundle) / "patch.diff").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _strip_prefix(path: str) -> str:
    path = path.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse(text: str) -> list[DiffLine]:
    """把统一 diff 解析成带类型的行。结构不对抛 DiffParseError。"""
    lines = text.splitlines()
    out: list[DiffLine] = []
    in_file = False
    in_hunk = False
    in_binary = False
    old_path: str | None = None
    rename_from: str | None = None
    saw_any = False
    for raw in lines:
        if raw.startswith("diff "):
            in_file, in_hunk, in_binary = True, False, False
            old_path = rename_from = None
            out.append(DiffLine("meta", raw))
            saw_any = True
            continue
        if in_binary:
            continue  # GIT binary patch 的数据行不展开
        if raw.startswith("rename from "):
            rename_from = raw[len("rename from "):].strip()
            in_file = True
            continue
        if raw.startswith("rename to "):
            new = raw[len("rename to "):].strip()
            out.append(DiffLine("rename", f"{rename_from or '?'} → {new}"))
            rename_from = None
            saw_any = True
            continue
        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            out.append(DiffLine("binary", BINARY_TEXT))
            in_binary = raw.startswith("GIT binary patch")
            in_file, in_hunk = False, False
            saw_any = True
            continue
        if raw.startswith("--- ") and not in_hunk:
            out.append(DiffLine("header", raw))
            old_path = raw[4:]
            in_file = True
            saw_any = True
            continue
        if raw.startswith("+++ ") and not in_hunk:
            if old_path is None:
                raise DiffParseError("+++ 之前缺少 --- 文件头")
            out.append(DiffLine("header", raw))
            new_path = raw[4:]
            old_name, new_name = _strip_prefix(old_path), _strip_prefix(new_path)
            if (
                "/dev/null" not in (old_name, new_name)
                and old_name != new_name
            ):
                out.append(DiffLine("rename", f"{old_name} → {new_name}"))
            old_path = None
            continue
        if raw.startswith("@@"):
            if not in_file or not _HUNK_RE.match(raw):
                raise DiffParseError(f"块头不合法：{raw[:40]}")
            in_hunk = True
            out.append(DiffLine("hunk", raw))
            continue
        if in_hunk:
            if raw.startswith("+"):
                out.append(DiffLine("add", raw))
            elif raw.startswith("-"):
                out.append(DiffLine("del", raw))
            elif raw.startswith(" ") or raw == "" or raw.startswith("\\"):
                out.append(DiffLine("ctx", raw))
            else:
                raise DiffParseError(f"块内出现无法识别的行：{raw[:40]}")
            continue
        if in_file:
            # index / new file mode / similarity 等 git 元信息行
            out.append(DiffLine("meta", raw))
            continue
        raise DiffParseError(f"文件头之前出现内容：{raw[:40]}")
    if lines and not saw_any:
        raise DiffParseError("没有任何文件头")
    return out


def _style(kind: str, color: bool) -> str:
    if kind == "header":
        return "bold"  # 加粗不算颜色，无色终端照样可辨
    if not color:
        return ""
    return {
        "hunk": COLOR_HUNK,
        "add": f"on {COLOR_ADD_BG}",
        "del": f"on {COLOR_DEL_BG}",
        "meta": COLOR_HUNK,
    }.get(kind, "")


def render(text: str, *, color: bool = True) -> Text:
    """渲染一个 diff 块（Text 样式，不含 ANSI 转义）。"""
    out = Text()
    out.append(TITLE, style="bold")
    all_lines = text.splitlines()
    total = len(all_lines)
    too_big = total > MAX_LINES or len(text.encode("utf-8")) > MAX_BYTES
    shown_text = "\n".join(all_lines[:KEEP_LINES]) if too_big else text
    try:
        parsed = parse(shown_text)
    except DiffParseError:
        for raw in shown_text.splitlines():
            out.append("\n" + raw)
        out.append("\n" + PARSE_FAILED_TEXT, style="bold")
    else:
        if not parsed:
            out.append("\n" + EMPTY_TEXT)
        for line in parsed:
            out.append("\n")
            out.append(line.text, style=_style(line.kind, color))
    if too_big:
        out.append(f"\n已截断，共 {total} 行", style="bold")
    return out
