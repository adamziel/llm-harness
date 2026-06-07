"""Non-agentic test runner that records full and parsed results in SQLite."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import db

PYTEST_RESULT_RE = re.compile(r"^(?P<node>\S+?)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR)\b")
UNITTEST_RESULT_RE = re.compile(r"^(?P<test>\S+)\s+\((?P<case>[^)]+)\)\s+\.\.\.\s+(?P<status>ok|FAIL|ERROR|skipped\b.*)$")
STATUS_EVENT_THROTTLE_SECONDS = 5 * 60
PUBLIC_PHPT_METRIC_RE = re.compile(r"accepted_public_phpt_passes\s*[:=]\s*(?P<value>[0-9_,]+)\s*/\s*(?P<target>[0-9_,]+)")
SOFT_KNOWN_RED_LIMIT = 5



def discover_test_command(root: str | Path) -> list[str]:
    """Choose a deterministic full-suite command for the current repository."""

    root_path = Path(root)
    repo_script = root_path / "tools" / "run-tests.sh"
    if repo_script.exists() and repo_script.is_file():
        return ["tools/run-tests.sh"]
    wants_pytest = (root_path / "pytest.ini").exists() or (root_path / "pyproject.toml").exists() or (root_path / "tests").exists()
    if wants_pytest and importlib.util.find_spec("pytest") is not None:
        return ["python", "-m", "pytest", "-vv"]
    if (root_path / "tests").exists():
        return ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
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
    metric_recorded = record_public_phpt_metric(conn, full_log)
    update_test_gate_state(conn, run_id, " ".join(shlex.quote(part) for part in cmd), status, parsed, metric_recorded)
    db.note_failing_tests(conn, run_id, commit)
    if status == "failed":
        queue_test_fix_lane(conn, run_id, parsed, commit)
        maybe_invoke_architect(conn)
        _log_periodic_event(conn, "tests_failed", "Full test suite failed; Coordinator should prioritize stabilization lanes", payload={"run_id": run_id})
    else:
        resolve_fixed_tests(conn, parsed, commit)
        db.log_event(conn, "tests_passed", "Full test suite passed", payload={"run_id": run_id})
    db.purge_old_test_logs(conn)
    return run_id


def parse_test_output(output: str) -> list[dict[str, Any]]:
    """Extract pytest-style per-test rows when the runner prints them."""

    results = []
    for line in output.splitlines():
        stripped = line.strip()
        pytest_match = PYTEST_RESULT_RE.match(stripped)
        if pytest_match:
            node = pytest_match.group("node")
            status = pytest_match.group("status").lower()
            results.append({"nodeid": node, "file": node.split("::", 1)[0], "status": "failed" if status == "failed" else status})
            continue
        unittest_match = UNITTEST_RESULT_RE.match(stripped)
        if unittest_match:
            status = _normalize_unittest_status(unittest_match.group("status"))
            case = unittest_match.group("case")
            node = case
            results.append({"nodeid": node, "file": case.rsplit(".", 1)[0].replace(".", "/") + ".py", "status": status})
    return results


def _normalize_unittest_status(status: str) -> str:
    """Map unittest verbose words to the status vocabulary stored in SQLite."""

    if status == "ok":
        return "passed"
    if status.startswith("skipped"):
        return "skipped"
    if status == "ERROR":
        return "error"
    return "failed"


def summarize_results(results: list[dict[str, Any]], returncode: int) -> dict[str, int]:
    """Summarize parsed results while still reflecting command-level failure."""

    summary = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    for result in results:
        status = str(result.get("status", ""))
        summary[status] = summary.get(status, 0) + 1
    if not results and returncode != 0:
        summary["failed"] = 1
    return summary


def queue_test_fix_lane(conn: sqlite3.Connection, run_id: int, results: list[dict[str, Any]], commit: str) -> None:
    """Put main-branch test failures at the top of the durable card queue."""

    failures = [result["nodeid"] for result in results if result.get("status") in {"failed", "error"}]
    run = conn.execute("SELECT command FROM test_runs WHERE id = ?", (run_id,)).fetchone()
    command = run["command"] if run else ""
    global_command = is_global_test_command(command)
    failure_key = "global-suite" if global_command else ",".join(sorted(failures)) if failures else "command-level-failure"
    title = "Fix global test suite failures" if global_command else f"Fix failing tests from run {run_id}"
    notes = "Failed tests: " + (", ".join(failures) if failures else "see full test log") + f"\nFirst failing commit: {commit}"
    source_key = "test-failure:global-suite" if global_command else f"test-failure:{command}:{failure_key}"
    existing = conn.execute(
        """
        SELECT id, status FROM worklanes
        WHERE source_key = ? AND stage != 'done' AND status NOT IN ('abandoned', 'cancelled', 'stale')
        ORDER BY id LIMIT 1
        """,
        (source_key,),
    ).fetchone()
    if existing:
        if global_command and existing["status"] == "integration_failed":
            db.requeue_card(conn, int(existing["id"]), notes + f"\nLatest failing run: {run_id}")
            db.log_event(conn, "worklane_requeued", f"Requeued failing global gate card#{existing['id']} from run {run_id}", payload={"card_id": existing["id"], "run_id": run_id})
            return
        conn.execute(
            """
            UPDATE worklanes
            SET notes = ?, priority = MIN(priority, 0), last_activity_at = ?
            WHERE id = ?
            """,
            (notes + f"\nLatest failing run: {run_id}", db.utc_now(), existing["id"]),
        )
        if not _recent_payload_event(conn, "worklane_deduplicated", "card_id", existing["id"], STATUS_EVENT_THROTTLE_SECONDS):
            db.log_event(conn, "worklane_deduplicated", f"Updated existing failing-test card#{existing['id']} from run {run_id}", payload={"card_id": existing["id"], "run_id": run_id})
        return
    db.create_card(
        conn,
        title,
        role_type="Developer",
        status="queued",
        stage="planned",
        notes=notes,
        priority=0,
        goal="Restore the main-branch full test suite.",
        acceptance_criteria="The failing tests pass in the deterministic test loop.",
        source_key=source_key,
    )


def record_public_phpt_metric(conn: sqlite3.Connection, output: str) -> bool:
    """Record the public PHPT pass-count metric when the full gate prints it."""

    match = PUBLIC_PHPT_METRIC_RE.search(output)
    if not match:
        return False
    value = int(match.group("value").replace(",", "").replace("_", ""))
    target = int(match.group("target").replace(",", "").replace("_", ""))
    if target <= 0:
        return False
    db.record_metric(conn, "accepted_public_phpt_passes", value, target)
    return True


def update_test_gate_state(
    conn: sqlite3.Connection,
    run_id: int,
    command: str,
    status: str,
    results: list[dict[str, Any]],
    metric_recorded: bool,
) -> None:
    """Classify global gate failures as hard blockers or soft known-red debt."""

    if not is_global_test_command(command):
        return
    failures = sorted({str(result.get("nodeid", "")) for result in results if result.get("status") in {"failed", "error"} and result.get("nodeid")})
    if status == "passed":
        _set_test_gate(conn, "green", "Global test gate is green.", [], run_id)
        return
    if not failures:
        failures = ["command-level-failure"]
    if metric_recorded and len(failures) <= SOFT_KNOWN_RED_LIMIT:
        _set_test_gate(
            conn,
            "soft_known_red",
            f"{len(failures)} known failures remain, but the metric-producing gate still ran.",
            failures,
            run_id,
        )
        return
    reason = "Global tests failed before producing a progress metric."
    if metric_recorded:
        reason = f"{len(failures)} failures exceeds the soft known-red limit of {SOFT_KNOWN_RED_LIMIT}."
    _set_test_gate(conn, "hard_blocker", reason, failures, run_id)


def _set_test_gate(conn: sqlite3.Connection, mode: str, reason: str, failures: list[str], run_id: int) -> None:
    previous = (
        db.get_meta(conn, "test_gate_mode"),
        db.get_meta(conn, "test_gate_reason"),
        db.get_meta(conn, "test_gate_failures_json"),
    )
    failures_json = json.dumps(failures, sort_keys=True)
    db.set_meta(conn, "test_gate_mode", mode)
    db.set_meta(conn, "test_gate_reason", reason)
    db.set_meta(conn, "test_gate_failures_json", failures_json)
    db.set_meta(conn, "test_gate_failure_count", str(len(failures)))
    db.set_meta(conn, "test_gate_run_id", str(run_id))
    current = (mode, reason, failures_json)
    if current != previous:
        db.log_event(conn, f"test_gate_{mode}", reason, payload={"run_id": run_id, "failures": failures})


def is_global_test_command(command: str) -> bool:
    """Return whether a failing command represents the main full-suite loop."""

    normalized = " ".join(command.split())
    return (
        "tools/run-tests.sh" in normalized
        or ("unittest discover" in normalized and "-s tests" in normalized)
        or ("pytest" in normalized and " tests" in f" {normalized}")
    )


def _log_periodic_event(conn: sqlite3.Connection, event_type: str, message: str, payload: dict[str, Any]) -> None:
    """Log noisy test-loop events at most once per throttle window."""

    if _recent_message_event(conn, event_type, message, STATUS_EVENT_THROTTLE_SECONDS):
        return
    db.log_event(conn, event_type, message, payload=payload)


def _recent_message_event(conn: sqlite3.Connection, event_type: str, message: str, seconds: int) -> bool:
    cutoff = (datetime.fromisoformat(db.utc_now()) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    return (
        conn.execute(
            "SELECT 1 FROM events WHERE type = ? AND message = ? AND ts >= ? ORDER BY id DESC LIMIT 1",
            (event_type, message, cutoff),
        ).fetchone()
        is not None
    )


def _recent_payload_event(conn: sqlite3.Connection, event_type: str, key: str, value: object, seconds: int) -> bool:
    cutoff = (datetime.fromisoformat(db.utc_now()) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = ? AND ts >= ? ORDER BY id DESC LIMIT 50",
        (event_type, cutoff),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get(key) == value:
            return True
    return False


def resolve_fixed_tests(conn: sqlite3.Connection, results: list[dict[str, Any]], commit: str) -> None:
    """Close bug reports when a later passing run proves the test is fixed."""

    passed = {result["nodeid"] for result in results if result.get("status") == "passed"}
    if passed:
        rows = conn.execute(
            "SELECT * FROM bug_reports WHERE status = 'open'"
        ).fetchall()
        fixed_ids = [row["id"] for row in rows if row["test_nodeid"] in passed]
    else:
        # Some runners only print a command-level success. A green full-suite run
        # still proves previously open test failures are no longer present.
        fixed_ids = [row["id"] for row in conn.execute("SELECT id FROM bug_reports WHERE status = 'open'").fetchall()]
    for bug_id in fixed_ids:
        bug = conn.execute("SELECT test_nodeid FROM bug_reports WHERE id = ?", (bug_id,)).fetchone()
        conn.execute(
            """
            UPDATE bug_reports
            SET status = 'fixed', fixed_commit = ?, resolution = 'Fixed before or during this passing test run.', updated_at = ?
            WHERE id = ?
            """,
            (commit, db.utc_now(), bug_id),
        )
        if bug:
            conn.execute(
                """
                UPDATE issues
                SET status = 'fixed', fixed_commit = ?, resolution = 'Fixed before or during this passing test run.', updated_at = ?
                WHERE issue_key = ?
                """,
                (commit, db.utc_now(), f"test:{bug['test_nodeid']}"),
            )
    conn.commit()


def maybe_invoke_architect(conn: sqlite3.Connection) -> None:
    """Escalate tests that have failed repeatedly in the last 24 hours."""

    since = (datetime.fromisoformat(db.utc_now()) - timedelta(hours=24)).isoformat(timespec="seconds")
    repeated = conn.execute(
        """
        SELECT test_nodeid, occurrences FROM bug_reports
        WHERE status = 'open' AND updated_at >= ? AND occurrences > 3
        """,
        (since,),
    ).fetchall()
    for row in repeated:
        db.queue_spawn_request(
            conn,
            role="Architect",
            title=f"Find systemic cause for repeated failure: {row['test_nodeid']}",
            prompt=(
                f"Test {row['test_nodeid']} has failed more than three times in 24 hours. "
                "Investigate the structural root cause and plan a reliability refactor."
            ),
            requester="test-loop",
        )


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
