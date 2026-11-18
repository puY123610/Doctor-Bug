"""Command line interface: ``python -m acceptor freeze|check``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .acceptor import FrozenAcceptor
from .frozen import DEFAULT_PACKAGE, freeze, freeze_behavior

DEFAULT_VAULT = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "acceptor_vault"
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acceptor",
        description="Freeze a judging suite and accept workspaces against it.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze_parser = subparsers.add_parser(
        "freeze", help="copy the judging surface into a vault"
    )
    freeze_parser.add_argument("--source", required=True, type=Path)
    freeze_parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    freeze_parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=(
            "repository-relative package directory to copy during check "
            f"(default: {DEFAULT_PACKAGE})"
        ),
    )
    freeze_parser.add_argument(
        "--check-command",
        default=None,
        help=(
            "judging command frozen into the manifest, as one "
            "space-separated string; {python} expands to the "
            "current interpreter (default: {python} -m pytest -q)"
        ),
    )
    freeze_parser.add_argument(
        "--parser",
        default="pytest",
        help="output parser frozen into the manifest (default: pytest)",
    )
    freeze_parser.add_argument(
        "--hidden",
        action="append",
        default=None,
        help="repo-relative test file frozen as vault-only (repeatable)",
    )

    behavior_parser = subparsers.add_parser(
        "freeze-behavior",
        help="freeze a behavior-check script as the judging surface",
    )
    behavior_parser.add_argument("--source", required=True, type=Path)
    behavior_parser.add_argument("--script", required=True)
    behavior_parser.add_argument(
        "--vault", type=Path, default=DEFAULT_VAULT
    )
    behavior_parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=(
            "repository-relative package directory to copy during check "
            f"(default: {DEFAULT_PACKAGE})"
        ),
    )

    check_parser = subparsers.add_parser(
        "check", help="judge a workspace with the frozen vault"
    )
    check_parser.add_argument("--workspace", required=True, type=Path)
    check_parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "freeze":
        command = (
            args.check_command.split() if args.check_command else None
        )
        manifest = freeze(
            args.source,
            args.vault,
            package=args.package,
            check_command=command,
            parser=args.parser,
            hidden=args.hidden,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "freeze-behavior":
        manifest = freeze_behavior(
            args.source,
            args.vault,
            script=args.script,
            package=args.package,
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    acceptor = FrozenAcceptor(args.vault)
    verdict = acceptor.check(args.workspace)
    print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
