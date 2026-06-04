"""Non-agentic test runner that records full and parsed results in SQLite."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import db

TEST_RESULT_RE = re.compile(r"^(?P<node>\S+?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b")


def discover_test_command(root: str | Path) -> list[str]:
    """Choose a deterministic full-suite command for the current repository."""

    root_path = Path(root)
    wants_pytest = (root_path / "pytest.ini").exists() or (root_path / "pyproject.toml").exists() or (root_path / "tests").exists()
    if wants_pytest and importlib.util.find_spec("pytest") is not None:
        return ["python", "-m", "pytest", "-q"]
    return ["python", "-m", "unittest", "discover"]


def run_tests_once(conn: sqlite3.Connection, root: str | Path, command: list[str] | None = None) -> int:
    """Run the full suite once and record logs, summaries, failures, and bugs."""

    root_path = Path(root)
    cmd = command or discover_test_command(root_path)
    started = db.utc_now()
    proc = subprocess.run(cmd, cwd=root_path, text=True, capture_output=True, check=False)
    ended = db.utc_now()
    full_log = proc.stdout + proc.stderr
    status = "passed" if proc.returncode == 0 else "failed"
    parsed = parse_test_output(full_log)
    summary = summarize_results(parsed, proc.returncode)
    commit = _git_commit(root_path)
    run_id = db.record_test_run(
        conn,
        command=" ".join(shlex.quote(part) for part in cmd),
        status=status,
        full_log=full_log,
        summary=summary,
        commit_sha=commit,
        results=parsed,
        started_at=started,
        ended_at=ended,
    )
    db.note_failing_tests(conn, run_id, commit)
    if status == "failed":
        db.log_event(conn, "tests_failed", "Full test suite failed; Manager should prioritize fixes", payload={"run_id": run_id})
    else:
        db.log_event(conn, "tests_passed", "Full test suite passed", payload={"run_id": run_id})
    db.purge_old_test_logs(conn)
    return run_id


def parse_test_output(output: str) -> list[dict[str, Any]]:
    """Extract pytest-style per-test rows when the runner prints them."""

    results = []
    for line in output.splitlines():
        match = TEST_RESULT_RE.match(line.strip())
        if not match:
            continue
        node = match.group("node")
        status = match.group("status").lower()
        results.append({"nodeid": node, "file": node.split("::", 1)[0], "status": "failed" if status == "failed" else status})
    return results


def summarize_results(results: list[dict[str, Any]], returncode: int) -> dict[str, int]:
    """Summarize parsed results while still reflecting command-level failure."""

    summary = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for result in results:
        status = str(result.get("status", ""))
        summary[status] = summary.get(status, 0) + 1
    if not results and returncode != 0:
        summary["failed"] = 1
    return summary


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
