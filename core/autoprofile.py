"""GEN2: turn an arbitrary pytest repository into a cifix profile.

The profiler is deliberately offline and model-free.  It discovers packages
and tests statically, runs the suite once on a private copy, merges three
independent mapping signals (AST imports, pytest tracebacks, naming
convention), and writes the same four artefacts a hand-written profile uses.
Node overrides are traceback-first: only package frames from the failing
node are dispatched; imports are a fallback for failures with no frame.

GEN5 narrows both channels at the package root:

* traceback: a node whose package frames contain any non-``__init__`` frame
  loses all ``__init__`` frames before dispatch - the package root only
  forwards the call, it is not the root cause.
* import: ``from <pkg> import <name>`` is resolved through the AST of the
  package ``__init__``'s re-export statements to the submodule that really
  defines ``<name>``.  An unresolved name is recorded under ``ambiguities``
  and contributes no module at all; it never fans out to the whole package.

    profiles/<name>/dispatch_table.json
    profiles/<name>/vault/              (acceptor.freeze + extra test config)
    profiles/<name>/profile.json
    profiles/<name>/PROFILE.md

Only the standard library and the project's own acceptor helpers are used.
The generated dispatch table is deterministic: every list and key is sorted,
and no timestamp is written into it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from acceptor.evidence import evidence_from_failed
from acceptor.frozen import MANIFEST_NAME, freeze, judging_surface
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROFILES_ROOT = PLATFORM_ROOT / "profiles"
DEFAULT_OUT_ROOT = "profiles"

#: Environment variable set by ``drbug``; its TUI passes
#: ``<workspace_root>/profiles`` as the profile output root, which must be
#: redirected to the per-project state directory for external projects.
_CHAT_WORKSPACE_ENV = "DRBUG_CHAT_WORKSPACE"


def state_root_for(workspace: str | Path) -> Path:
    """Return the state directory for *workspace*.

    Workspaces under the installation directory (the platform tree itself
    and the test fixtures under ``tests/fixtures``) keep writing to the
    platform tree.  Any external project writes its own ``<project>/.drbug/``
    directory instead of polluting the installation.
    """
    resolved = Path(workspace).expanduser().resolve()
    if resolved == PLATFORM_ROOT or PLATFORM_ROOT in resolved.parents:
        return PLATFORM_ROOT
    return resolved / ".drbug"


def work_root_for(workspace: str | Path, runs_root: str | Path) -> Path:
    """Pick a private work-copy root that is never inside the workspace.

    ``run_arm`` refuses a ``work_root`` nested in ``workspace_src`` because the
    copy would recurse into itself.  For external projects the state directory
    *is* inside the project, so transient work copies go to the user-level
    ``~/.drbug/work`` while records/bundles stay in ``<project>/.drbug``.
    """
    runs = Path(runs_root).expanduser()
    candidate = runs / "work"
    workspace_resolved = Path(workspace).expanduser().resolve()
    try:
        candidate_resolved = candidate.resolve()
    except OSError:
        candidate_resolved = candidate
    if (
        candidate_resolved == workspace_resolved
        or workspace_resolved in candidate_resolved.parents
    ):
        home = os.environ.get("DRBUG_HOME", "").strip()
        base = Path(home).expanduser() if home else Path.home() / ".drbug"
        fallback = base / "work"
        try:
            fallback_resolved = fallback.resolve()
        except OSError:
            fallback_resolved = fallback
        if (
            fallback_resolved == workspace_resolved
            or workspace_resolved in fallback_resolved.parents
        ):
            # Running drbug on the home directory itself: keep the copy in
            # the system temp area so it is never nested inside the workspace.
            return Path(tempfile.mkdtemp(prefix="drbug-work-"))
        return fallback
    return candidate
GENERATOR = "core.autoprofile"
GENERATOR_VERSION = "GEN2"
PROFILE_VERSION = 1
PYTEST_TIMEOUT_SECONDS = 300
_FALLBACK_ID = "generalist"

_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "site-packages",
    "venv",
}
_LAYOUT_DIR_NAMES = {"lib", "python", "src"}

_COPY_IGNORE = shutil.ignore_patterns(
    ".drbug",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "venv",
)

_SUMMARY_LINE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|RERUN)\s+(.+)$"
)
_TRACEBACK_FRAME = re.compile(r"^(\S[^:]*\.py):(\d+): (?:in .+|.*)$")
_FAILURE_BLOCK = re.compile(r"^_+ (.+?) _+$")


@dataclass(frozen=True)
class SourceModule:
    """One Python source module inside a detected package."""

    path: str
    stem: str
    package: str
    dotted: str


@dataclass
class ProfileResult:
    """Everything the caller needs to locate and inspect a generated profile."""

    name: str
    repo_path: Path
    profile_dir: Path
    dispatch_table_path: Path
    vault_dir: Path
    profile_json_path: Path
    markdown_path: Path
    packages: list[str] = field(default_factory=list)
    test_total: int = 0
    passed_count: int = 0
    failed_count: int = 0
    ambiguities: list[dict] = field(default_factory=list)
    dispatch_table: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def source_repo(self) -> Path:
        """Alias used by reports that call the input the source repository."""
        return self.repo_path

    @property
    def table(self) -> dict:
        """Alias for the generated dispatch table."""
        return self.dispatch_table

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "repo_path": str(self.repo_path),
            "profile_dir": str(self.profile_dir),
            "dispatch_table_path": str(self.dispatch_table_path),
            "vault_dir": str(self.vault_dir),
            "profile_json_path": str(self.profile_json_path),
            "markdown_path": str(self.markdown_path),
            "packages": list(self.packages),
            "test_total": self.test_total,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "ambiguities": [dict(item) for item in self.ambiguities],
        }


def default_profile_name(repo_path: str | Path) -> str:
    """Stable, filesystem-safe profile name for a repository directory."""
    raw = Path(repo_path).resolve().name or "profile"
    cleaned = re.sub(r"[^\w.-]+", "-", raw, flags=re.UNICODE).strip("-._")
    return cleaned or "profile"


def _posix(path: Path | PurePosixPath | str) -> str:
    return str(path).replace("\\", "/")


def _is_excluded_relative(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        if part in _EXCLUDED_DIR_NAMES or part.startswith("."):
            return True
    return False


def _is_test_filename(name: str) -> bool:
    return (
        name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _iter_python_files(root: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _is_excluded_relative(relative):
            continue
        yield path, relative.as_posix()


def _discover_packages(root: Path, ambiguities: list[dict]):
    """Return top-level package directories and their source modules."""
    package_dirs: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _EXCLUDED_DIR_NAMES and not name.startswith(".")
        )
        relative = Path(dirpath).relative_to(root)
        if relative == Path("."):
            continue
        parts = PurePosixPath(relative.as_posix()).parts
        if parts and parts[0] == "tests":
            continue
        if (Path(dirpath) / "__init__.py").is_file():
            package_dirs.append(relative.as_posix())

    package_set = set(package_dirs)
    top_level: list[str] = []
    for package in sorted(package_dirs):
        parent = PurePosixPath(package).parent
        if parent.as_posix() not in package_set:
            top_level.append(package)

    modules: list[SourceModule] = []
    seen_module_paths: set[str] = set()
    for package in sorted(set(top_level)):
        package_path = root / package
        for _path, relative in _iter_python_files(package_path):
            full_relative = (
                PurePosixPath(package) / PurePosixPath(relative)
            ).as_posix()
            if full_relative in seen_module_paths:
                continue
            seen_module_paths.add(full_relative)
            parts = PurePosixPath(full_relative).parts
            stem = PurePosixPath(full_relative).stem
            if parts[0] in _LAYOUT_DIR_NAMES:
                import_parts = list(parts[1:-1])
            else:
                import_parts = list(parts[:-1])
            if stem == "__init__":
                dotted = ".".join(import_parts)
            else:
                dotted = ".".join(import_parts + [stem])
            modules.append(
                SourceModule(
                    path=full_relative,
                    stem=stem,
                    package=package,
                    dotted=dotted,
                )
            )

    modules.sort(key=lambda item: item.path)
    if not modules:
        ambiguities.append(
            {
                "kind": "no_package",
                "detail": "未发现含 __init__.py 的包目录，无法生成模块专家。",
            }
        )
    return sorted(set(top_level)), modules


def _choose_vault_package(
    packages: list[str],
    ambiguities: list[dict],
) -> str | None:
    """Pick the single repository-relative directory the acceptor can copy.

    One package uses its own path (the GEN1 parameterisation).  Several
    packages use their common parent (usually ``src``) so the acceptor copies
    all of them together.  A flat multi-package root cannot be represented by
    the existing acceptor manifest, so the first package is used and the
    limitation is recorded instead of pretending the vault covers everything.
    """
    if not packages:
        return None
    if len(packages) == 1:
        return packages[0]
    try:
        common = os.path.commonpath(packages)
    except ValueError:
        common = ""
    common = _posix(common).strip("/")
    if common and common != ".":
        return common
    ambiguities.append(
        {
            "kind": "flat_multi_package",
            "detail": (
                "检测到多个顶层包但没有共同父目录；现有 acceptor 的 "
                "manifest.package 只能记录一个目录，金库暂按第一个包 "
                f"{packages[0]} 建档。"
            ),
            "packages": list(packages),
        }
    )
    return packages[0]


def _discover_test_files(root: Path) -> tuple[list[str], list[str]]:
    """Return repository-relative test files and non-test config files."""
    test_files: list[str] = []
    config_files: list[str] = set()

    for path, relative in _iter_python_files(root):
        pure = PurePosixPath(relative)
        if pure.name == "conftest.py":
            config_files.add(relative)
            continue
        in_tests_dir = bool(pure.parts) and pure.parts[0] == "tests"
        at_root = pure.parent == PurePosixPath(".")
        if (in_tests_dir or at_root) and _is_test_filename(pure.name):
            test_files.append(relative)

    for name in ("pytest.ini", "pyproject.toml", "setup.cfg"):
        candidate = root / name
        if not candidate.is_file():
            continue
        if name == "pytest.ini":
            config_files.add(name)
        elif name == "pyproject.toml" and _pyproject_has_pytest(candidate):
            config_files.add(name)
        elif name == "setup.cfg" and _setup_cfg_has_pytest(candidate):
            config_files.add(name)

    # tests/__init__.py files are needed when the test tree is a package.
    for test_file in test_files:
        directory = (root / test_file).parent
        while directory != root:
            init_file = directory / "__init__.py"
            if init_file.is_file():
                config_files.add(
                    init_file.relative_to(root).as_posix()
                )
            directory = directory.parent

    return sorted(set(test_files)), sorted(config_files)


def _pyproject_has_pytest(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    options = data.get("tool", {}).get("pytest", {}).get("ini_options")
    return isinstance(options, dict)


def _setup_cfg_has_pytest(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(
        line.strip() == "[tool:pytest]" for line in text.splitlines()
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_vault(
    repo: Path,
    vault_dir: Path,
    package: str,
    packages: list[str],
    test_files: list[str],
    config_files: list[str],
) -> dict:
    """Freeze the judging surface, then add test layouts freeze does not cover.

    ``acceptor.freeze`` only knows top-level ``tests/test_*.py`` plus root
    ``conftest.py``/``pytest.ini``.  The profiler detects ``*_test.py``, nested
    tests, package ``__init__.py`` and extra pytest config as well; those files
    are added to the same vault and manifest so the frozen acceptor can still
    judge them.
    """
    surface = judging_surface(repo)
    if surface:
        manifest = freeze(repo, vault_dir, package=package)
    else:
        # ``freeze`` has no judging file to seed the vault when a repository
        # only has root-level tests/config (its hard-coded surface is
        # tests/test_*.py + root conftest.py/pytest.ini).  Create the same
        # manifest shape, then the detection loop below adds those files.
        if vault_dir.exists():
            shutil.rmtree(vault_dir)
        vault_dir.mkdir(parents=True)
        manifest = {
            "files": {},
            "package": package,
            "source": str(repo.resolve()),
        }

    covered = {path.relative_to(repo).as_posix() for path in surface}
    extras = sorted((set(test_files) | set(config_files)) - covered)
    for relative in extras:
        source = repo / relative
        if not source.is_file():
            continue
        target = vault_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest.setdefault("files", {})[relative] = _sha256_file(target)

    manifest["package"] = package
    manifest["packages"] = list(packages)
    manifest["generator"] = GENERATOR
    manifest["generator_version"] = GENERATOR_VERSION
    (vault_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _relative_in_copy(raw: str, copy_root: Path) -> str | None:
    value = str(raw).strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(copy_root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def _parse_failure_blocks(output: str, copy_root: Path) -> list[dict]:
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("=") and " FAILURES " in line:
            start = index
            break
    if start is None:
        return []

    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.startswith("="):
            continue
        if any(
            marker in line
            for marker in (
                " PASSES ",
                " short test summary info ",
                " warnings summary ",
                " SKIPPED ",
            )
        ):
            end = index
            break

    blocks: list[dict] = []
    current: dict | None = None
    for line in lines[start + 1 : end]:
        header = _FAILURE_BLOCK.match(line)
        if header is not None:
            if current is not None:
                blocks.append(current)
            current = {
                "name": header.group(1).strip(),
                "frames": [],
                "frame_locs": [],
            }
            continue
        if current is None:
            continue
        frame = _TRACEBACK_FRAME.match(line)
        if frame is None:
            continue
        relative = _relative_in_copy(frame.group(1), copy_root)
        if relative is not None:
            current["frames"].append(relative)
            current["frame_locs"].append((relative, int(frame.group(2))))
    if current is not None:
        blocks.append(current)
    return blocks


def _parse_pytest_output(output: str, copy_root: Path) -> dict:
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        match = _SUMMARY_LINE.match(line)
        if match is None:
            continue
        status, payload = match.group(1), match.group(2).strip()
        node_id = re.split(r"\s+-\s+", payload, maxsplit=1)[0].strip()
        if not node_id:
            continue
        statuses.setdefault(node_id, status)

    failed_nodes = [
        node_id
        for node_id, status in statuses.items()
        if status in {"FAILED", "ERROR"}
    ]
    blocks = _parse_failure_blocks(output, copy_root)
    frames_by_node: dict[str, tuple[str, ...]] = {}
    frame_locs_by_node: dict[str, tuple[tuple[str, int], ...]] = {}
    if len(blocks) == len(failed_nodes):
        pairs = zip(failed_nodes, blocks)
    else:
        pairs = []
        unused = list(failed_nodes)
        for block in blocks:
            header = block["name"]
            match = next(
                (
                    node
                    for node in unused
                    if node.split("::")[-1] == header
                    or node.endswith("::" + header)
                ),
                None,
            )
            if match is not None:
                unused.remove(match)
                pairs.append((match, block))
    for node_id, block in pairs:
        frames_by_node[node_id] = tuple(dict.fromkeys(block["frames"]))
        frame_locs_by_node[node_id] = tuple(block["frame_locs"])

    passed_count = sum(
        1 for status in statuses.values() if status == "PASSED"
    )
    return {
        "all_nodes": sorted(statuses),
        "failed_nodes": failed_nodes,
        "passed_count": passed_count,
        "failed_count": len(failed_nodes),
        "test_total": len(statuses),
        "frames_by_node": frames_by_node,
        "frame_locs_by_node": frame_locs_by_node,
    }


def _run_pytest_on_copy(repo: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="autoprofile-run-") as temporary:
        copy_root = Path(temporary) / "repo"
        shutil.copytree(
            repo,
            copy_root,
            ignore=_COPY_IGNORE,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTHONPATH", None)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-rA",
                    "-p",
                    "no:cacheprovider",
                    "--tb=long",
                ],
                cwd=copy_root,
                capture_output=True,
                text=True,
                env=env,
                timeout=PYTEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return {
                "all_nodes": [],
                "failed_nodes": [],
                "passed_count": 0,
                "failed_count": 0,
                "test_total": 0,
                "frames_by_node": {},
                "frame_locs_by_node": {},
                "returncode": -1,
                "output": "pytest timed out",
            }
        output = completed.stdout + completed.stderr
        parsed = _parse_pytest_output(output, copy_root)
        parsed["returncode"] = completed.returncode
        parsed["output"] = output
        return parsed


def _resolve_dotted(
    name: str,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
) -> set[str]:
    if not name:
        return set()
    exact = modules_by_dotted.get(name)
    if exact:
        # ``import pkg`` (a package __init__) is treated as a broad import of
        # its submodules; tests that only touch re-exports still get experts.
        if all(item.stem == "__init__" for item in exact):
            expanded = {
                module.path
                for module in package_modules.get(name, [])
                if module.stem != "__init__"
            }
            return {item.path for item in exact} | expanded
        return {item.path for item in exact}
    if name in package_modules:
        return {
            module.path
            for module in package_modules[name]
            if module.stem != "__init__"
        }
    return set()


def _drop_relay_init_frames(frames: Iterable[str]) -> tuple[str, ...]:
    """Drop ``__init__.py`` relay frames when a real module frame exists.

    ``from pkg import name`` runs the package root first, so a failure below
    it can leave ``pkg/__init__.py`` in the traceback even though the bug is
    in ``pkg/sub.py``.  The root only switches the call over; when the node
    has at least one non-``__init__`` package frame, every ``__init__`` frame
    of that node is discarded.  A failure raised *inside* ``__init__`` has no
    other package frame and therefore keeps its frame.
    """
    ordered = tuple(dict.fromkeys(frames))
    if not any(
        PurePosixPath(path).name != "__init__.py" for path in ordered
    ):
        return ordered
    return tuple(
        path for path in ordered if PurePosixPath(path).name != "__init__.py"
    )


def _module_defined_names(
    repo: Path,
    modules: Iterable[SourceModule],
) -> dict[str, set[str]]:
    """Top-level names each source module defines itself (imports excluded)."""
    names: dict[str, set[str]] = {}
    for module in modules:
        path = repo / module.path
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        names[module.path] = _collect_defined_names(tree)
    return names


def _build_export_maps(
    repo: Path,
    modules: list[SourceModule],
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Map every package root to the names its ``__init__`` re-exports.

    ``from <pkg> import <name>`` must land on the module that really defines
    ``<name>``.  The package root is only a switchboard, so the AST of its
    re-export statements (``from .<sub> import <name>``) is the mapping.
    Names the ``__init__`` defines itself stay on the ``__init__`` path; names
    it neither defines nor re-exports are absent from the map and callers
    treat them as unresolved.
    """
    defined = _module_defined_names(repo, modules)
    maps: dict[str, dict[str, tuple[str, ...]]] = {}
    for module in modules:
        if module.stem != "__init__":
            continue
        exports: dict[str, set[str]] = {
            name: {module.path} for name in defined.get(module.path, set())
        }
        star_sources: set[str] = set()
        path = repo / module.path
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (OSError, SyntaxError, UnicodeDecodeError):
            maps[module.dotted] = {}
            continue
        package_parts = module.dotted.split(".") if module.dotted else []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = list(package_parts)
                if node.level > 1:
                    base = (
                        base[: -(node.level - 1)]
                        if node.level - 1 <= len(base)
                        else []
                    )
                prefix = ".".join(
                    base + (node.module.split(".") if node.module else [])
                )
            else:
                prefix = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    star_sources |= _resolve_dotted(
                        prefix, modules_by_dotted, package_modules
                    )
                    continue
                local = alias.asname or alias.name
                combined = (
                    f"{prefix}.{alias.name}" if prefix else alias.name
                )
                if combined in modules_by_dotted:
                    paths = _resolve_dotted(
                        combined, modules_by_dotted, package_modules
                    )
                elif prefix:
                    paths = _resolve_dotted(
                        prefix, modules_by_dotted, package_modules
                    )
                else:
                    paths = set()
                if paths:
                    exports.setdefault(local, set()).update(paths)
        # ``from .<sub> import *``: a name belongs to the submodule that
        # defines it.  Unknown star names stay unresolved on purpose.
        for source in sorted(star_sources):
            for name in defined.get(source, set()):
                exports.setdefault(name, set()).add(source)
        maps[module.dotted] = {
            name: tuple(sorted(paths))
            for name, paths in sorted(exports.items())
            if paths
        }
    return maps


def _resolve_imported_name(
    prefix: str,
    name: str,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
    export_maps: dict[str, dict[str, tuple[str, ...]]],
) -> set[str] | None:
    """Resolve one ``from <prefix> import <name>`` alias.

    Returns the defining module paths, or ``None`` when *prefix* is a package
    root that neither defines nor re-exports *name*.  ``None`` means
    "unresolved": the caller records an ambiguity and adds no module, so an
    unknown name can never fan out to the whole package.  Non-package
    prefixes (``from pkg.module import name``) keep the old one-module
    fallback byte-for-byte.
    """
    if prefix in export_maps:
        mapped = export_maps[prefix].get(name)
        return set(mapped) if mapped else None
    return _resolve_dotted(prefix, modules_by_dotted, package_modules)


def _test_module_parts(test_file: str, repo: Path) -> list[str]:
    pure = PurePosixPath(test_file)
    candidates = list(pure.with_suffix("").parts)
    directory = (repo / test_file).parent
    while directory != repo:
        if not (directory / "__init__.py").is_file():
            return [pure.stem]
        directory = directory.parent
    return candidates


def _resolve_relative(
    level: int,
    module: str | None,
    test_file: str,
    repo: Path,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
) -> set[str]:
    parts = _test_module_parts(test_file, repo)
    if parts == [PurePosixPath(test_file).stem]:
        return set()
    base = parts[:-1]
    if level > 1:
        base = base[: -(level - 1)] if level - 1 <= len(base) else []
    if module:
        base = base + module.split(".")
    return _resolve_dotted(".".join(base), modules_by_dotted, package_modules)


def _import_candidates(
    test_file: str,
    repo: Path,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
    ambiguities: list[dict],
    export_maps: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> set[str]:
    path = repo / test_file
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        ambiguities.append(
            {
                "kind": "test_parse_error",
                "test_file": test_file,
                "detail": f"无法解析测试文件 AST：{type(exc).__name__}: {exc}",
            }
        )
        return set()

    exports = export_maps or {}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _resolve_dotted(
                    alias.name, modules_by_dotted, package_modules
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found |= _resolve_relative(
                    node.level,
                    node.module,
                    test_file,
                    repo,
                    modules_by_dotted,
                    package_modules,
                )
                base = [
                    part
                    for part in _test_module_parts(test_file, repo)[:-1]
                ]
                if node.level > 1:
                    base = base[: -(node.level - 1)] if node.level - 1 <= len(base) else []
                if node.module:
                    base = base + node.module.split(".")
                prefix = ".".join(base)
            else:
                prefix = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    found |= _resolve_dotted(
                        prefix, modules_by_dotted, package_modules
                    )
                    continue
                combined = (
                    f"{prefix}.{alias.name}" if prefix else alias.name
                )
                if combined in modules_by_dotted:
                    found |= _resolve_dotted(
                        combined, modules_by_dotted, package_modules
                    )
                elif prefix:
                    resolved = _resolve_imported_name(
                        prefix,
                        alias.name,
                        modules_by_dotted,
                        package_modules,
                        exports,
                    )
                    if resolved is None:
                        ambiguities.append(
                            {
                                "kind": "unresolved_reexport",
                                "test_file": test_file,
                                "detail": (
                                    f"`from {prefix} import {alias.name}`："
                                    f"`{prefix}` 的 `__init__` 既未定义也未再"
                                    "导出该名字，未计入任何模块，不做整包回退。"
                                ),
                            }
                        )
                    else:
                        found |= resolved
    return found


def _module_dependencies(
    module: SourceModule,
    repo: Path,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
    export_maps: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> set[str]:
    """Direct package-internal imports of one source module (one hop).

    Test files often exercise a facade module (``bookings``) whose real
    failure lives in a module it imports (``conflicts``).  Adding one hop of
    static internal dependencies keeps the generated table covering that
    root cause without pretending to build a full call graph.
    """
    path = repo / module.path
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    if module.stem == "__init__":
        package_parts = module.dotted.split(".") if module.dotted else []
    else:
        package_parts = module.dotted.split(".")[:-1]

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _resolve_dotted(
                    alias.name, modules_by_dotted, package_modules
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package_parts)
                if node.level > 1:
                    base = (
                        base[: -(node.level - 1)]
                        if node.level - 1 <= len(base)
                        else []
                    )
                prefix = ".".join(
                    base
                    + (node.module.split(".") if node.module else [])
                )
            else:
                prefix = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    found |= _resolve_dotted(
                        prefix, modules_by_dotted, package_modules
                    )
                    continue
                combined = (
                    f"{prefix}.{alias.name}" if prefix else alias.name
                )
                if combined in modules_by_dotted:
                    found |= _resolve_dotted(
                        combined, modules_by_dotted, package_modules
                    )
                elif prefix:
                    resolved = _resolve_imported_name(
                        prefix,
                        alias.name,
                        modules_by_dotted,
                        package_modules,
                        export_maps or {},
                    )
                    if resolved is not None:
                        found |= resolved
    found.discard(module.path)
    return found


def _test_module_key(test_file: str) -> str:
    """Module key consumed by ``acceptor.evidence.module_from_node_id``."""
    stem = PurePosixPath(test_file).stem
    if stem.startswith("test_"):
        return stem[len("test_") :]
    if stem.endswith("_test"):
        return stem[: -len("_test")]
    return stem


def _naming_candidates(
    test_file: str,
    modules: list[SourceModule],
    ambiguities: list[dict],
) -> set[str]:
    stem = PurePosixPath(test_file).stem
    wanted = _test_module_key(test_file)
    matches = [
        module.path
        for module in modules
        if module.stem == wanted and module.stem != "__init__"
    ]
    if not matches:
        ambiguities.append(
            {
                "kind": "naming_convention_miss",
                "test_file": test_file,
                "detail": (
                    f"{stem}.py 无同名源模块 {wanted}.py，命名约定失效；"
                    "该文件改由 import / traceback 证据映射。"
                ),
            }
        )
    elif len(matches) > 1:
        ambiguities.append(
            {
                "kind": "duplicate_module_name",
                "test_file": test_file,
                "detail": (
                    f"命名约定命中多个同名模块：{'、'.join(matches)}；"
                    "候选全部保留。"
                ),
            }
        )
    return set(matches)


def _sanitise_identifier(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return cleaned or "module"


def _module_docstring(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ""
    docstring = ast.get_docstring(tree) or ""
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _specialist_id(
    module: SourceModule,
    used: dict[str, str],
) -> str:
    base = f"{_sanitise_identifier(module.stem)}-specialist"
    if base not in used:
        return base
    parent = PurePosixPath(module.path).parent.name or "pkg"
    candidate = (
        f"{_sanitise_identifier(parent)}-"
        f"{_sanitise_identifier(module.stem)}-specialist"
    )
    suffix = 2
    while candidate in used:
        candidate = (
            f"{_sanitise_identifier(parent)}-"
            f"{_sanitise_identifier(module.stem)}-{suffix}-specialist"
        )
        suffix += 1
    return candidate


def _build_specialists(
    repo: Path,
    modules: list[SourceModule],
) -> tuple[list[dict], dict[str, str], dict[str, dict]]:
    specialists: list[dict] = []
    module_to_specialist: dict[str, str] = {}
    roster_entries: dict[str, dict] = {}
    used: dict[str, str] = {}

    for module in modules:
        if module.stem == "__init__":
            continue
        specialist_id = _specialist_id(module, used)
        used[specialist_id] = module.path
        module_to_specialist[module.path] = specialist_id
        blurb = _module_docstring(repo / module.path)
        if not blurb:
            blurb = f"负责 {module.stem} 模块的修复。"
        specialists.append(
            {
                "id": specialist_id,
                "name": f"{module.stem} 专家",
                "scope": f"{module.path}：{blurb}",
                "files": [module.path],
                "blurb": blurb,
            }
        )
        roster_entries[specialist_id] = {
            "files": [module.path],
            "blurb": blurb,
            "specialist": True,
        }

    all_files = sorted(module.path for module in modules)
    generalist_blurb = (
        "你是 generalist（通用修理工），在没有明确归因或需要跨模块兜底时"
        "做最小修复。"
    )
    specialists.append(
        {
            "id": _FALLBACK_ID,
            "name": "通用修理工",
            "scope": "无专属专家或跨模块无明确归因时的兜底修复",
            "files": all_files,
            "blurb": generalist_blurb,
        }
    )
    roster_entries[_FALLBACK_ID] = {
        "files": all_files,
        "blurb": generalist_blurb,
        "specialist": False,
    }
    return specialists, module_to_specialist, roster_entries


def _candidate_ids(
    module_paths: Iterable[str],
    module_to_specialist: dict[str, str],
) -> tuple[str, ...]:
    ids: list[str] = []
    for path in sorted(set(module_paths)):
        specialist_id = module_to_specialist.get(path, _FALLBACK_ID)
        if specialist_id not in ids:
            ids.append(specialist_id)
    return tuple(ids)


def _file_primary(
    test_file: str,
    nodes: list[str],
    imports: set[str],
    naming: set[str],
    tracebacks: dict[str, str],
    modules_by_path: dict[str, SourceModule],
    file_default: str,
) -> str:
    if naming:
        return sorted(naming)[0]
    for node in nodes:
        if node in tracebacks:
            return tracebacks[node]
    if imports:
        return sorted(imports)[0]
    return file_default


@dataclass
class _ModuleFacts:
    """Static symbol/import facts used only for narrow failure attribution."""

    path: str
    tree: ast.Module
    bindings: dict[str, tuple[str, ...]]
    defined: set[str]
    functions: dict[str, list[ast.AST]]


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        found: set[str] = set()
        for item in target.elts:
            found |= _target_names(item)
        return found
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return {target.attr}
    return set()


def _statement_targets(nodes: list[ast.stmt]) -> set[str]:
    found: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                found |= _target_names(target)
    return found


def _collect_defined_names(tree: ast.Module) -> set[str]:
    found = _statement_targets(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            found |= _statement_targets(node.body)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    found.add(target.attr)
    return found


def _collect_functions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    functions: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.setdefault(child.name, []).append(child)
    return functions


def _module_bindings(
    tree: ast.Module,
    module: SourceModule | None,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
    export_maps: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> dict[str, tuple[str, ...]]:
    bindings: dict[str, set[str]] = {}
    exports = export_maps or {}
    if module is None:
        package_parts: list[str] = []
    elif module.stem == "__init__":
        package_parts = module.dotted.split(".") if module.dotted else []
    else:
        package_parts = module.dotted.split(".")[:-1] if module.dotted else []

    def add(local: str, paths: set[str]) -> None:
        if local and paths:
            bindings.setdefault(local, set()).update(paths)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    local = alias.asname
                    target = alias.name
                else:
                    local = alias.name.split(".")[0]
                    target = local
                add(
                    local,
                    _resolve_dotted(
                        target, modules_by_dotted, package_modules
                    ),
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package_parts)
                if node.level > 1:
                    base = (
                        base[: -(node.level - 1)]
                        if node.level - 1 <= len(base)
                        else []
                    )
                prefix = ".".join(
                    base + (node.module.split(".") if node.module else [])
                )
            else:
                prefix = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                combined = (
                    f"{prefix}.{alias.name}" if prefix else alias.name
                )
                paths = _resolve_dotted(
                    combined, modules_by_dotted, package_modules
                )
                if not paths and prefix:
                    resolved = _resolve_imported_name(
                        prefix,
                        alias.name,
                        modules_by_dotted,
                        package_modules,
                        exports,
                    )
                    # ``None`` is an unresolved re-export: bind nothing.  A
                    # binding to the whole package is exactly the fan-out
                    # GEN5 removes.
                    if resolved is not None:
                        paths = resolved
                add(alias.asname or alias.name, paths)
    return {
        name: tuple(sorted(paths))
        for name, paths in sorted(bindings.items())
    }


def _build_module_facts(
    path: str,
    tree: ast.Module,
    module: SourceModule | None,
    modules_by_dotted: dict[str, list[SourceModule]],
    package_modules: dict[str, list[SourceModule]],
    export_maps: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> _ModuleFacts:
    return _ModuleFacts(
        path=path,
        tree=tree,
        bindings=_module_bindings(
            tree,
            module,
            modules_by_dotted,
            package_modules,
            export_maps,
        ),
        defined=_collect_defined_names(tree),
        functions=_collect_functions(tree),
    )


def _function_env(func: ast.AST) -> dict[str, ast.AST]:
    env: dict[str, ast.AST] = {}

    def visit(node: ast.AST) -> None:
        if node is not func and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                env[node.target.id] = node.value
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                env[node.target.id] = node.iter
        elif isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    env[item.optional_vars.id] = item.context_expr
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                env[node.target.id] = node.value
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(func)
    return env


def _fixture_returns(tree: ast.Module) -> dict[str, ast.AST]:
    fixtures: dict[str, ast.AST] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            _decorator_name(item) == "fixture"
            for item in node.decorator_list
        ):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and child.value is not None:
                fixtures[node.name] = child.value
                break
    return fixtures


def _find_test_function(tree: ast.Module, node_id: str) -> ast.AST | None:
    name = node_id.split("::")[-1].split("[", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _analysis_expressions(func: ast.AST) -> list[ast.AST]:
    expressions: list[ast.AST] = []

    def visit(node: ast.AST) -> None:
        if node is not func and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            return
        if isinstance(node, ast.Assert):
            expressions.append(node.test)
        elif isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if not isinstance(call, ast.Call):
                    continue
                name = ""
                if isinstance(call.func, ast.Attribute):
                    name = call.func.attr
                elif isinstance(call.func, ast.Name):
                    name = call.func.id
                if name == "raises" and call.args:
                    expressions.append(call.args[0])
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(func)
    return expressions


class _EvidenceAnalyzer:
    """Narrow static attribution for failed nodes without traceback frames.

    The traceback is always the first choice.  When a failure died in the
    test body before/without entering package code (assertion failures,
    ``pytest.raises`` misses, collection errors), this resolver follows the
    asserted expression back to the source module that produced the value.
    It intentionally ignores dictionary subscripts: a storage round-trip
    assertion says nothing reliable about which source module is at fault.
    """

    def __init__(
        self,
        repo: Path,
        modules: list[SourceModule],
        modules_by_dotted: dict[str, list[SourceModule]],
        package_modules: dict[str, list[SourceModule]],
        export_maps: dict[str, dict[str, tuple[str, ...]]] | None = None,
    ) -> None:
        self.repo = repo
        self.modules_by_dotted = modules_by_dotted
        self.package_modules = package_modules
        self.export_maps = export_maps or {}
        self.paths = {module.path for module in modules}
        self.facts: dict[str, _ModuleFacts] = {}
        for module in modules:
            try:
                tree = ast.parse(
                    (repo / module.path).read_text(encoding="utf-8"),
                    filename=str(repo / module.path),
                )
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            self.facts[module.path] = _build_module_facts(
                module.path,
                tree,
                module,
                modules_by_dotted,
                package_modules,
                self.export_maps,
            )
        self._allowed: set[str] = set()

    def _test_facts(self, test_file: str) -> _ModuleFacts | None:
        path = self.repo / test_file
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (OSError, SyntaxError, UnicodeDecodeError):
            return None
        return _build_module_facts(
            test_file,
            tree,
            None,
            self.modules_by_dotted,
            self.package_modules,
            self.export_maps,
        )

    def _resolve(
        self,
        expr: ast.AST | None,
        env: dict[str, ast.AST],
        facts: _ModuleFacts,
        depth: int = 0,
    ) -> tuple[str, ...]:
        if expr is None or depth > 10:
            return ()
        if isinstance(expr, ast.Constant):
            return ()
        if isinstance(expr, ast.Name):
            if expr.id in env:
                return self._resolve(env[expr.id], env, facts, depth + 1)
            if expr.id in facts.bindings:
                return facts.bindings[expr.id]
            if expr.id in facts.functions or expr.id in facts.defined:
                return (facts.path,)
            return ()
        if isinstance(expr, ast.Attribute):
            return self._resolve_attribute(expr, env, facts, depth)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name):
                name = expr.func.id
                if name in env:
                    return self._resolve(env[name], env, facts, depth + 1)
                if name in facts.bindings:
                    return facts.bindings[name]
                if name in facts.functions or name in facts.defined:
                    return (facts.path,)
                return ()
            if isinstance(expr.func, ast.Attribute):
                return self._resolve_attribute(
                    expr.func, env, facts, depth
                )
            return self._resolve(expr.func, env, facts, depth + 1)
        if isinstance(expr, ast.Compare):
            return self._first_modules(
                [expr.left, *expr.comparators], env, facts, depth
            )
        if isinstance(expr, ast.BoolOp):
            return self._first_modules(expr.values, env, facts, depth)
        if isinstance(expr, ast.UnaryOp):
            return self._resolve(expr.operand, env, facts, depth + 1)
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            if expr.generators:
                return self._resolve(
                    expr.generators[0].iter, env, facts, depth + 1
                )
            return ()
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return self._first_modules(expr.elts, env, facts, depth)
        if isinstance(expr, ast.Subscript):
            # Storage round-trips / mapping lookups are not source logic.
            return ()
        if isinstance(expr, ast.BinOp):
            return self._first_modules(
                [expr.left, expr.right], env, facts, depth
            )
        if isinstance(expr, ast.IfExp):
            return self._first_modules(
                [expr.body, expr.orelse], env, facts, depth
            )
        if isinstance(expr, ast.Await):
            return self._resolve(expr.value, env, facts, depth + 1)
        if isinstance(expr, ast.NamedExpr):
            return self._resolve(expr.value, env, facts, depth + 1)
        return ()

    def _first_modules(
        self,
        expressions: Iterable[ast.AST],
        env: dict[str, ast.AST],
        facts: _ModuleFacts,
        depth: int,
    ) -> tuple[str, ...]:
        for item in expressions:
            modules = self._resolve(item, env, facts, depth + 1)
            if modules:
                return modules
        return ()

    def _resolve_attribute(
        self,
        expr: ast.Attribute,
        env: dict[str, ast.AST],
        facts: _ModuleFacts,
        depth: int,
    ) -> tuple[str, ...]:
        attr = expr.attr
        defined = sorted(
            path
            for path in self._allowed
            if path in self.facts and attr in self.facts[path].defined
        )
        if len(defined) == 1:
            return (defined[0],)

        provenance = self._attribute_provenance(
            expr, env, facts, depth
        )
        if provenance:
            return provenance

        base_modules = self._resolve(
            expr.value, env, facts, depth + 1
        )
        if len(defined) > 1:
            for module_path in base_modules:
                if module_path in defined:
                    return (module_path,)
            return tuple(defined)
        if defined:
            return tuple(defined)
        return base_modules

    def _attribute_provenance(
        self,
        expr: ast.Attribute,
        env: dict[str, ast.AST],
        facts: _ModuleFacts,
        depth: int,
    ) -> tuple[str, ...]:
        attr = expr.attr
        base = expr.value
        if not isinstance(base, ast.Name) or base.id not in env:
            return ()
        targets = self._call_targets(env[base.id], env, facts, depth + 1)
        for module_path, function_name in targets:
            resolved = self._return_field_module(
                module_path, function_name, attr, depth + 1
            )
            if resolved:
                return (resolved,)
        if len(targets) == 1:
            return (targets[0][0],)
        return ()

    def _call_targets(
        self,
        expr: ast.AST | None,
        env: dict[str, ast.AST],
        facts: _ModuleFacts,
        depth: int,
    ) -> tuple[tuple[str, str], ...]:
        if expr is None or depth > 8 or not isinstance(expr, ast.Call):
            return ()
        func = expr.func
        if isinstance(func, ast.Name):
            if func.id in facts.bindings:
                return tuple(
                    (path, func.id)
                    for path in facts.bindings[func.id]
                )
            if func.id in facts.functions:
                return ((facts.path, func.id),)
            if func.id in env:
                return self._call_targets(
                    env[func.id], env, facts, depth + 1
                )
            return ()
        if isinstance(func, ast.Attribute):
            modules = self._resolve(
                func.value, env, facts, depth + 1
            )
            return tuple(
                (module_path, func.attr) for module_path in modules
            )
        return ()

    def _find_function(
        self, module_path: str, name: str
    ) -> ast.AST | None:
        facts = self.facts.get(module_path)
        if facts is None:
            return None
        candidates = facts.functions.get(name, [])
        return candidates[0] if candidates else None

    def _return_field_module(
        self,
        module_path: str,
        function_name: str,
        field_name: str,
        depth: int,
    ) -> str | None:
        if depth > 10:
            return None
        func = self._find_function(module_path, function_name)
        if func is None:
            return None
        facts = self.facts[module_path]
        env = _function_env(func)
        for node in ast.walk(func):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            if isinstance(value, ast.Name) and value.id in env:
                value = env[value.id]
            if not isinstance(value, ast.Call):
                continue
            for keyword in value.keywords:
                if keyword.arg != field_name:
                    continue
                resolved = self._resolve(
                    keyword.value, env, facts, depth + 1
                )
                if resolved:
                    return resolved[0]
        return None

    def static_modules(
        self,
        test_file: str,
        node_id: str,
        allowed: Iterable[str],
    ) -> tuple[str, ...]:
        facts = self._test_facts(test_file)
        if facts is None:
            return ()
        func = _find_test_function(facts.tree, node_id)
        if func is None:
            return ()
        self._allowed = set(allowed)
        fixtures = _fixture_returns(facts.tree)
        env = _function_env(func)
        args = list(getattr(func.args, "posonlyargs", []))
        args += list(func.args.args) + list(func.args.kwonlyargs)
        for arg in args:
            if arg.arg in fixtures:
                env[arg.arg] = fixtures[arg.arg]
        modules: list[str] = []
        for expr in _analysis_expressions(func):
            modules.extend(self._resolve(expr, env, facts, 0))
        return tuple(
            sorted(
                {
                    module_path
                    for module_path in modules
                    if module_path in self.paths
                }
            )
        )

    def raise_chain_modules(
        self,
        frame_locs: Iterable[tuple[str, int]],
        source_paths: set[str],
    ) -> tuple[str, ...]:
        package_locs = [
            (path, line)
            for path, line in frame_locs
            if path in source_paths and path in self.facts
        ]
        if not package_locs:
            return ()
        module_path, line = package_locs[-1]
        added: set[str] = set()
        func = self._function_containing(module_path, line)
        if func is not None:
            expressions: list[ast.AST] = []
            enclosing = [
                node
                for node in ast.walk(func)
                if isinstance(node, ast.If)
                and node.lineno <= line <= node.end_lineno
            ]
            if enclosing:
                expressions.append(
                    min(
                        enclosing,
                        key=lambda node: node.end_lineno - node.lineno,
                    ).test
                )
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Raise)
                    and node.exc is not None
                    and node.lineno <= line <= node.end_lineno
                ):
                    expressions.append(node.exc)
            for expr in expressions:
                self._follow_calls(
                    module_path, expr, added, source_paths, 0
                )
        added.discard(module_path)
        return tuple(sorted(added))

    def _function_containing(
        self, module_path: str, line: int
    ) -> ast.AST | None:
        facts = self.facts.get(module_path)
        if facts is None:
            return None
        candidates = [
            func
            for funcs in facts.functions.values()
            for func in funcs
            if func.lineno <= line <= getattr(func, "end_lineno", func.lineno)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda func: func.lineno)

    def _follow_calls(
        self,
        module_path: str,
        expr: ast.AST,
        added: set[str],
        source_paths: set[str],
        depth: int,
    ) -> None:
        if depth > 3:
            return
        facts = self.facts.get(module_path)
        if facts is None:
            return
        for node in ast.walk(expr):
            if not isinstance(node, ast.Call):
                continue
            for target_module, function_name in self._source_call_targets(
                module_path, facts, node
            ):
                if target_module != module_path:
                    if target_module in source_paths:
                        added.add(target_module)
                    continue
                if depth >= 2:
                    continue
                target = self._find_function(
                    module_path, function_name
                )
                if target is None:
                    continue
                for child in ast.walk(target):
                    if isinstance(child, (ast.Return, ast.Assign, ast.AnnAssign)):
                        self._follow_calls(
                            module_path,
                            child,
                            added,
                            source_paths,
                            depth + 1,
                        )

    def _source_call_targets(
        self,
        module_path: str,
        facts: _ModuleFacts,
        call: ast.Call,
    ) -> tuple[tuple[str, str], ...]:
        func = call.func
        if isinstance(func, ast.Name):
            if func.id in facts.bindings:
                return tuple(
                    (path, func.id) for path in facts.bindings[func.id]
                )
            if func.id in facts.functions:
                return ((module_path, func.id),)
            return ()
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                if func.value.id == "self":
                    return ((module_path, func.attr),)
                if func.value.id in facts.bindings:
                    return tuple(
                        (path, func.attr)
                        for path in facts.bindings[func.value.id]
                    )
        return ()


def _is_coupling_node(node_id: str) -> bool:
    """Integration-style tests may legitimately couple more than one module."""
    lowered = node_id.lower()
    return any(
        marker in lowered
        for marker in (
            "end_to_end",
            "e2e",
            "integration",
            "full_",
            "_full",
            "flow",
            "cross",
        )
    )


def _format_paths(paths: Iterable[str]) -> str:
    rendered = "、".join(f"`{path}`" for path in sorted(paths))
    return rendered or "无"


def _traceback_rationale(
    trace_paths: tuple[str, ...],
    frame_locs: Iterable[tuple[str, int]],
    coupling_modules: set[str],
    imports_not_used: set[str],
) -> str:
    line_by_path: dict[str, int] = {}
    for path, line in frame_locs:
        line_by_path[path] = line
    rendered = "、".join(
        f"`{path}:{line_by_path.get(path, '?')}`" for path in trace_paths
    )
    deepest = trace_paths[-1] if trace_paths else "无"
    parts = [
        f"pytest traceback 包内帧 {rendered}（最深帧 `{deepest}` 为主）"
    ]
    if coupling_modules:
        parts.append(
            "耦合调用链补派 " + _format_paths(coupling_modules)
        )
    if imports_not_used:
        parts.append(
            "import 命中 " + _format_paths(imports_not_used)
            + " 但 traceback 未指向，未派"
        )
    return "自动建档证据合并（仅 traceback）：" + "；".join(parts) + "。"


def _import_fallback_rationale(
    candidates: set[str],
    imports_not_used: set[str],
    used_all_imports: bool,
) -> str:
    if used_all_imports:
        evidence = "无 traceback、回退全部 import 命中选择"
    else:
        evidence = "无 traceback、按 import/静态证据派单"
    parts = [evidence + " " + _format_paths(candidates)]
    if imports_not_used:
        parts.append(
            "import 命中 " + _format_paths(imports_not_used)
            + " 但静态证据未指向，未派"
        )
    return "自动建档证据合并：" + "；".join(parts) + "。"


def _build_table(
    repo: Path,
    modules: list[SourceModule],
    test_files: list[str],
    all_nodes: list[str],
    failed_nodes: list[str],
    frames_by_node: dict[str, tuple[str, ...]],
    frame_locs_by_node: dict[str, tuple[tuple[str, int], ...]],
    ambiguities: list[dict],
    vault_package: str | None,
) -> tuple[
    dict,
    dict[str, tuple[str, ...]],
    dict[str, tuple[str, ...]],
    dict[str, dict],
]:
    modules_by_path = {module.path: module for module in modules}
    source_paths = set(modules_by_path)

    modules_by_dotted: dict[str, list[SourceModule]] = {}
    package_modules: dict[str, list[SourceModule]] = {}
    for module in modules:
        modules_by_dotted.setdefault(module.dotted, []).append(module)
        if module.stem == "__init__":
            package_modules.setdefault(module.dotted, []).append(module)
        else:
            package = module.dotted.rsplit(".", 1)[0] if "." in module.dotted else ""
            package_modules.setdefault(package, []).append(module)

    specialists, module_to_specialist, roster_entries = _build_specialists(
        repo, modules
    )
    export_maps = _build_export_maps(
        repo, modules, modules_by_dotted, package_modules
    )

    nodes_by_file: dict[str, list[str]] = {path: [] for path in test_files}
    for node in all_nodes:
        test_file = node.split("::", 1)[0]
        nodes_by_file.setdefault(test_file, []).append(node)

    imports_by_file: dict[str, set[str]] = {}
    naming_by_file: dict[str, set[str]] = {}
    traceback_by_node: dict[str, str] = {}
    for node, frames in frames_by_node.items():
        package_frames = _drop_relay_init_frames(
            path for path in frames if path in source_paths
        )
        if package_frames:
            traceback_by_node[node] = package_frames[-1]

    for test_file in test_files:
        direct_imports = _import_candidates(
            test_file,
            repo,
            modules_by_dotted,
            package_modules,
            ambiguities,
            export_maps,
        )
        expanded = set(direct_imports)
        for module_path in sorted(direct_imports):
            module = modules_by_path.get(module_path)
            if module is None:
                continue
            expanded |= _module_dependencies(
                module,
                repo,
                modules_by_dotted,
                package_modules,
                export_maps,
            )
        imports_by_file[test_file] = expanded
        naming_by_file[test_file] = _naming_candidates(
            test_file, modules, ambiguities
        )

    analyzer = _EvidenceAnalyzer(
        repo,
        modules,
        modules_by_dotted,
        package_modules,
        export_maps,
    )
    failed_set = set(failed_nodes)

    module_defaults: dict[str, list[str]] = {}
    seen_stems: dict[str, str] = {}
    file_defaults: dict[str, str] = {}
    for test_file in sorted(nodes_by_file):
        stem = _test_module_key(test_file)
        if stem in seen_stems:
            ambiguities.append(
                {
                    "kind": "duplicate_test_stem",
                    "test_file": test_file,
                    "detail": (
                        f"测试文件 stem {stem!r} 与 {seen_stems[stem]} 重复；"
                        "module_defaults 只保留先出现的文件，节点级覆盖不受影响。"
                    ),
                }
            )
            continue
        seen_stems[stem] = test_file

        imports = imports_by_file.get(test_file, set())
        naming = naming_by_file.get(test_file, set())
        nodes = sorted(nodes_by_file.get(test_file, []))
        file_default = _file_primary(
            test_file,
            nodes,
            imports,
            naming,
            traceback_by_node,
            modules_by_path,
            _FALLBACK_ID,
        )
        file_defaults[test_file] = file_default
        primary_ids = _candidate_ids([file_default], module_to_specialist)
        module_defaults[stem] = [primary_ids[0] if primary_ids else _FALLBACK_ID]

    node_overrides: dict[str, dict] = {}
    dispatch_basis: dict[str, dict] = {}
    for test_file in sorted(nodes_by_file):
        imports = imports_by_file.get(test_file, set())
        naming = naming_by_file.get(test_file, set())
        allowed = set(imports) | set(naming)
        file_default = file_defaults.get(test_file, _FALLBACK_ID)
        for node in sorted(nodes_by_file[test_file]):
            # Rule 3: only currently failing nodes may get an override.
            if node not in failed_set:
                continue
            frame_locs = frame_locs_by_node.get(node, ())
            # GEN5 rule A1: package-root frames are relays.  When the node
            # has a non-__init__ package frame, drop every __init__ frame
            # (and its line locs) before traceback attribution.
            trace_paths = _drop_relay_init_frames(
                path
                for path in frames_by_node.get(node, ())
                if path in source_paths
            )
            kept_paths = set(trace_paths)
            package_locs = tuple(
                (path, line)
                for path, line in frame_locs
                if path in kept_paths
            )
            coupling_modules: set[str] = set()
            if trace_paths:
                candidates = set(trace_paths)
                # A naming-miss / integration test may legitimately fail
                # through a coupled module reached by the raising frame.
                if _is_coupling_node(node):
                    coupling_modules = (
                        set(
                            analyzer.raise_chain_modules(
                                package_locs, source_paths
                            )
                        )
                        - candidates
                    )
                    candidates |= coupling_modules
                imports_not_used = imports - candidates
                rationale = _traceback_rationale(
                    trace_paths,
                    package_locs,
                    coupling_modules,
                    imports_not_used,
                )
                source = (
                    "core.autoprofile 自动扫描：失败证据的副本 pytest "
                    "traceback 包内帧；import 命中只写 rationale，"
                    "不参与 override。"
                )
                basis_kind = "traceback"
            else:
                static_modules = (
                    analyzer.static_modules(test_file, node, allowed)
                    if allowed
                    else ()
                )
                used_all_imports = not static_modules
                if static_modules:
                    candidates = set(static_modules)
                else:
                    candidates = set(imports) | set(naming)
                imports_not_used = imports - candidates
                rationale = _import_fallback_rationale(
                    candidates, imports_not_used, used_all_imports
                )
                source = (
                    "core.autoprofile 自动扫描：该失败 node 无可用包内 "
                    "traceback 帧，按测试函数断言/静态 import 证据回退；"
                    "module_defaults 生成逻辑不变。"
                )
                basis_kind = "no_traceback"
                ambiguities.append(
                    {
                        "kind": "no_traceback_import_dispatch",
                        "test_file": test_file,
                        "node_id": node,
                        "detail": (
                            "无 traceback、按 import 派单：未发现该失败 "
                            "node 的包内 traceback 帧；候选 "
                            f"{_format_paths(candidates)}。"
                        ),
                    }
                )
            if not candidates:
                ambiguities.append(
                    {
                        "kind": "unmapped_node",
                        "test_file": test_file,
                        "node_id": node,
                        "detail": (
                            "traceback 与 import / 命名约定均未命中源模块；"
                            "该节点走 module_defaults 或 generalist 兜底。"
                        ),
                    }
                )
                continue
            candidate_ids = _candidate_ids(
                candidates, module_to_specialist
            )
            default_ids = _candidate_ids(
                [file_default], module_to_specialist
            )
            default_id = default_ids[0] if default_ids else _FALLBACK_ID
            dispatch_basis[node] = {
                "kind": basis_kind,
                "frames": list(package_locs),
                "candidates": sorted(candidates),
                "imports_not_used": sorted(imports_not_used),
                "rationale": rationale,
                "source": source,
            }
            if len(candidate_ids) == 1 and candidate_ids[0] == default_id:
                continue
            node_overrides[node] = {
                "specialists": list(candidate_ids),
                "rationale": rationale,
                "source": source,
            }

    table = {
        "version": 1,
        "specialists": specialists,
        "node_overrides": node_overrides,
        "module_defaults": module_defaults,
        "fallback": _FALLBACK_ID,
    }

    if vault_package is not None:
        prefix = vault_package.rstrip("/") + "/"
        all_inside = all(
            path.startswith(prefix) for path in roster_entries[_FALLBACK_ID]["files"]
        )
        if all_inside:
            table["roster"] = {
                "package": vault_package,
                "symptoms": "",
                "specialists": roster_entries,
            }
        else:
            # Multi-root repo: the roster package falls back to the
            # repository root ("." names the root; every relative
            # path is inside it).  Omitting the roster would leave
            # native runs without a team.
            table["roster"] = {
                "package": ".",
                "symptoms": "",
                "specialists": roster_entries,
            }
            ambiguities.append(
                {
                    "kind": "roster_package_root",
                    "detail": (
                        "源文件不在单一包根下，roster.package 落在"
                        "仓库根 '.'。"
                    ),
                }
            )

    return table, imports_by_file, naming_by_file, dispatch_basis


def _render_markdown(
    result: ProfileResult,
    table: dict,
    metadata: dict,
    imports_by_file: dict[str, set[str]],
    naming_by_file: dict[str, set[str]],
    dispatch_basis: dict[str, dict],
) -> str:
    specialists = {
        item["id"]: item for item in table["specialists"]
    }
    failed_nodes = list(metadata["failed_nodes"])
    evidence = evidence_from_failed(failed_nodes)
    plan = DispatchKB(table).lookup(evidence)
    assignments_by_node: dict[str, list[str]] = {}
    for actor_id, nodes in plan.assignments.items():
        for node in nodes:
            assignments_by_node.setdefault(node, []).append(actor_id)

    lines = [
        f"# 自动建档报告：{result.name}",
        "",
        f"- 源仓库：`{result.repo_path}`",
        f"- 生成器：`{GENERATOR}` ({GENERATOR_VERSION})",
        f"- profile 版本：{PROFILE_VERSION}",
        f"- profile 来源（origin）：`{metadata.get('origin', 'tests')}`",
        f"- 检测到的包：{'、'.join(f'`{item}`' for item in result.packages) or '无'}",
        f"- 金库 package：`{metadata.get('vault_package')}`",
        f"- 测试：{result.test_total} 条，当前通过 {result.passed_count}，"
        f"失败 {result.failed_count}",
        "",
        "## 检测结果",
        "",
        "### 测试文件",
        "",
    ]
    if metadata["test_files"]:
        lines.extend(
            f"- `{path}`" for path in metadata["test_files"]
        )
    else:
        lines.append("- 未发现测试文件")
    lines.extend(["", "### pytest / conftest 配置", ""])
    if metadata["config_files"]:
        lines.extend(
            f"- `{path}`" for path in metadata["config_files"]
        )
    else:
        lines.append("- 未发现额外配置（按 pytest 默认行为判卷）")

    lines.extend(
        [
            "",
            "## 专家与文件",
            "",
            "| 专家 | 允许文件 | 说明 |",
            "| --- | --- | --- |",
        ]
    )
    for specialist in table["specialists"]:
        files = "、".join(f"`{item}`" for item in specialist["files"])
        lines.append(
            f"| `{specialist['id']}` | {files} | {specialist['blurb']} |"
        )

    lines.extend(
        [
            "",
            "## 失败证据派单",
            "",
            "下表是当前失败集在生成表上的真实 lookup 结果"
            "（node 覆盖 → 模块默认 → fallback）。node 覆盖只对当前"
            "失败节点生成：优先采用 traceback 包内帧；无可用帧时才回退 "
            "import / 静态证据。",
            "",
            "| 失败 node | 模块 | 派给 | 覆盖文件 | 派单依据 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in evidence:
        actor_ids = assignments_by_node.get(item.node_id, [])
        files = sorted(
            {
                path
                for actor_id in actor_ids
                for path in specialists[actor_id]["files"]
            }
        )
        basis = dispatch_basis.get(item.node_id, {})
        basis_text = basis.get("rationale") or "—"
        lines.append(
            f"| `{item.node_id}` | `{item.module}` | "
            f"{'、'.join(f'`{actor}`' for actor in actor_ids) or '无'} | "
            f"{'、'.join(f'`{path}`' for path in files) or '无'} | "
            f"{basis_text} |"
        )

    lines.extend(
        [
            "",
            "## 测试文件映射摘要",
            "",
            "| 测试文件 | import 命中（含一层包内依赖） | 命名约定 | 文件默认专家 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for test_file in metadata["test_files"]:
        imports = "、".join(
            f"`{path}`" for path in sorted(imports_by_file.get(test_file, set()))
        ) or "无"
        naming = "、".join(
            f"`{path}`" for path in sorted(naming_by_file.get(test_file, set()))
        ) or "未命中"
        stem = _test_module_key(test_file)
        default = "、".join(
            f"`{item}`" for item in table["module_defaults"].get(
                stem, [table["fallback"]]
            )
        )
        lines.append(
            f"| `{test_file}` | {imports} | {naming} | {default} |"
        )

    lines.extend(["", "## 多派与公共文件说明", ""])
    overlap_notes: list[str] = []
    generalist_files = set(
        specialists[table["fallback"]]["files"]
    )
    for specialist in table["specialists"]:
        if specialist["id"] == table["fallback"]:
            continue
        shared = generalist_files & set(specialist["files"])
        if shared:
            overlap_notes.append(
                f"`{specialist['id']}` 与通用修理工共享 "
                + "、".join(f"`{item}`" for item in sorted(shared))
                + "（兜底专家需要覆盖全部源文件，属预期重叠）。"
            )
    multi_nodes = [
        node for node, rule in table["node_overrides"].items()
        if len(rule["specialists"]) > 1
    ]
    if multi_nodes:
        overlap_notes.append(
            f"多派节点 {len(multi_nodes)} 条；每条的多派候选均来自 "
            "`node_overrides` 的 traceback 包内帧或显式耦合补派。"
        )
    overlap_notes.append(
        "`node_overrides` 只对当前失败节点生成；有 traceback 时只取"
        "包内帧，AST import 命中只写入 rationale，不参与派工。"
    )
    overlap_notes.append(
        "无可用 traceback 帧的失败节点回退 import / 测试函数断言静态"
        "证据，并在「歧义与未把握处」逐条标注「无 traceback、按 import "
        "派单」。"
    )
    overlap_notes.append(
        "包根转手不算根因：某失败 node 的包内 traceback 帧中只要存在非 "
        "`__init__.py` 帧，该 node 的全部 `__init__.py` 帧即被丢弃后再派单。"
    )
    overlap_notes.append(
        "`from <包> import <名字>` 经包 `__init__` 的再导出语句解析到真正"
        "定义该名字的子模块；解析不到的名字不计入任何模块、只记歧义，"
        "绝不回退成整包派单。"
    )
    overlap_notes.append(
        "`module_defaults` 生成逻辑未变：仍按测试文件命名约定与 import "
        "首选模块给出文件默认专家。"
    )
    if not overlap_notes:
        overlap_notes.append("没有额外共享文件。")
    lines.extend(f"- {note}" for note in overlap_notes)

    lines.extend(["", "## 歧义与未把握处", ""])
    if result.ambiguities:
        for item in result.ambiguities:
            detail = item.get("detail", "")
            target = item.get("node_id") or item.get("test_file") or ""
            suffix = f"（{target}）" if target else ""
            lines.append(f"- `{item.get('kind')}`{suffix}：{detail}")
    else:
        lines.append("- 无；三路证据对所有测试均给出至少一个候选模块。")

    lines.extend(
        [
            "",
            "## 确定性说明",
            "",
            "`dispatch_table.json` 的专家、节点覆盖、模块默认与文件列表均按"
            "固定顺序排序，且不写入时间戳；同一仓库同一状态重复建档，"
            "派单表逐字一致。",
            "",
        ]
    )
    return "\n".join(lines)


def build_profile(
    repo_path: str | Path,
    out_root: str | Path = DEFAULT_OUT_ROOT,
    name: str | None = None,
    origin: str = "tests",
) -> ProfileResult:
    """Build a complete cifix profile for *repo_path* without any model call.

    The source repository is never written to: discovery only reads it, and
    the evidence run happens on a temporary copy.  The profile directory is
    rewritten from scratch so stale specialists cannot survive a rebuild.

    ``origin`` records where the profile came from.  ``"tests"`` (the
    default) is a repository with a real test suite; ``"repro"`` is the
    repro-first path in :mod:`core.repro`, where a single triage-authored
    repro test is the only safety net.  The value is written to
    ``profile.json``; :mod:`core.runner` uses only ``"repro"`` to switch on
    the extra write-scope guard, so profiles without the field keep the old
    behaviour byte-for-byte.
    """
    repo = Path(repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise FileNotFoundError(f"repository not found: {repo}")

    out_path = Path(out_root).expanduser()
    if not out_path.is_absolute():
        out_path = (PLATFORM_ROOT / out_path).resolve()
    # ``drbug`` historically handed the TUI workspace root + "profiles".
    # For an external project that must land in <repo>/.drbug, not in the
    # user's source tree.  The env var is only set while the TUI is running.
    chat_root = os.environ.get(_CHAT_WORKSPACE_ENV, "").strip()
    if chat_root:
        try:
            chat_root_path = Path(chat_root).expanduser().resolve()
            if out_path.resolve() == (chat_root_path / "profiles").resolve():
                out_path = state_root_for(repo) / "profiles"
        except OSError:
            pass
    profile_name = name or default_profile_name(repo)
    profile_name = _sanitise_identifier(profile_name)
    profile_dir = out_path / profile_name

    ambiguities: list[dict] = []
    packages, modules = _discover_packages(repo, ambiguities)
    test_files, config_files = _discover_test_files(repo)
    if not test_files:
        # F2: no Python tests — a JS/TS or Go repo gets a same-shaped
        # profile from the language builder instead of a dead end.
        from .autoprofile_lang import build_profile_lang, detect_language

        language = detect_language(repo)
        if language is not None:
            return build_profile_lang(
                repo,
                profile_dir=profile_dir,
                profile_name=profile_name,
                origin=origin,
                language=language,
            )
        raise FileNotFoundError(
            f"no test files found in {repo} "
            "(expected tests/test_*.py, tests/*_test.py or root test_*.py)"
        )

    vault_package = _choose_vault_package(packages, ambiguities)
    if vault_package is None:
        raise ValueError(
            f"no package directory found in {repo}; cannot build a vault"
        )

    evidence = _run_pytest_on_copy(repo)
    if evidence["test_total"] == 0:
        ambiguities.append(
            {
                "kind": "pytest_no_tests",
                "detail": (
                    "副本 pytest 没有收集到任何测试"
                    f"（exit={evidence.get('returncode')}）；"
                    "派单表只有静态分析结果。"
                ),
            }
        )

    profile_dir.mkdir(parents=True, exist_ok=True)
    vault_dir = profile_dir / "vault"
    manifest = _build_vault(
        repo,
        vault_dir,
        vault_package,
        packages,
        test_files,
        config_files,
    )

    table, imports_by_file, naming_by_file, dispatch_basis = _build_table(
        repo,
        modules,
        test_files,
        evidence["all_nodes"],
        evidence["failed_nodes"],
        evidence["frames_by_node"],
        evidence["frame_locs_by_node"],
        ambiguities,
        vault_package,
    )

    # TEAM1-fix1: 老板增删覆盖层（roster_overlay.json 不是建档产物、
    # 本函数从不写它）。删掉的人不复活、加的人保留；没有覆盖层时
    # apply_overlay 返回空且不动 table。
    from .roster import apply_overlay

    ambiguities.extend(apply_overlay(profile_dir, table))

    # Stable ambiguity ordering keeps profile.json and PROFILE.md repeatable.
    ambiguities.sort(
        key=lambda item: (
            item.get("kind", ""),
            item.get("test_file", ""),
            item.get("node_id", ""),
            item.get("detail", ""),
        )
    )
    failed_nodes = sorted(evidence["failed_nodes"])
    # GEN3: the dispatch table's ``specialists`` list covers every non-
    # ``__init__`` source module, but it is keyed by specialist id and its
    # top-level schema is frozen by the GEN2 tests.  Dynamic teaming needs an
    # evidence-module -> (path, docstring) map instead, so it lives in
    # ``profile.json`` as a new field; no existing field changes meaning.
    inventory: dict[str, dict[str, str]] = {}
    for module in modules:
        if module.stem in inventory:
            continue
        inventory[module.stem] = {
            "path": module.path,
            "blurb": _module_docstring(repo / module.path),
        }
    metadata = {
        "name": profile_name,
        "source_repo": str(repo),
        "packages": list(packages),
        "vault_package": vault_package,
        "test_files": list(test_files),
        "config_files": list(config_files),
        "test_total": evidence["test_total"],
        "passed_count": evidence["passed_count"],
        "failed_count": evidence["failed_count"],
        "failed_nodes": failed_nodes,
        "ambiguities": [dict(item) for item in ambiguities],
        "specialists": [item["id"] for item in table["specialists"]],
        "inventory": inventory,
        "origin": origin,
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "profile_version": PROFILE_VERSION,
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
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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
        packages=list(packages),
        test_total=evidence["test_total"],
        passed_count=evidence["passed_count"],
        failed_count=evidence["failed_count"],
        ambiguities=[dict(item) for item in ambiguities],
        dispatch_table=table,
        metadata=metadata,
    )
    result.markdown_path.write_text(
        _render_markdown(
            result,
            table,
            {**metadata, "vault_package": manifest.get("package")},
            imports_by_file,
            naming_by_file,
            dispatch_basis,
        )
        + "\n",
        encoding="utf-8",
    )
    # MEM1: one IDENTITY.md / MEMORY.md per roster member. Missing files
    # only — a hand-edited archive is never rewritten. No model call.
    from .agent_archive import ensure_archives

    ensure_archives(profile_dir, table.get("specialists") or [])
    versioning.commit("重建档带来的定义变化（按仓库重新生成派单表）", kind="rebuild")
    return result


__all__ = [
    "DEFAULT_OUT_ROOT",
    "GENERATOR",
    "GENERATOR_VERSION",
    "PROFILES_ROOT",
    "PROFILE_VERSION",
    "ProfileResult",
    "SourceModule",
    "build_profile",
    "default_profile_name",
    "state_root_for",
    "work_root_for",
]
