# -*- coding: utf-8 -*-
"""秘书审 diff：纯程序审查，零模型调用。

输入是本次运行的改动文件清单（与运行基线哈希比对得出）、
派单辖区并集、围栏拒绝记录与项目简报规则；输出结构化结论
{verdict: 通过/否决， reasons[], changed_files[],
brief_citations[]}。每条否决理由必须落到具体文件与实测
数字，不许出现无凭据的笼统否决。

否决边界：否决只拦汇报并升级老板，不改判卷结论与 outcome，
不改变 adopt 语义。返工上限 MAX_REWORK_ROUNDS 写死为 1。
改动规模阈值 MAX_CHANGED_FILES = 12：本产品已验证的正常
修复（演示仓、GEN-VAL 双仓）单次改动 ≤3 个文件，阈值留
4 倍余量，超过即视为异常大改动，交老板裁决。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

MAX_REWORK_ROUNDS = 1
MAX_CHANGED_FILES = 12

# 与 core.runner._COPY_IGNORE 同口径：工作副本的噪声条目
# 不计入改动清单。
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".pytest_cache", ".drbug", "*.pyc"
)


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}

    def walk(directory: Path) -> None:
        children = sorted(directory.iterdir(), key=lambda item: item.name)
        ignored = _COPY_IGNORE(
            str(directory), [item.name for item in children]
        )
        skipped = set(ignored) if ignored else set()
        for child in children:
            if child.name in skipped:
                continue
            if child.is_dir():
                walk(child)
            elif child.is_file():
                digest = hashlib.sha256()
                with child.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        digest.update(chunk)
                hashes[child.relative_to(root).as_posix()] = (
                    digest.hexdigest()
                )

    walk(root)
    return hashes


def changed_files_vs_baseline(
    root: str | Path, baseline: dict[str, str]
) -> list[str]:
    """工作副本相对运行基线的改动文件（新增/修改/删除），排序。"""
    current = _tree_hashes(Path(root))
    changed = [
        path
        for path, digest in current.items()
        if baseline.get(path) != digest
    ]
    changed.extend(path for path in baseline if path not in current)
    return sorted(changed)


def review_patch(
    *,
    changed: list[str],
    scope_files: set[str] | None,
    scope_denials: list[dict],
    brief_rules: list[dict],
) -> dict:
    """程序化审查一份补丁。scope_files 为 None 表示无辖区表可核。"""
    from .policy import is_test_path

    changed = sorted(changed)
    reasons: list[dict] = []
    for path in changed:
        if is_test_path(Path(path)):
            reasons.append(
                {
                    "check": "test_file",
                    "file": path,
                    "detail": f"补丁触碰测试文件 {path}",
                }
            )
    if scope_files is not None:
        allowed = set(scope_files)
        for path in changed:
            if path not in allowed:
                reasons.append(
                    {
                        "check": "out_of_scope",
                        "file": path,
                        "detail": (
                            f"改动文件 {path} 超出本轮派单辖区并集"
                        ),
                    }
                )
    for denial in scope_denials or []:
        reasons.append(
            {
                "check": "scope_denial",
                "file": denial.get("path"),
                "detail": (
                    f"第 {denial.get('round', '?')} 轮 "
                    f"{denial.get('actor_id', '?')} 撞围栏被拒: "
                    f"{denial.get('path')} — {denial.get('reason', '')}"
                ),
            }
        )
    if len(changed) > MAX_CHANGED_FILES:
        reasons.append(
            {
                "check": "too_large",
                "file": None,
                "detail": (
                    f"改动文件共 {len(changed)} 个，"
                    f"超过审查阈值 {MAX_CHANGED_FILES} 个"
                ),
            }
        )
    changed_set = set(changed)
    citations = [
        {
            "rule": item.get("rule", ""),
            "path": item.get("path", ""),
            "line": item.get("line"),
        }
        for item in brief_rules or []
        if item.get("path") in changed_set
    ]
    return {
        "verdict": "否决" if reasons else "通过",
        "reasons": reasons,
        "changed_files": changed,
        "brief_citations": citations,
    }
