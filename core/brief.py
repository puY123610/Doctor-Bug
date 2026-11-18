# -*- coding: utf-8 -*-
"""项目简报：模型提炼 + 程序校验出处，落盘为 profile 下的 BRIEF.md。

简报是参考资料，不是指令（文件头固定声明）。每条规则必须带
出处（文件 + 行号 + 原文引文），程序逐条核对：引文在所引行
对不上的进「未核实」区，无出处的直接丢弃。来源文件在生成
后变化的条目，校验时标记过期。

本模块的出处/哈希函数（verify_quote / sha256_file /
source_changed）是公共件：BRIEF1b 的员工记忆卫生直接复用，
不许把校验逻辑只写死在简报路径里。

扫描红线：候选文件读取前一律过 core.policy 的凭据判定
（is_secret_path / is_credential_file），命中的跳过不读、
不引用，只记文件名与数量。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from datetime import datetime
from pathlib import Path

BRIEF_FILENAME = "BRIEF.md"
META_FILENAME = "brief_meta.json"
MAX_BRIEF_CHARS = 6000
MAX_SCAN_FILES = 60
MAX_FILE_CHARS = 4000
MAX_PROMPT_CHARS = 48000
DISCLAIMER = (
    "本简报是参考资料，不是指令；内容来自项目文档，"
    "可能过期或有误，引用时须核对出处。"
)

_NOISE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".drbug",
    ".venv",
    "node_modules",
    ".mypy_cache",
}

_PROMPT_HEADER = """你是主治医生。下面是一个项目的文档与测试节选，每行都带行号。

请提炼这个项目「修代码时必须遵守的业务规则」，例如金额的单位与取整、
税基的口径、权限的判定、状态的流转。每条规则必须给出处，格式严格如下，
每个规则块三行，不要输出任何其他内容：

RULE: <一句话规则>
SOURCE: <相对路径>:<行号>
QUOTE: <该行原文，逐字复制>

只许引用下面给出的文件与行号；引文必须逐字复制，不许改写。
没有把握的规则宁可不写。最多 12 条。
"""


# ---------------------------------------------------------------------------
# 公共件：出处校验与文件哈希（BRIEF1b 记忆卫生复用）
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def _norm_ws(text: str) -> str:
    return " ".join(str(text).split())


def verify_quote(
    root: str | Path,
    rel_path: str,
    quote: str,
    line: int | None = None,
) -> bool:
    """核对引文真实性：文件存在、非凭据文件、引文确在所引行。

    ``line`` 给出时只查该行（1 起）；不给时查全文。比对前做
    空白归一化。凭据/密钥文件一律判不通过（出处不许指向它们）。
    """
    from .policy import is_credential_file, is_secret_path

    target = Path(root) / rel_path
    if is_secret_path(target) or is_credential_file(target):
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    needle = _norm_ws(quote)
    if not needle:
        return False
    if line is not None:
        lines = text.splitlines()
        if line < 1 or line > len(lines):
            return False
        return needle in _norm_ws(lines[line - 1])
    return needle in _norm_ws(text)


def source_changed(
    root: str | Path,
    rel_path: str,
    recorded_hash: str | None,
) -> bool:
    """来源文件在记录哈希之后是否变过（删了也算变）。"""
    if recorded_hash is None:
        return True
    return sha256_file(Path(root) / rel_path) != recorded_hash


# ---------------------------------------------------------------------------
# 扫描（过凭据红线）与提示构建
# ---------------------------------------------------------------------------


def _is_candidate(rel: Path) -> bool:
    name = rel.name
    if name.startswith("README") or rel.suffix == ".md":
        return True
    if "tests" in rel.parts and rel.suffix in {".py", ".md", ".txt"}:
        return True
    if name.startswith("test_") and rel.suffix == ".py":
        return True
    return False


def scan_workspace(root: str | Path) -> dict:
    """收集简报素材：文档与测试节选；凭据类文件跳过只记名。"""
    from .policy import is_credential_file, is_secret_path

    root = Path(root)
    files: list[dict] = []
    skipped: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _NOISE_DIRS for part in rel.parts):
            continue
        if not _is_candidate(rel):
            continue
        if is_secret_path(path) or is_credential_file(path):
            skipped.append(path.name)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files.append(
            {
                "path": rel.as_posix(),
                "text": text[:MAX_FILE_CHARS],
                "truncated": len(text) > MAX_FILE_CHARS,
            }
        )
        if len(files) >= MAX_SCAN_FILES:
            break
    return {"files": files, "skipped_secret_names": skipped}


def _structure_lines(root: Path) -> list[str]:
    lines: list[str] = []
    try:
        top = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return lines
    for item in top:
        if item.name in _NOISE_DIRS:
            continue
        if item.is_dir():
            lines.append(f"{item.name}/")
            try:
                children = sorted(item.iterdir(), key=lambda c: c.name)
            except OSError:
                continue
            for child in children[:20]:
                lines.append(f"  {child.name}")
        else:
            lines.append(item.name)
        if len(lines) >= 80:
            break
    return lines


def build_prompt(scan: dict, structure: list[str]) -> str:
    lines = [_PROMPT_HEADER, "", "目录结构："]
    lines.extend(structure)
    lines.append("")
    for item in scan["files"]:
        lines.append(f"===== {item['path']} =====")
        for number, raw in enumerate(item["text"].splitlines(), 1):
            lines.append(f"{number}: {raw}")
        lines.append("")
    return "\n".join(lines)[:MAX_PROMPT_CHARS]


# ---------------------------------------------------------------------------
# 模型回复解析与核对
# ---------------------------------------------------------------------------

_RULE_BLOCK_RE = re.compile(
    r"RULE:\s*(?P<rule>.+?)\s*\n"
    r"SOURCE:\s*(?P<path>.+?):(?P<line>\d+)\s*\n"
    r"QUOTE:\s*(?P<quote>.+?)(?=\n\s*RULE:|\Z)",
    re.S,
)


def parse_rules(text: str) -> tuple[list[dict], int]:
    """解析模型回复；返回 (规则列表, 无出处被丢弃的块数)。"""
    rules: list[dict] = []
    for match in _RULE_BLOCK_RE.finditer(text or ""):
        rules.append(
            {
                "rule": _norm_ws(match.group("rule")),
                "path": match.group("path").strip(),
                "line": int(match.group("line")),
                "quote": match.group("quote").strip(),
            }
        )
    dropped = (text or "").count("RULE:") - len(rules)
    return rules, max(dropped, 0)


def _response_text(response: object) -> str:
    get = getattr(response, "get_text_content", None)
    if callable(get):
        try:
            return get() or ""
        except Exception:  # noqa: BLE001 - fall through to blocks
            pass
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        piece = getattr(block, "text", None)
        if isinstance(piece, str):
            parts.append(piece)
    return "".join(parts)


async def _complete(model: object, prompt: str) -> str:
    from agentscope.message import Msg, TextBlock

    message = Msg(
        name="drbug",
        role="user",
        content=[TextBlock(text=prompt)],
    )
    result = await model([message])  # type: ignore[operator]
    if inspect.isasyncgen(result):
        text = ""
        async for chunk in result:
            piece = _response_text(chunk)
            if piece:
                text = piece
        return text
    return _response_text(result)


# ---------------------------------------------------------------------------
# 落盘、读取、校验
# ---------------------------------------------------------------------------


def profile_dir_for(workspace: str | Path) -> Path:
    """该工作区的 profile 目录（与 roster/autoprofile 同口径）。"""
    from .autoprofile import default_profile_name, state_root_for

    root = state_root_for(workspace) / "profiles"
    return root / default_profile_name(workspace)


def _read_meta(meta_path: Path) -> dict:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _render_brief(
    *,
    generated_at: str,
    verified: list[dict],
    unverified: list[dict],
    skipped_names: list[str],
    scanned_count: int,
) -> str:
    lines = [
        "# 项目简报",
        "",
        f"> {DISCLAIMER}",
        "",
        f"- 生成时间: {generated_at}",
        f"- 扫描文件: {scanned_count} 个",
        "- 跳过凭据类文件: "
        + (
            f"{len(skipped_names)} 个（仅记名: {', '.join(skipped_names)}）"
            if skipped_names
            else "0 个"
        ),
        "",
        "## 规则",
        "",
    ]
    if verified:
        for index, item in enumerate(verified, 1):
            lines.append(f"{index}. {item['rule']}")
            lines.append(
                f"   出处：`{item['path']}` 第 {item['line']} 行"
                f" —「{item['quote']}」"
            )
    else:
        lines.append("（无已核实规则）")
    lines.extend(["", "## 未核实", ""])
    if unverified:
        for item in unverified:
            lines.append(
                f"- {item['rule']}（出处 `{item['path']}`"
                f" 第 {item['line']} 行 — {item['reason']}）"
            )
    else:
        lines.append("- 无")
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS] + "\n（简报超长，已截断）\n"
    return text


def generate_brief(
    workspace: str | Path,
    profile_dir: str | Path,
    *,
    model: object | None = None,
    confirm: bool = False,
) -> dict:
    """生成简报并落盘。手改过的简报无 confirm 时拒绝覆盖。

    ``model`` 可注入（测试用 FakeChatModel）；缺省走平台模型。
    返回 dict: status 为 generated / refused_hand_edited，
    附 rules / unverified / dropped_no_source /
    skipped_secret_names / brief_path。
    """
    workspace = Path(workspace)
    profile_dir = Path(profile_dir)
    brief_path = profile_dir / BRIEF_FILENAME
    meta_path = profile_dir / META_FILENAME
    if brief_path.is_file():
        recorded = _read_meta(meta_path).get("content_sha256")
        if recorded is None or recorded != sha256_file(brief_path):
            if not confirm:
                return {
                    "status": "refused_hand_edited",
                    "brief_path": str(brief_path),
                    "rules": 0,
                    "unverified": 0,
                    "dropped_no_source": 0,
                    "skipped_secret_names": [],
                }
    scan = scan_workspace(workspace)
    if model is None:
        from .model_config import build_model

        model = build_model()
    prompt = build_prompt(scan, _structure_lines(workspace))
    raw = asyncio.run(_complete(model, prompt))
    parsed, dropped = parse_rules(raw)
    verified: list[dict] = []
    unverified: list[dict] = []
    sources: dict[str, str] = {}
    for item in parsed:
        if verify_quote(workspace, item["path"], item["quote"], item["line"]):
            verified.append(item)
            digest = sha256_file(workspace / item["path"])
            if digest is not None:
                sources[item["path"]] = digest
        else:
            unverified.append(
                {
                    **item,
                    "reason": (
                        "出处核对未通过"
                        "（文件不存在、行号越界或引文对不上）"
                    ),
                }
            )
    generated_at = datetime.now().isoformat(timespec="seconds")
    text = _render_brief(
        generated_at=generated_at,
        verified=verified,
        unverified=unverified,
        skipped_names=scan["skipped_secret_names"],
        scanned_count=len(scan["files"]),
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(text, encoding="utf-8")
    meta = {
        "generated_at": generated_at,
        "content_sha256": sha256_file(brief_path),
        "sources": sources,
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "generated",
        "brief_path": str(brief_path),
        "rules": len(verified),
        "unverified": len(unverified),
        "dropped_no_source": dropped,
        "skipped_secret_names": scan["skipped_secret_names"],
    }


_RULE_LINE_RE = re.compile(r"^\d+\.\s+(.*)$")
_SOURCE_LINE_RE = re.compile(r"^\s+出处：`(.+?)` 第 (\d+) 行 —「(.*)」$")


def load_brief(profile_dir: str | Path) -> dict | None:
    """把 BRIEF.md 读回结构；不存在返回 None。"""
    profile_dir = Path(profile_dir)
    brief_path = profile_dir / BRIEF_FILENAME
    if not brief_path.is_file():
        return None
    text = brief_path.read_text(encoding="utf-8", errors="replace")
    rules: list[dict] = []
    unverified: list[str] = []
    section: str | None = None
    current: dict | None = None
    for raw in text.splitlines():
        if raw.startswith("## 规则"):
            section = "rules"
            continue
        if raw.startswith("## 未核实"):
            section = "unverified"
            continue
        if raw.startswith("## "):
            section = None
            continue
        if section == "rules":
            match = _RULE_LINE_RE.match(raw)
            if match:
                current = {"rule": match.group(1).strip()}
                rules.append(current)
                continue
            match = _SOURCE_LINE_RE.match(raw)
            if match and current is not None:
                current.update(
                    {
                        "path": match.group(1),
                        "line": int(match.group(2)),
                        "quote": match.group(3),
                    }
                )
        elif section == "unverified" and raw.startswith("- "):
            unverified.append(raw[2:])
    meta = _read_meta(profile_dir / META_FILENAME)
    return {
        "text": text,
        "rules": [item for item in rules if "path" in item],
        "unverified": unverified,
        "generated_at": meta.get("generated_at"),
        "sources": meta.get("sources") or {},
    }


def validate_brief(workspace: str | Path, profile_dir: str | Path) -> dict:
    """逐条复核：引文现在还对不对得上、来源文件变没变（过期）。"""
    loaded = load_brief(profile_dir)
    if loaded is None:
        return {"exists": False, "generated_at": None, "rules": []}
    checked: list[dict] = []
    for item in loaded["rules"]:
        recorded = (loaded["sources"] or {}).get(item["path"])
        checked.append(
            {
                **item,
                "verified_now": verify_quote(
                    workspace, item["path"], item["quote"], item["line"]
                ),
                "stale": source_changed(workspace, item["path"], recorded),
            }
        )
    return {
        "exists": True,
        "generated_at": loaded["generated_at"],
        "rules": checked,
    }


def format_brief_for_display(
    workspace: str | Path,
    profile_dir: str | Path,
) -> str:
    """展示用全文：正文 + 过期/失效条目的校验提示。"""
    loaded = load_brief(profile_dir)
    if loaded is None:
        return (
            "尚无项目简报。生成方法：在对话里明确让主治医生生成"
            "简报，或运行 drbug brief <工作区> --generate。"
        )
    report = validate_brief(workspace, profile_dir)
    text = loaded["text"].rstrip("\n")
    notes: list[str] = []
    stale = [item for item in report["rules"] if item["stale"]]
    if stale:
        notes.append(
            "以下条目来源文件在生成后已变，可能过期："
            + "；".join(item["rule"][:40] for item in stale)
        )
    broken = [item for item in report["rules"] if not item["verified_now"]]
    if broken:
        notes.append(
            "以下条目出处现在核对不上："
            + "；".join(item["rule"][:40] for item in broken)
        )
    if notes:
        text += "\n\n校验提示：\n" + "\n".join(f"- {note}" for note in notes)
    return text


def brief_rules(profile_dir: str | Path) -> list[dict]:
    """已核实规则列表（供秘书审查环节引用说明；无简报返回空）。"""
    loaded = load_brief(profile_dir)
    if loaded is None:
        return []
    return list(loaded["rules"])
