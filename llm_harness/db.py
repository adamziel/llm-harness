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


def is_retryable_error(exc: BaseException) -> bool:
    """Return whether SQLite hit a transient filesystem or lock condition."""

    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "disk i/o error" in message


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

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "disk i/o error" not in str(exc).lower():
                raise
            conn.execute("PRAGMA journal_mode = DELETE")
    except Exception:
        conn.close()
        raise
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

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS worklanes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            acceptance_criteria TEXT NOT NULL DEFAULT '',
            owner_agent_id INTEGER,
            role_type TEXT NOT NULL DEFAULT 'Developer',
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'queued',
            base_branch TEXT NOT NULL DEFAULT '',
            branch_name TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            dependencies TEXT NOT NULL DEFAULT '[]',
            conflict_risk TEXT NOT NULL DEFAULT 'unknown',
            expected_metric_impact REAL NOT NULL DEFAULT 0,
            integration_queue TEXT NOT NULL DEFAULT '',
            test_evidence TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            assigned_at TEXT,
            last_activity_at TEXT,
            ready_for_integration_at TEXT,
            integrated_at TEXT,
            abandoned_at TEXT,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'broadcast',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
        );

        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'broadcast',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            delivered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            agent_name TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            role TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worktrees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL DEFAULT '',
            owner_agent TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            base_commit TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            last_activity_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS commits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sha TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            agent_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS integration_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worklane_id INTEGER NOT NULL,
            attempt_branch TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'integrating',
            merge_result TEXT NOT NULL DEFAULT '',
            tests_json TEXT NOT NULL DEFAULT '[]',
            failure_reason TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT
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

        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            severity INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT '',
            first_seen_commit TEXT NOT NULL DEFAULT '',
            fixed_commit TEXT NOT NULL DEFAULT '',
            root_cause TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS status_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_worklanes_status ON worklanes(status);
        CREATE INDEX IF NOT EXISTS idx_worklanes_queue ON worklanes(integration_queue, status, priority);
        CREATE INDEX IF NOT EXISTS idx_messages_target_status ON messages(target, status);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_target_status ON agent_messages(target, status);
        CREATE INDEX IF NOT EXISTS idx_agent_reports_worklane ON agent_reports(worklane_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_worktrees_owner ON worktrees(owner_agent, status);
        CREATE INDEX IF NOT EXISTS idx_integration_attempts_lane ON integration_attempts(worklane_id, status);
        CREATE INDEX IF NOT EXISTS idx_spawn_requests_status ON spawn_requests(status);
        CREATE INDEX IF NOT EXISTS idx_resource_samples_ts ON resource_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_test_runs_started ON test_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_bugs_node_status ON bug_reports(test_nodeid, status);
        CREATE INDEX IF NOT EXISTS idx_issues_key_status ON issues(issue_key, status);
        CREATE INDEX IF NOT EXISTS idx_code_index_worktree_path ON code_index(worktree, path);
        """
    )
    ensure_worklane_compat(conn)
    ensure_mcp_compat_columns(conn)
    if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def ensure_worklane_compat(conn: sqlite3.Connection) -> None:
    """Expose the refined worklanes table through the legacy work_lanes name."""

    object_type = _sqlite_object_type(conn, "work_lanes")
    if object_type == "table":
        for row in conn.execute("SELECT * FROM work_lanes").fetchall():
            conn.execute(
                """
                INSERT OR IGNORE INTO worklanes(
                    id, title, role_type, status, branch_name, worktree_path,
                    expected_metric_impact, created_at, last_activity_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["title"],
                    row["role"],
                    row["status"],
                    row["branch"],
                    row["worktree"],
                    row["expected_metric_delta"],
                    row["ts"],
                    row["ts"],
                    row["notes"],
                ),
            )
        conn.execute("DROP TABLE work_lanes")
    elif object_type == "view":
        conn.execute("DROP VIEW work_lanes")
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS work_lanes_insert;
        DROP TRIGGER IF EXISTS work_lanes_update;
        DROP TRIGGER IF EXISTS work_lanes_delete;

        CREATE VIEW work_lanes AS
        SELECT
            id,
            created_at AS ts,
            title,
            role_type AS role,
            status,
            branch_name AS branch,
            worktree_path AS worktree,
            expected_metric_impact AS expected_metric_delta,
            notes
        FROM worklanes;

        CREATE TRIGGER work_lanes_insert INSTEAD OF INSERT ON work_lanes
        BEGIN
            INSERT INTO worklanes(
                id, title, role_type, status, branch_name, worktree_path,
                expected_metric_impact, created_at, last_activity_at, notes
            ) VALUES (
                NEW.id,
                COALESCE(NEW.title, ''),
                COALESCE(NEW.role, 'Developer'),
                COALESCE(NEW.status, 'queued'),
                COALESCE(NEW.branch, ''),
                COALESCE(NEW.worktree, ''),
                COALESCE(NEW.expected_metric_delta, 0),
                COALESCE(NEW.ts, datetime('now')),
                COALESCE(NEW.ts, datetime('now')),
                COALESCE(NEW.notes, '')
            );
        END;

        CREATE TRIGGER work_lanes_update INSTEAD OF UPDATE ON work_lanes
        BEGIN
            UPDATE worklanes SET
                title = COALESCE(NEW.title, title),
                role_type = COALESCE(NEW.role, role_type),
                status = COALESCE(NEW.status, status),
                branch_name = COALESCE(NEW.branch, branch_name),
                worktree_path = COALESCE(NEW.worktree, worktree_path),
                expected_metric_impact = COALESCE(NEW.expected_metric_delta, expected_metric_impact),
                notes = COALESCE(NEW.notes, notes),
                last_activity_at = datetime('now')
            WHERE id = OLD.id;
        END;

        CREATE TRIGGER work_lanes_delete INSTEAD OF DELETE ON work_lanes
        BEGIN
            DELETE FROM worklanes WHERE id = OLD.id;
        END;
        """
    )


def _sqlite_object_type(conn: sqlite3.Connection, name: str) -> str:
    """Return the SQLite object type for migrations, or an empty string."""

    row = conn.execute("SELECT type FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    if row is None:
        return ""
    return str(row["type"] if isinstance(row, sqlite3.Row) else row[0])


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
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
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

    now = utc_now()
    cur = conn.execute(
        "INSERT INTO messages(ts, target, message, status) VALUES (?, ?, ?, 'queued')",
        (now, target, message),
    )
    conn.execute(
        "INSERT INTO agent_messages(id, created_at, target, message, status) VALUES (?, ?, ?, ?, 'queued')",
        (int(cur.lastrowid), now, target, message),
    )
    log_event(conn, "poke", f"Queued message for {target}", payload={"message": message})
    return int(cur.lastrowid)


def mark_message(conn: sqlite3.Connection, message_id: int, status: str) -> None:
    """Mark a user or scheduler prompt as delivered or failed."""

    delivered_at = utc_now() if status == "delivered" else None
    conn.execute("UPDATE messages SET status = ? WHERE id = ?", (status, message_id))
    conn.execute("UPDATE agent_messages SET status = ?, delivered_at = COALESCE(?, delivered_at) WHERE id = ?", (status, delivered_at, message_id))
    conn.commit()


def queue_worklane(
    conn: sqlite3.Connection,
    title: str,
    role_type: str = "Developer",
    status: str = "queued",
    notes: str = "",
    priority: int = 100,
    description: str = "",
    goal: str = "",
    acceptance_criteria: str = "",
    expected_metric_impact: float = 0,
) -> int:
    """Create a refined worklane row and return its durable id."""

    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO worklanes(
            title, description, goal, acceptance_criteria, role_type, priority,
            status, expected_metric_impact, created_at, last_activity_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, goal, acceptance_criteria, role_type, priority, status, expected_metric_impact, now, now, notes),
    )
    log_event(conn, "worklane_created", title, payload={"worklane_id": int(cur.lastrowid), "status": status})
    return int(cur.lastrowid)


def claim_next_worklane(conn: sqlite3.Connection, agent_name: str, worktree: str, branch: str) -> sqlite3.Row | None:
    """Assign the highest-priority queued lane to a developer, if one exists."""

    lane = conn.execute(
        """
        SELECT * FROM worklanes
        WHERE status = 'queued' AND role_type IN ('Developer', 'Designer')
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """
    ).fetchone()
    if lane is None:
        return None
    now = utc_now()
    agent = conn.execute("SELECT id FROM agents WHERE name = ?", (agent_name,)).fetchone()
    conn.execute(
        """
        UPDATE worklanes
        SET status = 'assigned', owner_agent_id = ?, branch_name = ?, worktree_path = ?,
            assigned_at = ?, last_activity_at = ?
        WHERE id = ?
        """,
        (agent["id"] if agent else None, branch, worktree, now, now, lane["id"]),
    )
    log_event(conn, "worklane_assigned", f"Assigned worklane#{lane['id']} to {agent_name}", agent_name=agent_name, payload={"worklane_id": lane["id"]})
    return conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane["id"],)).fetchone()


def update_worklane_status(conn: sqlite3.Connection, lane_id: int, status: str, notes: str | None = None) -> None:
    """Move one worklane through its per-lane lifecycle."""

    now = utc_now()
    fields = ["status = ?", "last_activity_at = ?"]
    params: list[Any] = [status, now]
    if status == "ready_for_integration":
        fields.extend(["ready_for_integration_at = ?", "integration_queue = ?"])
        params.extend([now, "ready_fast_path"])
    elif status == "integrated":
        fields.append("integrated_at = ?")
        params.append(now)
    elif status == "abandoned":
        fields.append("abandoned_at = ?")
        params.append(now)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    params.append(lane_id)
    conn.execute(f"UPDATE worklanes SET {', '.join(fields)} WHERE id = ?", tuple(params))
    log_event(conn, "worklane_status", f"worklane#{lane_id} -> {status}", payload={"worklane_id": lane_id, "status": status})


def record_agent_report(conn: sqlite3.Connection, report: Mapping[str, Any]) -> int:
    """Store a structured agent report and reflect authoritative lane status."""

    now = utc_now()
    agent_name = str(report.get("agent_id") or report.get("integrator_id") or report.get("agent_name") or "")
    lane_value = report.get("worklane_id")
    lane_id = int(lane_value) if str(lane_value or "").isdigit() else None
    status = str(report.get("status") or "")
    agent = conn.execute("SELECT role FROM agents WHERE name = ?", (agent_name,)).fetchone()
    cur = conn.execute(
        """
        INSERT INTO agent_reports(created_at, agent_name, worklane_id, role, status, report_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (now, agent_name, lane_id, agent["role"] if agent else "", status, json.dumps(report, sort_keys=True)),
    )
    if lane_id and status:
        update_worklane_status(conn, lane_id, status, str(report.get("summary") or ""))
    log_event(conn, "agent_report", f"{agent_name or 'agent'} reported {status or 'status'}", agent_name=agent_name, payload={"report_id": int(cur.lastrowid), "worklane_id": lane_id})
    conn.commit()
    return int(cur.lastrowid)


def record_worktree(
    conn: sqlite3.Connection,
    path: str,
    branch: str = "",
    owner_agent: str = "",
    worklane_id: int | None = None,
    base_commit: str = "",
    status: str = "active",
) -> None:
    """Record a harness-owned worktree so Janitor can preserve unintegrated work."""

    now = utc_now()
    conn.execute(
        """
        INSERT INTO worktrees(path, branch, owner_agent, worklane_id, base_commit, status, last_activity_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            branch = excluded.branch,
            owner_agent = excluded.owner_agent,
            worklane_id = excluded.worklane_id,
            base_commit = excluded.base_commit,
            status = excluded.status,
            last_activity_at = excluded.last_activity_at
        """,
        (path, branch, owner_agent, worklane_id, base_commit, status, now),
    )
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
        conn.execute(
            """
            INSERT INTO issues(issue_key, title, status, severity, source, first_seen_commit, created_at, updated_at)
            VALUES (?, ?, 'open', 1, 'test-loop', ?, ?, ?)
            ON CONFLICT(issue_key) DO UPDATE SET
                status = 'open',
                severity = severity + 1,
                updated_at = excluded.updated_at
            """,
            (f"test:{row['nodeid']}", f"Failing test: {row['nodeid']}", commit_sha, now, now),
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
