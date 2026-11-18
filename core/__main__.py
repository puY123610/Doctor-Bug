"""CLI: ``python -m core run --arm C --workspace <repo> --vault <vault> --kb <table> --out runs/c.json``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one dispatch arm")
    run_parser.add_argument("--arm", required=True, choices=["A", "B", "C"])
    run_parser.add_argument("--workspace", required=True, type=Path)
    run_parser.add_argument("--out", required=True, type=Path)
    kb_group = run_parser.add_mutually_exclusive_group()
    kb_group.add_argument("--kb-enabled", dest="kb_enabled", action="store_true")
    kb_group.add_argument("--no-kb", dest="kb_enabled", action="store_false")
    run_parser.set_defaults(kb_enabled=True)
    run_parser.add_argument("--max-rounds", type=int, default=6)
    run_parser.add_argument("--work-root", type=Path, default=Path("runs") / "work")
    run_parser.add_argument(
        "--vault",
        type=Path,
        required=True,
        help="验收金库目录（档案内的 vault/）",
    )
    run_parser.add_argument(
        "--kb",
        type=Path,
        required=True,
        help="派单表路径（档案内的 dispatch_table.json）",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        vault_dir, kb_path = args.vault, args.kb
        record = run_arm(
            arm=args.arm,
            workspace_src=args.workspace,
            kb_enabled=args.kb_enabled,
            max_rounds=args.max_rounds,
            work_root=args.work_root,
            actor_backend="agentscope",
            vault_dir=vault_dir,
            kb_path=kb_path,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"{record.arm} kb_enabled={record.kb_enabled} -> {record.outcome} "
            f"rounds={len(record.rounds)} final_failed={len(record.final_failed)}"
        )
        return 0

    parser.error(f"unknown command: {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
