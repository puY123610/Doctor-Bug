#!/usr/bin/env python3
"""Environment self-check for the AgentScope 2.0.11 project baseline.

Only the Python standard library is used. Imports are performed for real, so
the class lines below reflect what is actually installed in the running
interpreter, not hard-coded assumptions.
"""

from __future__ import annotations

import importlib
import sys

REQUIRED_AGENTSCOPE_VERSION = "2.0.11"

KEY_CLASSES = (
    ("Agent", "agentscope.agent", "Agent"),
    ("GoalPipeline", "agentscope.pipeline", "GoalPipeline"),
    ("TeamPipeline", "agentscope.pipeline", "TeamPipeline"),
)

STUB_SCAN_MODULES = (
    "agentscope.agent",
    "agentscope.pipeline",
    "agentscope.model",
)


def main() -> int:
    print(f"Python: {sys.version}")

    agentscope_version = None
    try:
        agentscope = importlib.import_module("agentscope")
    except Exception as exc:  # noqa: BLE001 - report the real import failure
        print(f"agentscope: MISSING ({type(exc).__name__}: {exc})")
    else:
        agentscope_version = getattr(agentscope, "__version__", None)
        print(f"agentscope: OK (version {agentscope_version})")

    for label, module_name, attribute in KEY_CLASSES:
        try:
            module = importlib.import_module(module_name)
            obj = getattr(module, attribute)
        except Exception as exc:  # noqa: BLE001 - report the real import failure
            print(
                f"{label}: MISSING "
                f"({module_name}.{attribute}; {type(exc).__name__}: {exc})"
            )
        else:
            print(f"{label}: OK ({obj.__module__}.{obj.__qualname__})")

    stub_or_mock_names = []
    for module_name in STUB_SCAN_MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - optional module; required classes are checked above
            continue
        stub_or_mock_names.extend(
            f"{module_name}.{name}"
            for name in dir(module)
            if "stub" in name.lower() or "mock" in name.lower()
        )
    print(
        "Stub/Mock public names (agent/pipeline/model): "
        + ("NONE" if not stub_or_mock_names else ", ".join(stub_or_mock_names))
    )

    if agentscope_version == REQUIRED_AGENTSCOPE_VERSION:
        print("ENV_CHECK_PASS")
        return 0

    print("ENV_CHECK_FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
