"""SQLite storage used as the harness' durable memory.

The scheduler treats the database as the source of truth for goals, agents,
work lanes, test history, resource samples, and events.  Agent-facing MCP tools
write here too, so the harness can restart without trusting any Codex process to
remember what happened before a crash.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_MS = 60_000
AGENT_TERMINAL_STATUSES = ("crash", "success", "stopped")
AGENT_LIFECYCLE_STATUSES = ("running", *AGENT_TERMINAL_STATUSES)
MCP_COMPAT_COLUMNS = {
    "events": [
        ("created_at", "TEXT GENERATED ALWAYS AS (ts) VIRTUAL"),
    ],
    "test_runs": [
        ("summary", "TEXT GENERATED ALWAYS AS (summary_json) VIRTUAL"),
        ("created_at", "TEXT GENERATED ALWAYS AS (started_at) VIRTUAL"),
        ("updated_at", "TEXT GENERATED ALWAYS AS (COALESCE(ended_at, started_at)) VIRTUAL"),
    ],
    "bug_reports": [
        ("severity", "INTEGER GENERATED ALWAYS AS (occurrences) VIRTUAL"),
        ("title", "TEXT GENERATED ALWAYS AS (test_nodeid) VIRTUAL"),
        (
            "notes",
            "TEXT GENERATED ALWAYS AS (trim(root_cause || CASE WHEN root_cause != '' AND resolution != '' THEN char(10) ELSE '' END || resolution)) VIRTUAL",
        ),
    ],
}


def is_active_agent_status(status: str) -> bool:
    """Return whether an agent status still represents a live harness worker."""

    return status not in AGENT_TERMINAL_STATUSES


def is_locked_error(exc: BaseException) -> bool:
    """Return whether SQLite is asking the harness to wait and retry."""

    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


@dataclass(frozen=True)
class HarnessPaths:
    """Canonical filesystem locations for one repository's harness state."""

    root: Path
    home: Path
    db: Path
    prompts: Path
    worktrees: Path
    tmp: Path


def utc_now() -> str:
    """Return an ISO timestamp with a timezone so rows sort lexicographically."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def paths_for(root: str | Path) -> HarnessPaths:
    """Resolve the state directories without creating them."""

    root_path = Path(root).resolve()
    home = root_path / ".harness"
    return HarnessPaths(
        root=root_path,
        home=home,
        db=home / "harness.sqlite3",
        prompts=home / "prompts",
        worktrees=home / "worktrees",
        tmp=home / "tmp",
    )


def ensure_dirs(paths: HarnessPaths) -> None:
    """Create only the directories the deterministic scheduler owns."""

    for directory in (paths.home, paths.prompts, paths.worktrees, paths.tmp):
        directory.mkdir(parents=True, exist_ok=True)


@contextmanager
def connect(db_path: str | Path):
    """Open SQLite with row dictionaries and foreign key enforcement enabled."""

    conn = sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """Install or update the durable schema used by the scheduler and MCP."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            text TEXT NOT NULL,
            measure TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'planning',
            plan_path TEXT NOT NULL DEFAULT 'PLAN.md',
            auditor_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            source_agent TEXT NOT NULL DEFAULT '',
            round INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            agent_name TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            current_status TEXT NOT NULL,
            tmux_session TEXT NOT NULL DEFAULT '',
            tmux_window TEXT NOT NULL DEFAULT '',
            tmux_pane TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            last_seen_at TEXT NOT NULL,
            last_prompt_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            crash_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS work_lanes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            title TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'Developer',
            status TEXT NOT NULL DEFAULT 'queued',
            branch TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            expected_metric_delta REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'broadcast',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
        );

        CREATE TABLE IF NOT EXISTS spawn_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            requester TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            agent_name TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS resource_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            cpu_percent REAL NOT NULL,
            ram_percent REAL NOT NULL,
            disk_free_gb REAL NOT NULL,
            load1 REAL NOT NULL DEFAULT 0,
            process_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS metric_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value REAL NOT NULL,
            target REAL NOT NULL,
            percent_ready REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS test_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            command TEXT NOT NULL,
            commit_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            full_log TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
            nodeid TEXT NOT NULL,
            file TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            duration REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bug_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_nodeid TEXT NOT NULL,
            first_failed_commit TEXT NOT NULL DEFAULT '',
            fixed_commit TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            root_cause TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            occurrences INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worktree TEXT NOT NULL,
            path TEXT NOT NULL,
            mtime REAL NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(worktree, path)
        );

        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(current_status);
        CREATE INDEX IF NOT EXISTS idx_work_lanes_status ON work_lanes(status);
        CREATE INDEX IF NOT EXISTS idx_messages_target_status ON messages(target, status);
        CREATE INDEX IF NOT EXISTS idx_spawn_requests_status ON spawn_requests(status);
        CREATE INDEX IF NOT EXISTS idx_resource_samples_ts ON resource_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_test_runs_started ON test_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_bugs_node_status ON bug_reports(test_nodeid, status);
        CREATE INDEX IF NOT EXISTS idx_code_index_worktree_path ON code_index(worktree, path);
        """
    )
    ensure_mcp_compat_columns(conn)
    if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def ensure_mcp_compat_columns(conn: sqlite3.Connection) -> None:
    """Add generated aliases for common agent memory queries without duplicating data."""

    for table, columns in MCP_COMPAT_COLUMNS.items():
        existing = {
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
            for row in conn.execute(f"PRAGMA table_xinfo({table})")
        }
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def bootstrap(root: str | Path) -> HarnessPaths:
    """Create state directories and initialize the database for a repository."""

    paths = paths_for(root)
    ensure_dirs(paths)
    while True:
        try:
            with connect(paths.db) as conn:
                init_db(conn)
            return paths
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                raise
            print("\033[33mSQLite database is locked during bootstrap; waiting and retrying.\033[0m", file=sys.stderr)
            time.sleep(2)


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Store a small scalar value used by the scheduler itself."""

    now = utc_now()
    conn.execute(
        """
        INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read a scheduler metadata value without raising when it is absent."""

    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    message: str,
    agent_name: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> int:
    """Append an event so status reports can narrate what changed."""

    cur = conn.execute(
        """
        INSERT INTO events(ts, type, message, agent_name, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (utc_now(), event_type, message, agent_name, json.dumps(payload or {}, sort_keys=True)),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_goal(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the single active goal row, if planning has begun."""

    return conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()


def set_goal(
    conn: sqlite3.Connection,
    text: str,
    measure: str = "",
    status: str = "planning",
    auditor_summary: str = "",
) -> None:
    """Store the user goal without making Codex sessions the only copy of it."""

    now = utc_now()
    conn.execute(
        """
        INSERT INTO goals(id, text, measure, status, auditor_summary, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text = excluded.text,
            measure = excluded.measure,
            status = excluded.status,
            auditor_summary = excluded.auditor_summary,
            updated_at = excluded.updated_at
        """,
        (text, measure, status, auditor_summary, now, now),
    )
    log_event(conn, "goal", "Goal recorded", payload={"status": status})


def upsert_agent(conn: sqlite3.Connection, **fields: Any) -> None:
    """Create or update an agent run while preserving required bookkeeping columns."""

    if "name" not in fields or "role" not in fields:
        raise ValueError("agent name and role are required")
    now = utc_now()
    values = {
        "current_status": "running",
        "tmux_session": "",
        "tmux_window": "",
        "tmux_pane": "",
        "cwd": "",
        "worktree": "",
        "branch": "",
        "pid": None,
        "started_at": now,
        "ended_at": None,
        "last_seen_at": now,
        "last_prompt_at": now,
        "notes": "",
        "crash_count": 0,
    }
    values.update(fields)
    conn.execute(
        """
        INSERT INTO agents(
            name, role, current_status, tmux_session, tmux_window, tmux_pane,
            cwd, worktree, branch, pid, started_at, ended_at, last_seen_at,
            last_prompt_at, notes, crash_count
        ) VALUES (
            :name, :role, :current_status, :tmux_session, :tmux_window, :tmux_pane,
            :cwd, :worktree, :branch, :pid, :started_at, :ended_at, :last_seen_at,
            :last_prompt_at, :notes, :crash_count
        )
        ON CONFLICT(name) DO UPDATE SET
            role = excluded.role,
            current_status = excluded.current_status,
            tmux_session = excluded.tmux_session,
            tmux_window = excluded.tmux_window,
            tmux_pane = excluded.tmux_pane,
            cwd = excluded.cwd,
            worktree = excluded.worktree,
            branch = excluded.branch,
            pid = excluded.pid,
            ended_at = excluded.ended_at,
            last_seen_at = excluded.last_seen_at,
            last_prompt_at = excluded.last_prompt_at,
            notes = excluded.notes,
            crash_count = excluded.crash_count
        """,
        values,
    )
    conn.commit()


def update_agent_status(
    conn: sqlite3.Connection,
    name: str,
    status: str,
    notes: str | None = None,
    ended: bool = False,
) -> None:
    """Record liveness changes from the watchdog without deleting history."""

    now = utc_now()
    if notes is None:
        conn.execute(
            """
            UPDATE agents SET current_status = ?, last_seen_at = ?, ended_at = CASE WHEN ? THEN ? ELSE ended_at END
            WHERE name = ?
            """,
            (status, now, 1 if ended else 0, now, name),
        )
    else:
        conn.execute(
            """
            UPDATE agents SET current_status = ?, last_seen_at = ?, notes = ?,
                ended_at = CASE WHEN ? THEN ? ELSE ended_at END
            WHERE name = ?
            """,
            (status, now, notes, 1 if ended else 0, now, name),
        )
    conn.commit()


def list_agents(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    """Return agent rows, optionally narrowed to a current status."""

    if status is None:
        return list(conn.execute("SELECT * FROM agents ORDER BY role, name"))
    return list(conn.execute("SELECT * FROM agents WHERE current_status = ? ORDER BY role, name", (status,)))


def recent_events(conn: sqlite3.Connection, limit: int = 12) -> list[sqlite3.Row]:
    """Fetch the newest events in display order."""

    rows = list(
        conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )
    rows.reverse()
    return rows


def queue_message(conn: sqlite3.Connection, message: str, target: str = "broadcast") -> int:
    """Persist a prompt injection before tmux delivery is attempted."""

    cur = conn.execute(
        "INSERT INTO messages(ts, target, message, status) VALUES (?, ?, ?, 'queued')",
        (utc_now(), target, message),
    )
    log_event(conn, "poke", f"Queued message for {target}", payload={"message": message})
    return int(cur.lastrowid)


def mark_message(conn: sqlite3.Connection, message_id: int, status: str) -> None:
    """Mark a user or scheduler prompt as delivered or failed."""

    conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))
    conn.commit()


def record_resource_sample(conn: sqlite3.Connection, sample: Mapping[str, Any]) -> int:
    """Store one deterministic resource probe for watchdog and reports."""

    cur = conn.execute(
        """
        INSERT INTO resource_samples(ts, cpu_percent, ram_percent, disk_free_gb, load1, process_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            float(sample.get("cpu_percent", 0)),
            float(sample.get("ram_percent", 0)),
            float(sample.get("disk_free_gb", 0)),
            float(sample.get("load1", 0)),
            json.dumps(sample.get("processes", []), sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_metric(conn: sqlite3.Connection, name: str, value: float, target: float) -> int:
    """Persist a progress point so stalled-progress checks have data."""

    percent = 0.0 if target == 0 else max(0.0, min(100.0, (value / target) * 100.0))
    cur = conn.execute(
        "INSERT INTO metric_samples(ts, metric_name, value, target, percent_ready) VALUES (?, ?, ?, ?, ?)",
        (utc_now(), name, float(value), float(target), percent),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_metric(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the newest metric sample regardless of metric name."""

    return conn.execute("SELECT * FROM metric_samples ORDER BY id DESC LIMIT 1").fetchone()


def queue_spawn_request(
    conn: sqlite3.Connection,
    role: str,
    title: str,
    prompt: str,
    requester: str = "",
    notes: str = "",
) -> int:
    """Route sub-agent spawning requests through scheduler-owned state."""

    cur = conn.execute(
        """
        INSERT INTO spawn_requests(ts, requester, role, title, prompt, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), requester, role, title, prompt, notes),
    )
    log_event(conn, "spawn_request", f"{requester or 'agent'} requested {role}: {title}")
    return int(cur.lastrowid)


def next_spawn_requests(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """Fetch queued spawn requests in the order the scheduler should handle them."""

    return list(
        conn.execute(
            "SELECT * FROM spawn_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
            (limit,),
        )
    )


def mark_spawn_request(conn: sqlite3.Connection, request_id: int, status: str, agent_name: str = "") -> None:
    """Record whether the scheduler accepted an MCP spawn request."""

    conn.execute(
        "UPDATE spawn_requests SET status = ?, agent_name = ? WHERE id = ?",
        (status, agent_name, request_id),
    )
    conn.commit()


def record_test_run(
    conn: sqlite3.Connection,
    command: str,
    status: str,
    full_log: str,
    summary: Mapping[str, Any] | None = None,
    commit_sha: str = "",
    results: Iterable[Mapping[str, Any]] = (),
    started_at: str | None = None,
    ended_at: str | None = None,
) -> int:
    """Store a full test run and parsed per-test rows when available."""

    cur = conn.execute(
        """
        INSERT INTO test_runs(started_at, ended_at, command, commit_sha, status, summary_json, full_log)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at or utc_now(),
            ended_at or utc_now(),
            command,
            commit_sha,
            status,
            json.dumps(summary or {}, sort_keys=True),
            full_log,
        ),
    )
    run_id = int(cur.lastrowid)
    for result in results:
        conn.execute(
            """
            INSERT INTO test_results(run_id, nodeid, file, status, message, duration)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(result.get("nodeid", "")),
                str(result.get("file", "")),
                str(result.get("status", "")),
                str(result.get("message", "")),
                float(result.get("duration", 0) or 0),
            ),
        )
    conn.commit()
    return run_id


def note_failing_tests(conn: sqlite3.Connection, run_id: int, commit_sha: str) -> None:
    """Turn failing test rows into durable bug reports for later lookups."""

    now = utc_now()
    failures = conn.execute(
        "SELECT DISTINCT nodeid FROM test_results WHERE run_id = ? AND status IN ('failed', 'error')",
        (run_id,),
    ).fetchall()
    for row in failures:
        existing = conn.execute(
            "SELECT * FROM bug_reports WHERE test_nodeid = ? AND status = 'open'",
            (row["nodeid"],),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE bug_reports SET occurrences = occurrences + 1, updated_at = ? WHERE id = ?",
                (now, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO bug_reports(test_nodeid, first_failed_commit, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["nodeid"], commit_sha, now, now),
            )
    conn.commit()


def purge_old_test_logs(conn: sqlite3.Connection) -> None:
    """Keep recent full logs and compact older test runs without dropping metadata."""

    rows = conn.execute("SELECT id, started_at FROM test_runs ORDER BY id DESC").fetchall()
    keep_full = {row["id"] for row in rows[:5]}
    now = datetime.now(timezone.utc)
    hourly: set[str] = set()
    daily: set[str] = set()
    weekly: set[str] = set()
    for row in rows[5:]:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except (TypeError, ValueError):
            continue
        age = now - started
        if age.days == 0:
            bucket = started.strftime("%Y-%m-%d-%H")
            if bucket not in hourly:
                hourly.add(bucket)
                keep_full.add(row["id"])
        elif age.days <= 7:
            bucket = started.strftime("%Y-%m-%d")
            if bucket not in daily:
                daily.add(bucket)
                keep_full.add(row["id"])
        else:
            year, week, _ = started.isocalendar()
            bucket = f"{year}-W{week}"
            if bucket not in weekly:
                weekly.add(bucket)
                keep_full.add(row["id"])
    if keep_full:
        placeholders = ",".join("?" for _ in keep_full)
        conn.execute(
            f"UPDATE test_runs SET full_log = '' WHERE id NOT IN ({placeholders})",
            tuple(keep_full),
        )
    conn.commit()


def read_only_query(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Run SELECT-style queries from the MCP while blocking database mutation."""

    stripped = sql.strip().lower()
    if not (stripped.startswith("select") or stripped.startswith("with") or stripped.startswith("pragma")):
        raise ValueError("memory_query only allows SELECT, WITH, or PRAGMA statements")
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]
