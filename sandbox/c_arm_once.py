"""Run one scripted C-arm repair on demo_repo inside the sandbox copy.

This is a thin caller of ``core.runner.run_arm``; it changes nothing in the
kernel.  It exits 0 only when the acceptor's final verdict is PASS and prints
the repaired lines as evidence.
"""

from __future__ import annotations

import difflib
import sys
import tempfile
from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from core.runner import run_arm  # noqa: E402

DEMO_REPO = PLATFORM_ROOT / "demo_repo"
REPAIRED_FILES = (
    "backoffice/billing.py",
    "backoffice/inventory.py",
    "backoffice/auth.py",
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sandbox-c-arm-") as temporary:
        work_root = Path(temporary) / "work"
        record = run_arm("C", DEMO_REPO, True, 6, work_root)
        print(
            f"[c-arm] outcome={record.outcome} rounds={len(record.rounds)} "
            f"final_failed={len(record.final_failed)}"
        )
        for round_record in record.rounds:
            print(
                f"[c-arm] round {round_record['round']}: "
                f"status={round_record['status']} "
                f"failed={len(round_record['failed_tests'])}"
            )
            for result in round_record["fix_results"]:
                print(
                    f"[c-arm]   {result['actor_id']} applied={result['applied']} "
                    f"note={result['note']}"
                )
        if record.outcome != "PASS":
            print(f"[c-arm] C_ARM_FINAL FAIL outcome={record.outcome}")
            return 1

        workspace = next(work_root.glob("c-kb-*"))
        print(f"[c-arm] repaired workspace={workspace}")
        for relative in REPAIRED_FILES:
            original = (DEMO_REPO / relative).read_text(encoding="utf-8")
            repaired = (workspace / relative).read_text(encoding="utf-8")
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                repaired.splitlines(keepends=True),
                fromfile=f"demo_repo/{relative}",
                tofile=f"workspace/{relative}",
            )
            print("".join(diff), end="")
        print("[c-arm] C_ARM_FINAL PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
