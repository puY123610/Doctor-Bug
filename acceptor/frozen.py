"""Freeze the judging surface and verify repositories against its manifest.

The judging surface is deliberately small: ``tests/test_*.py``, the repository
root ``conftest.py`` and ``pytest.ini``.  Those are the only files an executor
must not be able to change without being detected.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

from .parsers import get_parser

MANIFEST_NAME = "manifest.json"
DEFAULT_PACKAGE = "backoffice"
DEFAULT_CHECK_COMMAND = ["{python}", "-m", "pytest", "-q"]
_ROOT_JUDGING_FILES = (
    "conftest.py",
    "pytest.ini",
    "package.json",
    "jest.config.js",
    "jest.config.ts",
    "vitest.config.ts",
    "vitest.config.js",
    "tsconfig.json",
    "go.mod",
    "go.sum",
)

_JS_TEST_PATTERNS = (
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.js",
    "test.ts",
    "test.tsx",
    "test.js",
    "test.jsx",
)


def normalise_package(package: str | Path) -> str:
    """Return a safe repository-relative package path.

    The value is stored in ``manifest.json`` and later joined to a workspace
    path, so absolute paths and ``..`` segments must be rejected instead of
    silently escaping the workspace.
    """
    raw = str(package).replace("\\", "/").strip()
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"invalid package path: {package!r}")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise ValueError(f"invalid package path: {package!r}")
    return "/".join(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def judging_surface(repo: Path) -> list[Path]:
    """Return the judging files present in *repo*, sorted by relative path.

    Python: root conftest.py/pytest.ini + tests/test_*.py.
    JS/TS: root configs + *.test.* / *.spec.* under src/, tests/,
    __tests__/ or the repo root. Go: root go.mod + *_test.go
    anywhere. node_modules/.git never count.
    """
    repo = Path(repo)
    files: set[Path] = set()
    for name in _ROOT_JUDGING_FILES:
        candidate = repo / name
        if candidate.is_file():
            files.add(candidate)
    tests_dir = repo / "tests"
    if tests_dir.is_dir():
        files.update(
            path for path in tests_dir.glob("test_*.py") if path.is_file()
        )
    for base in (repo / "src", tests_dir, repo / "__tests__", repo):
        if not base.is_dir():
            continue
        for pattern in _JS_TEST_PATTERNS:
            for path in base.rglob(pattern):
                if path.is_file() and "node_modules" not in path.parts:
                    files.add(path)
    for path in repo.rglob("*_test.go"):
        if (
            path.is_file()
            and "node_modules" not in path.parts
            and ".git" not in path.parts
        ):
            files.add(path)
    return sorted(files, key=lambda path: path.relative_to(repo).as_posix())


def freeze(
    source_repo: Path,
    vault_dir: Path,
    package: str | Path = DEFAULT_PACKAGE,
    *,
    check_command: list[str] | None = None,
    parser: str = "pytest",
    hidden: list[str] | None = None,
) -> dict:
    """Copy the judging surface into *vault_dir* and write ``manifest.json``.

    *package* is the repository-relative directory that holds the code under
    test (``backoffice`` for the demo, ``src/studyroom`` for a src layout).
    It is recorded in the manifest so ``check`` can copy the right package
    into the temporary judging directory.  The default keeps demo vaults and
    older manifests behaving exactly as before.

    The vault is fully owned by this command: an existing directory is
    replaced so a stale test file can never survive a re-freeze.
    """
    source_repo = Path(source_repo)
    vault_dir = Path(vault_dir)
    package = normalise_package(package)
    if not source_repo.is_dir():
        raise FileNotFoundError(f"source repository not found: {source_repo}")

    surface = judging_surface(source_repo)
    if not surface:
        raise FileNotFoundError(
            f"no judging files found in {source_repo} "
            "(expected tests/test_*.py, conftest.py or pytest.ini)"
        )

    if vault_dir.exists():
        shutil.rmtree(vault_dir)
    vault_dir.mkdir(parents=True)

    hidden_rels: set[str] = set()
    for item in hidden or []:
        rel = Path(item).as_posix()
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"hidden path must be repo-relative: {item!r}")
        if not (source_repo / rel).is_file():
            raise FileNotFoundError(f"hidden file not in source repo: {item!r}")
        hidden_rels.add(rel)

    files: dict[str, str] = {}
    hidden_files: dict[str, str] = {}
    surface_rels = {
        source.relative_to(source_repo).as_posix() for source in surface
    }
    for rel in sorted(surface_rels | hidden_rels):
        source = source_repo / rel
        target = vault_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if rel in hidden_rels:
            hidden_files[rel] = _sha256(target)
        else:
            files[rel] = _sha256(target)

    get_parser(parser)  # 未知解析器在冻结时就拒绝，不许冻进金库
    command = list(check_command) if check_command else list(
        DEFAULT_CHECK_COMMAND
    )
    if not command or not all(
        isinstance(token, str) and token for token in command
    ):
        raise ValueError(f"invalid check command: {check_command!r}")
    manifest = {
        "files": files,
        "package": package,
        "source": str(source_repo.resolve()),
        "check_command": command,
        "parser": parser,
        "kind": "tests",
    }
    if hidden_files:
        manifest["hidden_files"] = hidden_files
    (vault_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_against_manifest(
    manifest: dict, candidate_repo: Path, *, strict_hidden: bool = False
) -> list[str]:
    """Return sorted relative paths that differ from *manifest*.

    A path is reported when its bytes changed, when a frozen file is missing,
    or when the candidate contains an extra judging file. Hidden files
    (``hidden_files``) are vault-only: their absence in the candidate is
    fine (unless *strict_hidden*, used for vault integrity), but a copy
    with different bytes is tampering, and they never count as extras.
    """
    candidate_repo = Path(candidate_repo)
    expected = manifest.get("files", {})
    hidden = manifest.get("hidden_files", {})
    mismatched: set[str] = set()

    for relative, expected_hash in expected.items():
        candidate = candidate_repo / relative
        if not candidate.is_file():
            mismatched.add(relative)
        elif _sha256(candidate) != expected_hash:
            mismatched.add(relative)

    for relative, expected_hash in hidden.items():
        candidate = candidate_repo / relative
        if not candidate.is_file():
            if strict_hidden:
                mismatched.add(relative)
        elif _sha256(candidate) != expected_hash:
            mismatched.add(relative)

    for candidate in judging_surface(candidate_repo):
        relative = candidate.relative_to(candidate_repo).as_posix()
        if relative not in expected and relative not in hidden:
            mismatched.add(relative)

    return sorted(mismatched)


def freeze_behavior(
    source_repo: Path,
    vault_dir: Path,
    script: str | Path,
    package: str | Path = DEFAULT_PACKAGE,
) -> dict:
    """冻结一个行为检查脚本：脚本进金库受哈希保护，判卷命令
    就是用当前 Python 跑它，退出 0 为 PASS、非 0 为 FAIL。

    script 必须是 source_repo 内的文件（相对路径按仓库根
    解析）。金库整体替换，与 freeze 同规矩。
    """

    source_repo = Path(source_repo)
    vault_dir = Path(vault_dir)
    package = normalise_package(package)
    if not source_repo.is_dir():
        raise FileNotFoundError(f"source repository not found: {source_repo}")
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = source_repo / script_path
    script_path = script_path.resolve()
    try:
        script_path.relative_to(source_repo.resolve())
    except ValueError:
        raise ValueError(
            f"behavior script must live inside the source repo: {script}"
        ) from None
    if not script_path.is_file():
        raise FileNotFoundError(f"behavior script not found: {script_path}")

    if vault_dir.exists():
        shutil.rmtree(vault_dir)
    vault_dir.mkdir(parents=True)

    name = script_path.name
    target = vault_dir / name
    shutil.copyfile(script_path, target)
    manifest = {
        "files": {name: _sha256(target)},
        "package": package,
        "source": str(source_repo.resolve()),
        "check_command": ["{python}", name],
        "parser": "exitcode",
        "kind": "behavior",
    }
    (vault_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
