# -*- coding: utf-8 -*-
"""Installed-tree reconciliation: every file the runtime reads must be
in the package, and every shipped module must import cleanly from the
installed tree (the catalog.json lesson, turned into a gate)."""
import importlib
import pkgutil
import sys
from pathlib import Path

root = Path(sys.argv[1])  # <prefix>/lib/node_modules/drbug/platform

required = [
    "market/catalog.json",
    "market/skills/repo-triage/SKILL.md",
    "market/skills/repo-triage/manifest.json",
    "model_config.example.json",
    "requirements.txt",
    "pyproject.toml",
    "core/setup_wizard.py",
    "core/tui.py",
    "core/cli.py",
]
missing = [rel for rel in required if not (root / rel).is_file()]
print("MISSING:", missing)
assert not missing, missing

failures = []
for package in ("core", "acceptor", "agentscope_ext"):
    module = importlib.import_module(package)
    for info in pkgutil.iter_modules(module.__path__):
        name = f"{package}.{info.name}"
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report them all
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
print("IMPORT_FAILURES:", failures)
assert not failures, failures
print("TREE_OK")
