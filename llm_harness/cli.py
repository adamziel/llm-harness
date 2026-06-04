"""Command-line interface for ./harness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import db
from .indexer import refresh_index
from .janitor import run_janitor
from .mcp_server import main as mcp_main
from .scheduler import HarnessScheduler, nixos_service, watchdog_loop, watchdog_service
from .status import dashboard, refresh_reports
from .testing_loop import run_tests_once


def main(argv: list[str] | None = None) -> int:
    """Dispatch harness subcommands while keeping the top-level script tiny."""

    parser = argparse.ArgumentParser(prog="harness", description="Deterministic Codex agent harness")
    parser.add_argument("--root", default=os.getcwd(), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True, metavar="{run,status,poke}")

    run = sub.add_parser("run", help="Start or resume the scheduler")
    run.add_argument("--goal", help="Goal to record on first start")
    run.add_argument("--team", default="auto", choices=sorted(["auto", "planning", "building", "minimal"]), help=argparse.SUPPRESS)
    run.add_argument("--once", action="store_true", help=argparse.SUPPRESS)

    status = sub.add_parser("status", help="Show the Unicode/ANSI dashboard")
    status.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)

    poke = sub.add_parser("poke", help="Inject a prompt into running agents")
    poke.add_argument("message")
    poke.add_argument("--target", default="broadcast", help="Agent name, role, or broadcast")

    _hidden_command(sub, "update-status")
    _hidden_command(sub, "janitor")

    test_loop = _hidden_command(sub, "test-loop")
    test_loop.add_argument("--once", action="store_true", help=argparse.SUPPRESS)

    _hidden_command(sub, "mcp")
    _hidden_command(sub, "index")

    watchdog = _hidden_command(sub, "watchdog")
    watchdog.add_argument("--once", action="store_true")

    install = _hidden_command(sub, "install-watchdog")
    install.add_argument("--format", choices=["systemd", "nixos"], default="systemd")

    _hidden_command(sub, "mcp-config")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    paths = db.bootstrap(root)

    if args.command == "run":
        return HarnessScheduler(root).run(goal=args.goal, team=args.team, once=args.once)
    if args.command == "status":
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            if args.refresh or not (root / "STATUS.md").exists():
                refresh_reports(conn, root)
            print(dashboard(conn))
        return 0
    if args.command == "poke":
        HarnessScheduler(root).poke(args.message, args.target)
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            refresh_reports(conn, root)
            print(dashboard(conn))
        return 0
    if args.command == "update-status":
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            md, html = refresh_reports(conn, root)
            db.log_event(conn, "status", f"Updated {md.name} and {html.name}")
        print(f"Updated {md} and {html}")
        return 0
    if args.command == "janitor":
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            result = run_janitor(conn, root)
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "test-loop":
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            run_id = run_tests_once(conn, root)
        print(f"Recorded test run {run_id}")
        return 0
    if args.command == "mcp":
        return mcp_main(["--root", str(root), "--db", str(paths.db)])
    if args.command == "index":
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            count = refresh_index(conn, root)
            db.log_event(conn, "index", f"Indexed {count} files")
        print(f"Indexed {count} files")
        return 0
    if args.command == "watchdog":
        return watchdog_loop(root, once=args.once)
    if args.command == "install-watchdog":
        print(watchdog_service(root) if args.format == "systemd" else nixos_service(root))
        return 0
    if args.command == "mcp-config":
        print(json.dumps(_mcp_config(root), indent=2))
        return 0
    parser.error(f"Unhandled command {args.command}")
    return 2


def _hidden_command(subparsers: argparse._SubParsersAction, name: str) -> argparse.ArgumentParser:
    """Register an internal command without exposing it in public help output."""

    parser = subparsers.add_parser(name, help=argparse.SUPPRESS)
    subparsers._choices_actions = [action for action in subparsers._choices_actions if action.dest != name]
    return parser


def _mcp_config(root: Path) -> dict[str, object]:
    """Generate an MCP config agents can paste into their client settings."""

    return {
        "mcpServers": {
            "llm-harness": {
                "command": str(root / "harness"),
                "args": ["--root", str(root), "mcp"],
                "env": {
                    "HARNESS_ROOT": str(root),
                    "HARNESS_DB": str(root / ".harness" / "harness.sqlite3"),
                },
            }
        }
    }
