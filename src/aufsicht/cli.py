"""Command surface (distribution spec §14).

    aufsicht fast      → task runner exposes it as quality-fast
    aufsicht full      → quality-full
    aufsicht fix       → quality-fix
    aufsicht init
    aufsicht upgrade

Gate exit codes per v5.1 §15 (0/1/2/3); init exit codes per
distribution spec §5.2 (0/1/2/3) — distinct meanings for a distinct
command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .errors import AufsichtError, ToolingError


def _repo_path(value: str | None) -> Path:
    return Path(value).resolve() if value else Path.cwd()


def _cmd_gate(mode: str, args: argparse.Namespace) -> int:
    from . import adapters  # noqa: F401 — registers gates
    from .pipeline import run_pipeline
    from .report import human_summary

    report = run_pipeline(_repo_path(args.repo), mode)
    print(json.dumps(report.to_dict(), indent=2))
    print(human_summary(report), file=sys.stderr)
    return report.exit_code


def _cmd_fix(args: argparse.Namespace) -> int:
    from . import adapters  # noqa: F401
    from .pipeline import build_context

    repo = _repo_path(args.repo)
    try:
        ctx = build_context(repo, "fix")
        from .adapters.ruff import apply_fixes
        n = apply_fixes(ctx)
        print(f"quality-fix: applied {n} fix(es); re-run quality-fast")
    except ToolingError as exc:
        print(f"tooling error: {exc}", file=sys.stderr)
        if exc.remedy:
            print(f"  remedy: {exc.remedy}", file=sys.stderr)
        return 3
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from .init.cli import run_init

    return run_init(
        repo=_repo_path(args.repo),
        dry_run=args.dry_run,
        force=args.force,
        as_json=args.json,
        write=args.write,
    )


def _cmd_upgrade(args: argparse.Namespace) -> int:
    from .upgrade import run_upgrade

    return run_upgrade(repo=_repo_path(args.repo), as_json=args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aufsicht",
        description="Deterministic AI code quality guardrails (spec v5.1, Tier 1).",
    )
    parser.add_argument("--version", action="version", version=f"aufsicht {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_repo(p):
        p.add_argument("--repo", help="repository root (default: cwd)")
        return p

    with_repo(sub.add_parser("fast", help="quality-fast: read-only, diff-scoped, <15s"))
    with_repo(sub.add_parser("full", help="quality-full: read-only, whole repo, ratchets"))
    with_repo(sub.add_parser("fix", help="quality-fix: MAY mutate (never in CI)"))
    p_upgrade = with_repo(sub.add_parser(
        "upgrade", help="print the .quality/ diff this runner would apply"))
    p_upgrade.add_argument("--json", action="store_true",
                           help="machine-readable diff output")

    p_init = with_repo(sub.add_parser("init", help="Layer 2: install guardrails into a repo"))
    p_init.add_argument("--dry-run", action="store_true",
                        help="stop after propose (default when stdout is not a TTY)")
    p_init.add_argument("--write", action="store_true",
                        help="proceed to the write phase even when stdout is not a TTY")
    p_init.add_argument("--force", action="store_true",
                        help="overwrite an existing installation (refuses a dirty tree)")
    p_init.add_argument("--json", action="store_true",
                        help="machine-readable plan/output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fast":
            return _cmd_gate("fast", args)
        if args.command == "full":
            return _cmd_gate("full", args)
        if args.command == "fix":
            return _cmd_fix(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "upgrade":
            return _cmd_upgrade(args)
    except AufsichtError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if getattr(exc, "remedy", None):
            print(f"  remedy: {exc.remedy}", file=sys.stderr)
        return 3
    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
