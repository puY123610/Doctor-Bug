"""JS/TS 结构清单：与 Python profile inventory 同形的
``{stem: {"path": ..., "blurb": ...}}``。

优先 tree-sitter 真解析（需 tree_sitter_javascript /
tree_sitter_typescript 语法包）；语法包缺失或解析失败时
退化为正则扫描，并在返回的 notice 里如实说明——清单可以
简版，来源不许含糊。
"""

from __future__ import annotations

import re
from pathlib import Path

_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_SKIP_DIRS = {"node_modules", "dist", "build", ".git", "coverage"}
_TEST_MARKERS = (".test.", ".spec.", "__tests__")

_FUNC_RE = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
)
_CLASS_RE = re.compile(r"(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")
_ARROW_RE = re.compile(
    r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("
)
_DOC_RE = re.compile(r"/\*\*(.+?)\*/", re.DOTALL)


def _grammar_for(suffix: str):
    try:
        if suffix in (".ts", ".tsx"):
            import tree_sitter_typescript as ts_mod

            if suffix == ".ts":
                return ts_mod.language_typescript()
            return ts_mod.language_tsx()
        import tree_sitter_javascript as js_mod

        return js_mod.language()
    except Exception:  # noqa: BLE001 - 缺包即退化，调用方看 notice
        return None


def _ts_extract(
    text: str, language
) -> tuple[list[str], list[str]] | None:
    try:
        from tree_sitter import Language, Parser

        parser = Parser(Language(language))
        tree = parser.parse(text.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    data = text.encode("utf-8")
    funcs: list[str] = []
    classes: list[str] = []

    def name_of(node) -> str:
        name = node.child_by_field_name("name")
        if name is None:
            return ""
        return data[name.start_byte : name.end_byte].decode(
            "utf-8", "replace"
        )

    def visit(node) -> None:
        if node.type in ("function_declaration", "generator_function_declaration"):
            found = name_of(node)
            if found:
                funcs.append(found)
        elif node.type == "class_declaration":
            found = name_of(node)
            if found:
                classes.append(found)
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return funcs, classes


def _regex_extract(text: str) -> tuple[list[str], list[str]]:
    funcs = _FUNC_RE.findall(text) + _ARROW_RE.findall(text)
    classes = _CLASS_RE.findall(text)
    return funcs, classes


def _blurb(text: str, funcs: list[str], classes: list[str]) -> str:
    parts: list[str] = []
    doc = _DOC_RE.search(text)
    if doc:
        first = " ".join(doc.group(1).split()).strip("* ").strip()
        if first:
            parts.append(first)
    if funcs:
        parts.append("函数：" + "、".join(funcs[:6]))
    if classes:
        parts.append("类：" + "、".join(classes[:4]))
    return " · ".join(parts)


def _stem_of(path: Path) -> str:
    stem = path.stem
    for suffix in (".test", ".spec"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


def scan_js_ts_inventory(repo: str | Path) -> dict:
    """扫描仓库的 JS/TS 源码，返回 {"modules": ..., "notice": ...}。"""

    repo = Path(repo)
    modules: dict[str, dict[str, str]] = {}
    used_ts = False
    used_fallback = False
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if any(marker in path.name for marker in _TEST_MARKERS):
            continue
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        language = _grammar_for(path.suffix)
        extracted = (
            _ts_extract(text, language) if language is not None else None
        )
        if extracted is not None:
            used_ts = True
            funcs, classes = extracted
        else:
            used_fallback = True
            funcs, classes = _regex_extract(text)
        stem = _stem_of(path)
        if stem in modules:
            continue
        modules[stem] = {"path": rel, "blurb": _blurb(text, funcs, classes)}
    notice = None
    if modules and not used_ts:
        notice = (
            "未装 tree-sitter 语法包（tree_sitter_javascript / "
            "tree_sitter_typescript），结构清单为正则简版"
        )
    elif used_fallback:
        notice = "部分文件未用上 tree-sitter（语法包缺失或解析失败），条目含正则简版"
    return {"modules": modules, "notice": notice}
