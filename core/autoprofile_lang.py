"""F2: JS/TS 与 Go 仓库的自动建档（与 Python 链同形的 profile）。

``core.autoprofile.build_profile`` 只懂 pytest 仓；本模块在它
发现不了 Python 测试时接手，产出同样形状的档案：dispatch
table、vault（manifest 带 check_command/parser，PERM3 契约）、
profile.json、PROFILE.md 与员工档案目录，runner/dispatcher
无需改动即可消费。

信号强度如实声明：派单只用命名信号（测试文件 stem ↔ 源码
模块 stem；Go 用包目录名），不解析失败堆栈帧，node_overrides
恒为空——证据不足时派单回落 generalist，与 Python 链的回落
语义一致。JS 证据运行需要仓库已装依赖（node_modules 存在，
或能 npm install）；Go 证据运行需要模块依赖已在本地缓存
（GOPROXY=off，不联网下载）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from acceptor.evidence import module_from_node_id
from acceptor.parsers import get_parser

from .agent_archive import ensure_archives
from .autoprofile import ProfileResult, _sanitise_identifier
from .js_inventory import scan_js_ts_inventory

GENERATOR = "core.autoprofile_lang"
GENERATOR_VERSION = "F2-1"

_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "coverage", ".drbug", "vendor",
}
_JS_TEST_RE = re.compile(r"\.(test|spec)\.[tj]sx?$")
_JS_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_GO_TEST_SUFFIX = "_test.go"


def detect_language(repo: Path) -> str | None:
    """按测试文件存在性判定语言；混合仓选测试文件多的一方。"""

    go_tests = _find_files(repo, lambda p: p.name.endswith(_GO_TEST_SUFFIX))
    js_tests = _find_files(repo, lambda p: bool(_JS_TEST_RE.search(p.name)))
    if go_tests and len(go_tests) >= len(js_tests):
        return "go"
    if js_tests:
        return "javascript"
    if (repo / "go.mod").is_file():
        return "go"
    if (repo / "package.json").is_file():
        return "javascript"
    return None


def _find_files(repo: Path, predicate) -> list[str]:
    found: list[str] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if predicate(path):
            found.append(path.relative_to(repo).as_posix())
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _augmented_env() -> dict:
    env = dict(os.environ)
    candidates = ["/opt/homebrew/bin", "/usr/local/bin"]
    fnm = Path.home() / ".local" / "share" / "fnm" / "node-versions"
    if fnm.is_dir():
        for install in sorted(fnm.glob("*/installation/bin")):
            candidates.append(str(install))
    env["PATH"] = ":".join(candidates + [env.get("PATH", "")])
    return env


def _run_on_copy(
    repo: Path,
    command: list[str],
    parser_name: str,
    *,
    language: str,
    ambiguities: list[dict],
) -> dict:
    """在仓库副本上跑检查命令并解析；失败模式与 Python 链同调。"""

    empty = {
        "test_total": 0,
        "passed_count": 0,
        "failed_count": 0,
        "failed_nodes": [],
        "returncode": None,
    }
    tmp = Path(tempfile.mkdtemp(prefix="drbug-lang-evidence-"))
    copy_root = tmp / "repo"
    try:
        shutil.copytree(
            repo,
            copy_root,
            ignore=shutil.ignore_patterns(
                ".git", "node_modules", "dist", "build", "coverage",
                ".drbug", "__pycache__",
            ),
        )
        env = _augmented_env()
        if language == "javascript":
            source_modules = repo / "node_modules"
            if source_modules.exists():
                os.symlink(
                    source_modules.resolve(), copy_root / "node_modules"
                )
            else:
                install = subprocess.run(
                    ["npm", "install", "--ignore-scripts",
                     "--no-audit", "--no-fund"],
                    cwd=copy_root, env=env, capture_output=True,
                    text=True, timeout=300,
                )
                if install.returncode != 0 or not (
                    copy_root / "node_modules"
                ).exists():
                    ambiguities.append(
                        {
                            "kind": "lang_deps_missing",
                            "detail": (
                                "JS 证据运行未完成：仓库没有 node_modules "
                                "且 npm install 未成功；派单表只有静态"
                                "分析结果，判卷仍可按冻结命令执行"
                                "（运行环境需自行备妥依赖）。"
                            ),
                        }
                    )
                    return empty
        if language == "go":
            env["GOPROXY"] = "off"
            env["GOFLAGS"] = "-mod=mod"
        try:
            proc = subprocess.run(
                command,
                cwd=copy_root, env=env, capture_output=True,
                text=True, timeout=900, errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            ambiguities.append(
                {
                    "kind": "lang_evidence_failed",
                    "detail": f"证据运行未完成：{type(exc).__name__}: {exc}",
                }
            )
            return empty
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        # 与判卷器同一处理：工具链在管道里也吐 ANSI 颜色码，
        # 不剥掉汇总行与失败行都匹配不上（F8 同款）。
        output = re.sub(r"\x1b\[[0-9;:]*[A-Za-z]", "", output)
        parsed = get_parser(parser_name)(output, proc.returncode)
        return {
            "test_total": parsed.passed_count + parsed.failed_count,
            "passed_count": parsed.passed_count,
            "failed_count": parsed.failed_count,
            "failed_nodes": sorted(parsed.failed_tests),
            "returncode": proc.returncode,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _js_plan(repo: Path, ambiguities: list[dict]) -> dict:
    test_files = _find_files(
        repo, lambda p: bool(_JS_TEST_RE.search(p.name))
    )
    scan = scan_js_ts_inventory(repo)
    if scan.get("notice"):
        ambiguities.append(
            {"kind": "js_inventory_notice", "detail": str(scan["notice"])}
        )
    modules = [
        {
            "stem": stem,
            "files": [entry["path"]],
            "blurb": entry.get("blurb", ""),
            "path": entry["path"],
        }
        for stem, entry in sorted(scan["modules"].items())
    ]
    package_json = {}
    try:
        package_json = json.loads(
            (repo / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        pass
    scripts = package_json.get("scripts") or {}
    test_script = str(scripts.get("test") or "")
    dev = {
        **(package_json.get("devDependencies") or {}),
        **(package_json.get("dependencies") or {}),
    }
    if "vitest" in dev or "vitest" in test_script:
        command, parser_name = ["npx", "vitest", "run"], "vitest"
    elif "jest" in dev or "jest" in test_script:
        command, parser_name = ["npx", "jest"], "jest"
    elif test_script:
        command, parser_name = ["npm", "test"], "vitest"
        ambiguities.append(
            {
                "kind": "js_runner_guessed",
                "detail": (
                    "未识别出 vitest/jest，冻结命令用 npm test、解析器"
                    "按 jest/vitest 家族处理；若测试脚本不是这两家，"
                    "判卷计数可能不准。"
                ),
            }
        )
    else:
        command, parser_name = ["npx", "vitest", "run"], "vitest"
        ambiguities.append(
            {
                "kind": "js_runner_guessed",
                "detail": "package.json 无 test 脚本且未声明 vitest/jest，"
                          "默认按 vitest 冻结。",
            }
        )
    config_files = [
        rel
        for rel in _find_files(
            repo,
            lambda p: p.name == "package.json"
            or p.name.startswith("tsconfig")
            or p.name.startswith("vitest.config")
            or p.name.startswith("vitest.workspace")
            or p.name.startswith("jest.config")
            or p.name.startswith("babel.config"),
        )
        if "/" not in rel  # 只冻根目录配置，避免误扫子包
    ]
    return {
        "test_files": test_files,
        "config_files": config_files,
        "modules": modules,
        "command": command,
        "parser": parser_name,
        "test_module_of": {
            rel: module_from_node_id(f"{rel}::x") for rel in test_files
        },
    }


_GO_PACKAGE_RE = re.compile(r"^\s*package\s+(\w+)", re.MULTILINE)
_GO_DOC_RE = re.compile(r"^((?://[^\n]*\n)+)package\s", re.MULTILINE)


def _go_plan(repo: Path, ambiguities: list[dict]) -> dict:
    test_files = _find_files(
        repo, lambda p: p.name.endswith(_GO_TEST_SUFFIX)
    )
    by_dir: dict[str, list[str]] = {}
    for rel in _find_files(repo, lambda p: p.suffix == ".go"):
        if rel.endswith(_GO_TEST_SUFFIX):
            continue
        by_dir.setdefault(str(Path(rel).parent), []).append(rel)
    modules = []
    for directory, files in sorted(by_dir.items()):
        files = sorted(files)
        stem = (
            Path(directory).name
            if directory not in ("", ".")
            else _go_root_stem(repo)
        )
        blurb = ""
        for rel in files:
            text = (repo / rel).read_text(
                encoding="utf-8", errors="replace"
            )
            doc = _GO_DOC_RE.search(text)
            if doc:
                blurb = " ".join(
                    line.lstrip("/ ").strip()
                    for line in doc.group(1).splitlines()
                )[:200]
                break
        modules.append(
            {
                "stem": stem,
                "files": files,
                "blurb": blurb,
                "path": files[0],
            }
        )
    config_files = [
        rel for rel in ("go.mod", "go.sum") if (repo / rel).is_file()
    ]
    test_module_of = {}
    for rel in test_files:
        parent = str(Path(rel).parent)
        test_module_of[rel] = (
            Path(parent).name
            if parent not in ("", ".")
            else _go_root_stem(repo)
        )
    return {
        "test_files": test_files,
        "config_files": config_files,
        "modules": modules,
        "command": ["go", "test", "-count=1", "-v", "./..."],
        "parser": "go",
        "test_module_of": test_module_of,
    }


def _go_root_stem(repo: Path) -> str:
    try:
        text = (repo / "go.mod").read_text(encoding="utf-8")
        match = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
        if match:
            return match.group(1).rstrip("/").split("/")[-1]
    except OSError:
        pass
    return repo.name


def _vault_package(modules: list[dict], test_files: list[str]) -> str:
    counter: Counter = Counter()
    for module in modules:
        for rel in module["files"]:
            parts = Path(rel).parts
            if len(parts) > 1:
                counter[parts[0]] += 1
    if counter:
        return counter.most_common(1)[0][0]
    for rel in test_files:
        parts = Path(rel).parts
        if len(parts) > 1:
            return parts[0]
    return "src"


def _roster_package_of(modules: list[dict]) -> str:
    """全体源码文件的最长公共目录前缀；没有就用 "."（仓库根）。"""

    parents: list[str] = []
    for module in modules:
        for rel in module["files"]:
            parents.append(str(Path(rel).parent))
    if not parents:
        return "."
    common = parents[0]
    for parent in parents[1:]:
        while common and not (
            parent == common or parent.startswith(common + "/")
        ):
            common = common.rsplit("/", 1)[0] if "/" in common else ""
        if not common:
            break
    return common if common not in ("", ".") else "."


def _build_lang_table(
    modules: list[dict],
    test_files: list[str],
    test_module_of: dict[str, str],
    vault_package: str,
    roster_package: str,
) -> tuple[dict, dict]:
    specialists: list[dict] = []
    roster_specialists: dict[str, dict] = {}
    inventory: dict[str, dict[str, str]] = {}
    used_ids: set[str] = set()
    all_files: list[str] = []
    for module in modules:
        base = _sanitise_identifier(module["stem"]) or "module"
        specialist_id = f"{base}-specialist"
        suffix = 2
        while specialist_id in used_ids:
            specialist_id = f"{base}{suffix}-specialist"
            suffix += 1
        used_ids.add(specialist_id)
        module["specialist_id"] = specialist_id
        files = list(module["files"])
        all_files.extend(files)
        blurb = module.get("blurb", "")
        specialists.append(
            {
                "id": specialist_id,
                "name": f"{module['stem']} 专家",
                "blurb": blurb,
                "files": files,
                "scope": f"{'、'.join(files)}：{blurb}" if blurb else "、".join(files),
            }
        )
        roster_specialists[specialist_id] = {
            # roster_from_table 要求 blurb 非空：无文档注释的模块
            # 用中性说明兜底（与 Python 链 _neutral_blurb 同义）。
            "blurb": blurb or f"{module['stem']} 模块的源码维护。",
            "files": files,
            "specialist": True,
        }
        inventory.setdefault(
            module["stem"],
            {"path": module["path"], "blurb": blurb},
        )
    specialists.append(
        {
            "id": "generalist",
            "name": "通用修理工",
            "blurb": "你是 generalist（通用修理工），在没有明确归因或需要跨模块兜底时做最小修复。",
            "files": sorted(set(all_files)),
            "scope": "无专属专家或跨模块无明确归因时的兜底修复",
        }
    )
    roster_specialists["generalist"] = {
        "blurb": specialists[-1]["blurb"],
        "files": sorted(set(all_files)),
        "specialist": False,
    }
    by_stem = {m["stem"]: m["specialist_id"] for m in modules}
    module_defaults: dict[str, list[str]] = {}
    for rel in test_files:
        stem = test_module_of.get(rel, "")
        if stem in by_stem and stem not in module_defaults:
            module_defaults[stem] = [by_stem[stem]]
    table = {
        "version": 1,
        "specialists": specialists,
        "node_overrides": {},
        "module_defaults": module_defaults,
        "fallback": "generalist",
        "roster": {
            "package": roster_package,
            "specialists": roster_specialists,
            "symptoms": "",
        },
    }
    return table, inventory


def _build_lang_vault(
    repo: Path,
    vault_dir: Path,
    package: str,
    test_files: list[str],
    config_files: list[str],
    command: list[str],
    parser_name: str,
) -> dict:
    get_parser(parser_name)  # 未知解析器在冻结时就拒绝
    if vault_dir.exists():
        shutil.rmtree(vault_dir)
    vault_dir.mkdir(parents=True)
    files: dict[str, str] = {}
    for relative in sorted(set(test_files) | set(config_files)):
        source = repo / relative
        if not source.is_file():
            continue
        target = vault_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files[relative] = _sha256_file(target)
    manifest = {
        "files": files,
        "package": package,
        "packages": [package],
        "source": str(repo.resolve()),
        "check_command": list(command),
        "parser": parser_name,
        "kind": "tests",
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
    }
    (vault_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_profile_lang(
    repo_path: str | Path,
    *,
    profile_dir: Path,
    profile_name: str,
    origin: str = "tests",
    language: str,
) -> ProfileResult:
    """为 JS/TS 或 Go 仓库写出与 Python 链同形的 profile。"""

    repo = Path(repo_path).expanduser().resolve()
    ambiguities: list[dict] = [
        {
            "kind": "lang_profile",
            "detail": (
                f"F2 多语言建档（{language}）：派单只用命名信号，"
                "未解析失败堆栈帧，node_overrides 为空。"
            ),
        }
    ]
    plan = (
        _js_plan(repo, ambiguities)
        if language == "javascript"
        else _go_plan(repo, ambiguities)
    )
    if not plan["test_files"]:
        raise FileNotFoundError(
            f"no test files found in {repo} for language {language}"
        )
    if not plan["modules"]:
        raise ValueError(f"no source modules found in {repo}")
    evidence = _run_on_copy(
        repo,
        plan["command"],
        plan["parser"],
        language=language,
        ambiguities=ambiguities,
    )
    if evidence["test_total"] == 0:
        ambiguities.append(
            {
                "kind": "lang_no_tests",
                "detail": (
                    "副本上检查命令没有收集到任何测试"
                    f"（exit={evidence.get('returncode')}）；"
                    "派单表只有静态分析结果。"
                ),
            }
        )
    vault_package = _vault_package(plan["modules"], plan["test_files"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = profile_dir / "vault"
    manifest = _build_lang_vault(
        repo,
        vault_dir,
        vault_package,
        plan["test_files"],
        plan["config_files"],
        plan["command"],
        plan["parser"],
    )
    table, inventory = _build_lang_table(
        plan["modules"],
        plan["test_files"],
        plan["test_module_of"],
        vault_package,
        _roster_package_of(plan["modules"]),
    )
    from .roster import apply_overlay

    ambiguities.extend(apply_overlay(profile_dir, table))
    ambiguities.sort(
        key=lambda item: (item.get("kind", ""), item.get("detail", ""))
    )
    metadata = {
        "name": profile_name,
        "source_repo": str(repo),
        "packages": [vault_package],
        "vault_package": vault_package,
        "test_files": list(plan["test_files"]),
        "config_files": list(plan["config_files"]),
        "test_total": evidence["test_total"],
        "passed_count": evidence["passed_count"],
        "failed_count": evidence["failed_count"],
        "failed_nodes": list(evidence["failed_nodes"]),
        "ambiguities": [dict(item) for item in ambiguities],
        "specialists": [item["id"] for item in table["specialists"]],
        "inventory": inventory,
        "origin": origin,
        "language": language,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "profile_version": 1,
        "dispatch_table": "dispatch_table.json",
        "vault": "vault",
    }
    # VER1：重建档走版本入口。重建优先、不钉版；只给真有变化的员工记新版。
    from .agent_versions import begin as _begin_versions

    versioning = _begin_versions(profile_dir)
    dispatch_table_path = profile_dir / "dispatch_table.json"
    dispatch_table_path.write_text(
        json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    profile_json_path = profile_dir / "profile.json"
    profile_json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    result = ProfileResult(
        name=profile_name,
        repo_path=repo,
        profile_dir=profile_dir,
        dispatch_table_path=dispatch_table_path,
        vault_dir=vault_dir,
        profile_json_path=profile_json_path,
        markdown_path=profile_dir / "PROFILE.md",
        packages=[vault_package],
        test_total=evidence["test_total"],
        passed_count=evidence["passed_count"],
        failed_count=evidence["failed_count"],
        ambiguities=[dict(item) for item in ambiguities],
        dispatch_table=table,
        metadata=metadata,
    )
    lines = [
        f"# Profile: {profile_name}",
        "",
        f"- 语言: {language}（F2 多语言建档）",
        f"- 源仓库: {repo}",
        f"- 测试: 共 {evidence['test_total']}，"
        f"通过 {evidence['passed_count']}，失败 {evidence['failed_count']}",
        f"- 判卷命令: {' '.join(plan['command'])}（解析器 {plan['parser']}）",
        f"- 判卷包: {manifest.get('package')}",
        "",
        "## 专家",
        "",
    ]
    for item in table["specialists"]:
        lines.append(
            f"- {item['id']}：{'、'.join(item['files'])}"
        )
    if ambiguities:
        lines += ["", "## 存疑与边界", ""]
        for item in ambiguities:
            lines.append(f"- [{item.get('kind', '')}] {item.get('detail', '')}")
    result.markdown_path.write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    ensure_archives(profile_dir, table.get("specialists") or [])
    versioning.commit("重建档带来的定义变化（按仓库重新生成派单表）", kind="rebuild")
    return result
