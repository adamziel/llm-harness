"""SQLite storage used as the harness' durable memory.

The scheduler treats the database as the source of truth for goals, agents,
work lanes, test history, resource samples, and events.  Agent-facing MCP tools
write here too, so the harness can restart without trusting any Codex process to
remember what happened before a crash.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_MS = 60_000
DB_DRIVER_ENV = "HARNESS_DB_DRIVER"
AGENT_TERMINAL_STATUSES = ("crash", "success", "stopped")
AGENT_LIFECYCLE_STATUSES = ("running", *AGENT_TERMINAL_STATUSES)
CARD_STAGES = ("planned", "development", "review", "integration", "done")
STATUS_STAGE = {
    "queued": "planned",
    "assigned": "development",
    "active": "development",
    "working": "development",
    "needs_verification": "review",
    "ready_for_integration": "integration",
    "integrating": "integration",
    "integration_failed": "integration",
    "integrated": "done",
    "done": "done",
}
STAGE_STATUS = {
    "planned": "queued",
    "development": "assigned",
    "review": "needs_verification",
    "integration": "ready_for_integration",
    "done": "done",
}
CODE_PRODUCING_ROLES = {"Developer", "Designer", "Conflict Resolver", "Reproducer"}
NON_ACTIONABLE_REPORT_STATUSES = {"reserve_no_source_edits", "no_source_edits", "not_actionable", "superseded"}
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


def is_non_actionable_report_status(status: str) -> bool:
    """Return whether an agent report says this card should be retired, not retried."""

    normalized = status.strip().lower()
    return normalized in NON_ACTIONABLE_REPORT_STATUSES or normalized.startswith("superseded") or "no_source_edits" in normalized


def is_retryable_error(exc: BaseException) -> bool:
    """Return whether Turso/SQLite reported a write-concurrency conflict."""

    module = exc.__class__.__module__.split(".", 1)[0]
    if not isinstance(exc, sqlite3.OperationalError) and module != "turso":
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message or "conflict" in message


def is_disk_io_error(exc: BaseException) -> bool:
    """Return whether the database driver reported a local disk I/O failure."""

    return "disk i/o error" in str(exc).lower()


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
    """Open Turso when available, otherwise SQLite with conservative pragmas."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(path)
    if connection_driver(conn) == "sqlite":
        conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if connection_driver(conn) == "turso":
            remove_autoincrement_tables(conn)
        if _set_journal_mode(conn, "mvcc") != "mvcc" and connection_driver(conn) == "sqlite":
            _set_journal_mode(conn, "wal")
    except Exception:
        conn.close()
        raise
    try:
        yield conn
    finally:
        conn.close()


def open_connection(path: Path):
    """Open the configured database driver, preferring Turso's local MVCC engine."""

    requested = os.environ.get(DB_DRIVER_ENV, "auto").strip().lower() or "auto"
    if requested in {"auto", "turso", "pyturso"}:
        try:
            turso = _import_turso()
        except ModuleNotFoundError:
            if requested in {"turso", "pyturso"}:
                raise RuntimeError(
                    f"{DB_DRIVER_ENV}=turso requires the pyturso package; install it with `pip install pyturso`."
                ) from None
        else:
            conn = turso.connect(str(path), experimental_features="views,triggers,generated_columns")
            conn.row_factory = turso.Row
            return conn
    if requested in {"auto", "sqlite", "sqlite3"}:
        return sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    raise RuntimeError(f"Unsupported {DB_DRIVER_ENV}={requested!r}; use auto, turso, or sqlite.")


def _import_turso():
    """Import pyturso lazily so the single-file harness can still run without it."""

    import turso

    return turso


def connection_driver(conn: object) -> str:
    """Return the effective database driver name for diagnostics and branching."""

    module = conn.__class__.__module__.split(".", 1)[0]
    return "turso" if module == "turso" else "sqlite"


def remove_autoincrement_tables(conn: sqlite3.Connection) -> None:
    """Rewrite legacy SQLite AUTOINCREMENT tables so Turso MVCC can write them."""

    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
          AND sql LIKE '%AUTOINCREMENT%'
        ORDER BY name
        """
    ).fetchall()
    for row in rows:
        table = row["name"] if isinstance(row, sqlite3.Row) else row[0]
        sql = row["sql"] if isinstance(row, sqlite3.Row) else row[1]
        if not table or not sql:
            continue
        replacement = f"CREATE TABLE {table}"
        if replacement not in sql:
            continue
        temp_table = f"__harness_no_autoincrement_{table}"
        columns = [
            column["name"] if isinstance(column, sqlite3.Row) else column[1]
            for column in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
        ]
        quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
        conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(temp_table)}")
        conn.execute(sql.replace(replacement, f"CREATE TABLE {temp_table}", 1).replace("AUTOINCREMENT", ""))
        conn.execute(
            f"""
            INSERT INTO {_quote_identifier(temp_table)}({quoted_columns})
            SELECT {quoted_columns} FROM {_quote_identifier(table)}
            """
        )
        conn.execute(f"DROP TABLE {_quote_identifier(table)}")
        conn.execute(f"ALTER TABLE {_quote_identifier(temp_table)} RENAME TO {_quote_identifier(table)}")
    if rows:
        conn.commit()


def _quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier produced by this harness, not user input SQL."""

    return '"' + identifier.replace('"', '""') + '"'


def _set_journal_mode(conn: sqlite3.Connection, mode: str) -> str:
    """Set a journal mode when supported and return the mode SQLite selected."""

    try:
        row = conn.execute(f"PRAGMA journal_mode = {mode}").fetchone()
    except Exception as exc:
        if "disk i/o error" in str(exc).lower():
            return ""
        raise
    if row is None:
        return ""
    value = row[0] if not isinstance(row, sqlite3.Row) else row[0]
    return str(value).lower()


def journal_mode(conn: sqlite3.Connection) -> str:
    """Return the active SQLite/Turso journal mode."""

    row = conn.execute("PRAGMA journal_mode").fetchone()
    if row is None:
        return ""
    value = row[0] if not isinstance(row, sqlite3.Row) else row[0]
    return str(value).lower()


def begin_concurrent(conn: sqlite3.Connection) -> bool:
    """Start a Turso concurrent write transaction when MVCC is active."""

    if journal_mode(conn) != "mvcc":
        return False
    conn.execute("BEGIN CONCURRENT")
    return True


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
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            source_agent TEXT NOT NULL DEFAULT '',
            round INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            agent_name TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY,
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
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS worklanes (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            acceptance_criteria TEXT NOT NULL DEFAULT '',
            owner_agent_id INTEGER,
            role_type TEXT NOT NULL DEFAULT 'Developer',
            card_type TEXT NOT NULL DEFAULT 'implementation',
            priority INTEGER NOT NULL DEFAULT 100,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'planned',
            review_required INTEGER NOT NULL DEFAULT 1,
            integration_required INTEGER NOT NULL DEFAULT 1,
            base_branch TEXT NOT NULL DEFAULT '',
            branch_name TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            dependencies TEXT NOT NULL DEFAULT '[]',
            conflict_risk TEXT NOT NULL DEFAULT 'unknown',
            expected_metric_impact REAL NOT NULL DEFAULT 0,
            integration_queue TEXT NOT NULL DEFAULT '',
            test_evidence TEXT NOT NULL DEFAULT '[]',
            source_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            planned_at TEXT,
            assigned_at TEXT,
            last_activity_at TEXT,
            review_ready_at TEXT,
            reviewed_at TEXT,
            ready_for_integration_at TEXT,
            integrated_at TEXT,
            done_at TEXT,
            abandoned_at TEXT,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'broadcast',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
        );

        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'broadcast',
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            delivered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_reports (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            agent_name TEXT NOT NULL DEFAULT '',
            card_id INTEGER,
            worklane_id INTEGER,
            role TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worktrees (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL DEFAULT '',
            owner_agent TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            base_commit TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            last_activity_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS commits (
            id INTEGER PRIMARY KEY,
            sha TEXT NOT NULL UNIQUE,
            branch TEXT NOT NULL DEFAULT '',
            worklane_id INTEGER,
            agent_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS integration_attempts (
            id INTEGER PRIMARY KEY,
            worklane_id INTEGER NOT NULL,
            attempt_branch TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'integrating',
            merge_result TEXT NOT NULL DEFAULT '',
            tests_json TEXT NOT NULL DEFAULT '[]',
            failure_reason TEXT,
            remote_ref TEXT NOT NULL DEFAULT '',
            remote_sha TEXT NOT NULL DEFAULT '',
            push_result TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE TABLE IF NOT EXISTS spawn_requests (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            requester TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            agent_name TEXT NOT NULL DEFAULT '',
            card_id INTEGER,
            notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS card_stage_transitions (
            id INTEGER PRIMARY KEY,
            card_id INTEGER NOT NULL,
            from_stage TEXT NOT NULL DEFAULT '',
            to_stage TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resource_samples (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            cpu_percent REAL NOT NULL,
            ram_percent REAL NOT NULL,
            disk_free_gb REAL NOT NULL,
            load1 REAL NOT NULL DEFAULT 0,
            process_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS metric_samples (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value REAL NOT NULL,
            target REAL NOT NULL,
            percent_ready REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS test_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            command TEXT NOT NULL,
            commit_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            full_log TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS test_results (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES test_runs(id) ON DELETE CASCADE,
            nodeid TEXT NOT NULL,
            file TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            duration REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS bug_reports (
            id INTEGER PRIMARY KEY,
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
            id INTEGER PRIMARY KEY,
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
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_index (
            id INTEGER PRIMARY KEY,
            worktree TEXT NOT NULL,
            path TEXT NOT NULL,
            mtime REAL NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(worktree, path)
        );

        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(current_status);
        CREATE INDEX IF NOT EXISTS idx_worklanes_status ON worklanes(status);
        CREATE INDEX IF NOT EXISTS idx_messages_target_status ON messages(target, status);
        CREATE INDEX IF NOT EXISTS idx_agent_messages_target_status ON agent_messages(target, status);
        CREATE INDEX IF NOT EXISTS idx_worktrees_owner ON worktrees(owner_agent, status);
        CREATE INDEX IF NOT EXISTS idx_integration_attempts_lane ON integration_attempts(worklane_id, status);
        CREATE INDEX IF NOT EXISTS idx_spawn_requests_status ON spawn_requests(status);
        CREATE INDEX IF NOT EXISTS idx_card_stage_transitions_card ON card_stage_transitions(card_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_resource_samples_ts ON resource_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_test_runs_started ON test_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_bugs_node_status ON bug_reports(test_nodeid, status);
        CREATE INDEX IF NOT EXISTS idx_issues_key_status ON issues(issue_key, status);
        CREATE INDEX IF NOT EXISTS idx_code_index_worktree_path ON code_index(worktree, path);
        """
    )
    ensure_card_schema(conn)
    ensure_worklane_compat(conn)
    ensure_mcp_compat_columns(conn)
    if get_meta(conn, "schema_version") != str(SCHEMA_VERSION):
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    set_meta(conn, "db_driver", connection_driver(conn))
    conn.commit()


def ensure_card_schema(conn: sqlite3.Connection) -> None:
    """Add card-board columns to older harness databases and backfill stages."""

    _ensure_columns(
        conn,
        "worklanes",
        [
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("goal", "TEXT NOT NULL DEFAULT ''"),
            ("acceptance_criteria", "TEXT NOT NULL DEFAULT ''"),
            ("owner_agent_id", "INTEGER"),
            ("card_type", "TEXT NOT NULL DEFAULT 'implementation'"),
            ("stage", "TEXT NOT NULL DEFAULT 'planned'"),
            ("review_required", "INTEGER NOT NULL DEFAULT 1"),
            ("integration_required", "INTEGER NOT NULL DEFAULT 1"),
            ("base_branch", "TEXT NOT NULL DEFAULT ''"),
            ("branch_name", "TEXT NOT NULL DEFAULT ''"),
            ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
            ("dependencies", "TEXT NOT NULL DEFAULT '[]'"),
            ("conflict_risk", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("integration_queue", "TEXT NOT NULL DEFAULT ''"),
            ("test_evidence", "TEXT NOT NULL DEFAULT '[]'"),
            ("source_key", "TEXT NOT NULL DEFAULT ''"),
            ("planned_at", "TEXT"),
            ("assigned_at", "TEXT"),
            ("review_ready_at", "TEXT"),
            ("reviewed_at", "TEXT"),
            ("ready_for_integration_at", "TEXT"),
            ("integrated_at", "TEXT"),
            ("done_at", "TEXT"),
            ("abandoned_at", "TEXT"),
        ],
    )
    _ensure_columns(
        conn,
        "agent_reports",
        [
            ("card_id", "INTEGER"),
            ("worklane_id", "INTEGER"),
            ("stage", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _ensure_columns(
        conn,
        "integration_attempts",
        [
            ("remote_ref", "TEXT NOT NULL DEFAULT ''"),
            ("remote_sha", "TEXT NOT NULL DEFAULT ''"),
            ("push_result", "TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _ensure_columns(conn, "spawn_requests", [("card_id", "INTEGER")])
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS card_stage_transitions (
            id INTEGER PRIMARY KEY,
            card_id INTEGER NOT NULL,
            from_stage TEXT NOT NULL DEFAULT '',
            to_stage TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worklanes_stage ON worklanes(stage, priority, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worklanes_source_key ON worklanes(source_key, stage, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worklanes_queue ON worklanes(integration_queue, status, priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_reports_card ON agent_reports(card_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_reports_worklane ON agent_reports(worklane_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_spawn_requests_card ON spawn_requests(card_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_card_stage_transitions_card ON card_stage_transitions(card_id, created_at)")
    now = utc_now()
    conn.execute(
        """
        UPDATE worklanes
        SET
            stage = CASE status
                WHEN 'queued' THEN 'planned'
                WHEN 'assigned' THEN 'development'
                WHEN 'active' THEN 'development'
                WHEN 'working' THEN 'development'
                WHEN 'needs_verification' THEN 'review'
                WHEN 'ready_for_integration' THEN 'integration'
                WHEN 'integrating' THEN 'integration'
                WHEN 'integration_failed' THEN 'integration'
                WHEN 'integrated' THEN 'done'
                WHEN 'done' THEN 'done'
                ELSE stage
            END,
            card_type = CASE
                WHEN card_type != '' THEN card_type
                WHEN role_type IN ('Developer', 'Designer', 'Conflict Resolver', 'Reproducer') THEN 'implementation'
                ELSE 'advisory'
            END,
            integration_required = CASE
                WHEN role_type IN ('Developer', 'Designer', 'Conflict Resolver', 'Reproducer') THEN integration_required
                ELSE 0
            END,
            review_required = CASE WHEN review_required IS NULL THEN 1 ELSE review_required END,
            planned_at = COALESCE(planned_at, created_at, ?),
            review_ready_at = CASE WHEN status = 'needs_verification' THEN COALESCE(review_ready_at, last_activity_at, created_at, ?) ELSE review_ready_at END,
            reviewed_at = CASE WHEN status IN ('ready_for_integration', 'integrating', 'integration_failed') THEN COALESCE(reviewed_at, last_activity_at, created_at, ?) ELSE reviewed_at END,
            done_at = CASE WHEN status IN ('integrated', 'done') THEN COALESCE(done_at, integrated_at, last_activity_at, created_at, ?) ELSE done_at END
        """,
        (now, now, now, now),
    )


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]]) -> None:
    """Add missing SQLite columns using definitions from the current schema."""

    existing = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_xinfo({table})")
    }
    for name, definition in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
        object_type = ""
    if object_type != "view":
        create_view = "CREATE VIEW" if connection_driver(conn) == "turso" else "CREATE VIEW IF NOT EXISTS"
        conn.execute(
            f"""
            {create_view} work_lanes AS
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
            FROM worklanes
            """
        )
    if connection_driver(conn) == "turso":
        return
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS work_lanes_insert INSTEAD OF INSERT ON work_lanes
        BEGIN
            INSERT INTO worklanes(
                id, title, role_type, status, branch_name, worktree_path,
                expected_metric_impact, stage, integration_required, created_at, planned_at, last_activity_at, notes
            ) VALUES (
                NEW.id,
                COALESCE(NEW.title, ''),
                COALESCE(NEW.role, 'Developer'),
                COALESCE(NEW.status, 'queued'),
                COALESCE(NEW.branch, ''),
                COALESCE(NEW.worktree, ''),
                COALESCE(NEW.expected_metric_delta, 0),
                CASE COALESCE(NEW.status, 'queued')
                    WHEN 'queued' THEN 'planned'
                    WHEN 'assigned' THEN 'development'
                    WHEN 'needs_verification' THEN 'review'
                    WHEN 'ready_for_integration' THEN 'integration'
                    WHEN 'integration_failed' THEN 'integration'
                    WHEN 'integrated' THEN 'done'
                    ELSE 'planned'
                END,
                CASE WHEN COALESCE(NEW.role, 'Developer') IN ('Developer', 'Designer', 'Conflict Resolver', 'Reproducer') THEN 1 ELSE 0 END,
                COALESCE(NEW.ts, datetime('now')),
                COALESCE(NEW.ts, datetime('now')),
                COALESCE(NEW.ts, datetime('now')),
                COALESCE(NEW.notes, '')
            );
        END;

        CREATE TRIGGER IF NOT EXISTS work_lanes_update INSTEAD OF UPDATE ON work_lanes
        BEGIN
            UPDATE worklanes SET
                title = COALESCE(NEW.title, title),
                role_type = COALESCE(NEW.role, role_type),
                status = COALESCE(NEW.status, status),
                stage = CASE COALESCE(NEW.status, status)
                    WHEN 'queued' THEN 'planned'
                    WHEN 'assigned' THEN 'development'
                    WHEN 'needs_verification' THEN 'review'
                    WHEN 'ready_for_integration' THEN 'integration'
                    WHEN 'integration_failed' THEN 'integration'
                    WHEN 'integrated' THEN 'done'
                    ELSE stage
                END,
                branch_name = COALESCE(NEW.branch, branch_name),
                worktree_path = COALESCE(NEW.worktree, worktree_path),
                expected_metric_impact = COALESCE(NEW.expected_metric_delta, expected_metric_impact),
                notes = COALESCE(NEW.notes, notes),
                last_activity_at = datetime('now')
            WHERE id = OLD.id;
        END;

        CREATE TRIGGER IF NOT EXISTS work_lanes_delete INSTEAD OF DELETE ON work_lanes
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
    with connect(paths.db) as conn:
        init_db(conn)
    return paths


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
    card_type: str = "",
    stage: str = "",
    review_required: bool = True,
    integration_required: bool | None = None,
    source_key: str = "",
) -> int:
    """Create a refined worklane row and return its durable id."""

    now = utc_now()
    resolved_stage = normalize_stage(stage or stage_for_status(status))
    resolved_card_type = card_type or card_type_for_role(role_type)
    resolved_integration_required = role_requires_integration(role_type) if integration_required is None else bool(integration_required)
    cur = conn.execute(
        """
        INSERT INTO worklanes(
            title, description, goal, acceptance_criteria, role_type, card_type, priority,
            status, stage, review_required, integration_required, expected_metric_impact,
            source_key, created_at, planned_at, last_activity_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            description,
            goal,
            acceptance_criteria,
            role_type,
            resolved_card_type,
            priority,
            status,
            resolved_stage,
            1 if review_required else 0,
            1 if resolved_integration_required else 0,
            expected_metric_impact,
            source_key,
            now,
            now if resolved_stage == "planned" else None,
            now,
            notes,
        ),
    )
    card_id = int(cur.lastrowid)
    record_card_transition(conn, card_id, "", resolved_stage, "created")
    log_event(conn, "worklane_created", title, payload={"worklane_id": card_id, "card_id": card_id, "status": status, "stage": resolved_stage})
    return card_id


def create_card(conn: sqlite3.Connection, title: str, **fields: Any) -> int:
    """Create a durable card; implementation cards are represented as worklanes."""

    return queue_worklane(conn, title, **fields)


def normalize_stage(stage: str) -> str:
    """Return a known card stage, defaulting unknown input to planned."""

    return stage if stage in CARD_STAGES else "planned"


def stage_for_status(status: str) -> str:
    """Map legacy worklane statuses onto the card board."""

    return STATUS_STAGE.get(status, "planned")


def status_for_stage(stage: str, integration_required: bool = True) -> str:
    """Return the legacy status that best represents a card stage."""

    if stage == "done" and integration_required:
        return "integrated"
    return STAGE_STATUS.get(stage, "queued")


def card_type_for_role(role_type: str) -> str:
    """Classify cards by the kind of worker output they authorize."""

    if role_type in {"Coordinator", "Manager"}:
        return "control-plane"
    if role_type in {"Integrator", "Verifier", "Auditor", "Conflict Resolver"}:
        return "integration-support"
    if role_type in CODE_PRODUCING_ROLES:
        return "implementation"
    return "advisory"


def role_requires_integration(role_type: str) -> bool:
    """Return whether this role normally produces source changes to merge."""

    return role_type in CODE_PRODUCING_ROLES


def record_card_transition(
    conn: sqlite3.Connection,
    card_id: int,
    from_stage: str,
    to_stage: str,
    reason: str = "",
    agent_name: str = "",
) -> None:
    """Append a stage transition audit row."""

    conn.execute(
        """
        INSERT INTO card_stage_transitions(card_id, from_stage, to_stage, reason, agent_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (card_id, from_stage, to_stage, reason, agent_name, utc_now()),
    )


def claim_next_worklane(conn: sqlite3.Connection, agent_name: str, worktree: str, branch: str) -> sqlite3.Row | None:
    """Assign the highest-priority planned implementation card to a developer."""

    roles = tuple(sorted(CODE_PRODUCING_ROLES))
    placeholders = ",".join("?" for _ in roles)
    lane = conn.execute(
        f"""
        SELECT * FROM worklanes
        WHERE stage = 'planned' AND role_type IN ({placeholders})
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """,
        roles,
    ).fetchone()
    if lane is None:
        return None
    return assign_card(conn, int(lane["id"]), agent_name, worktree, branch)


def assign_card(conn: sqlite3.Connection, card_id: int, agent_name: str, worktree: str = "", branch: str = "") -> sqlite3.Row:
    """Move one planned card into development and attach it to an agent."""

    now = utc_now()
    agent = conn.execute("SELECT id FROM agents WHERE name = ?", (agent_name,)).fetchone()
    lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (card_id,)).fetchone()
    old_stage = str(lane["stage"] if lane else "")
    conn.execute(
        """
        UPDATE worklanes
        SET status = 'assigned', stage = 'development', owner_agent_id = ?, branch_name = ?, worktree_path = ?,
            assigned_at = ?, last_activity_at = ?
        WHERE id = ?
        """,
        (agent["id"] if agent else None, branch, worktree, now, now, card_id),
    )
    if old_stage != "development":
        record_card_transition(conn, card_id, old_stage, "development", "assigned", agent_name)
    log_event(conn, "worklane_assigned", f"Assigned worklane#{card_id} to {agent_name}", agent_name=agent_name, payload={"worklane_id": card_id, "card_id": card_id, "stage": "development"})
    return conn.execute("SELECT * FROM worklanes WHERE id = ?", (card_id,)).fetchone()


def update_worklane_status(conn: sqlite3.Connection, lane_id: int, status: str, notes: str | None = None) -> None:
    """Move one worklane through its per-lane lifecycle and card stage."""

    now = utc_now()
    row = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
    stage = stage_for_status(status)
    old_stage = str(row["stage"] if row else "")
    fields = ["status = ?", "stage = ?", "last_activity_at = ?"]
    params: list[Any] = [status, stage, now]
    if status == "ready_for_integration":
        fields.extend(["ready_for_integration_at = ?", "integration_queue = ?"])
        params.extend([now, "ready_fast_path"])
    elif status == "integrated":
        fields.extend(["integrated_at = ?", "done_at = ?"])
        params.extend([now, now])
    elif status == "abandoned":
        fields.append("abandoned_at = ?")
        params.append(now)
    if stage == "planned":
        fields.append("planned_at = COALESCE(planned_at, ?)")
        params.append(now)
    elif stage == "review":
        fields.append("review_ready_at = COALESCE(review_ready_at, ?)")
        params.append(now)
    elif stage == "integration":
        fields.append("reviewed_at = COALESCE(reviewed_at, ?)")
        params.append(now)
    elif stage == "done":
        fields.append("done_at = COALESCE(done_at, ?)")
        params.append(now)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    params.append(lane_id)
    conn.execute(f"UPDATE worklanes SET {', '.join(fields)} WHERE id = ?", tuple(params))
    if old_stage != stage:
        record_card_transition(conn, lane_id, old_stage, stage, status)
    log_event(conn, "worklane_status", f"worklane#{lane_id} -> {status}", payload={"worklane_id": lane_id, "card_id": lane_id, "status": status, "stage": stage})


def move_card_stage(
    conn: sqlite3.Connection,
    card_id: int,
    stage: str,
    status: str | None = None,
    notes: str | None = None,
    reason: str = "",
    agent_name: str = "",
) -> None:
    """Move a card between board stages using the legacy status for compatibility."""

    target_stage = normalize_stage(stage)
    row = conn.execute("SELECT * FROM worklanes WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown card_id {card_id}")
    old_stage = str(row["stage"])
    integration_required = bool(row["integration_required"])
    next_status = status or status_for_stage(target_stage, integration_required)
    now = utc_now()
    fields = ["stage = ?", "status = ?", "last_activity_at = ?"]
    params: list[Any] = [target_stage, next_status, now]
    if target_stage == "planned":
        fields.extend(["planned_at = COALESCE(planned_at, ?)", "owner_agent_id = NULL"])
        params.append(now)
    elif target_stage == "development":
        fields.append("assigned_at = COALESCE(assigned_at, ?)")
        params.append(now)
    elif target_stage == "review":
        fields.append("review_ready_at = COALESCE(review_ready_at, ?)")
        params.append(now)
    elif target_stage == "integration":
        fields.extend(["reviewed_at = COALESCE(reviewed_at, ?)", "ready_for_integration_at = COALESCE(ready_for_integration_at, ?)", "integration_queue = COALESCE(NULLIF(integration_queue, ''), 'ready_fast_path')"])
        params.extend([now, now])
    elif target_stage == "done":
        fields.append("done_at = COALESCE(done_at, ?)")
        params.append(now)
        if integration_required:
            fields.append("integrated_at = COALESCE(integrated_at, ?)")
            params.append(now)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    params.append(card_id)
    conn.execute(f"UPDATE worklanes SET {', '.join(fields)} WHERE id = ?", tuple(params))
    if old_stage != target_stage:
        record_card_transition(conn, card_id, old_stage, target_stage, reason or next_status, agent_name)
    log_event(conn, "card_stage", f"card#{card_id} {old_stage or '?'} -> {target_stage}", agent_name=agent_name or None, payload={"card_id": card_id, "from_stage": old_stage, "to_stage": target_stage, "status": next_status})


def requeue_card(conn: sqlite3.Connection, card_id: int, notes: str | None = None) -> None:
    """Return a card to planned so Python can assign it again."""

    move_card_stage(conn, card_id, "planned", "queued", notes, reason="requeued")


def complete_card(conn: sqlite3.Connection, card_id: int, notes: str | None = None) -> None:
    """Mark a card done; integration-required cards should only call this after push."""

    row = conn.execute("SELECT integration_required FROM worklanes WHERE id = ?", (card_id,)).fetchone()
    status = "integrated" if row and row["integration_required"] else "done"
    move_card_stage(conn, card_id, "done", status, notes, reason="completed")


def retire_card(conn: sqlite3.Connection, card_id: int, notes: str | None = None, reason: str = "retired", agent_name: str = "") -> None:
    """Mark a non-actionable card stale without pretending it was integrated."""

    row = conn.execute("SELECT stage FROM worklanes WHERE id = ?", (card_id,)).fetchone()
    old_stage = str(row["stage"] if row else "")
    now = utc_now()
    conn.execute(
        """
        UPDATE worklanes
        SET stage = 'done',
            status = 'stale',
            owner_agent_id = NULL,
            done_at = COALESCE(done_at, ?),
            last_activity_at = ?,
            notes = COALESCE(?, notes)
        WHERE id = ?
        """,
        (now, now, notes, card_id),
    )
    if old_stage != "done":
        record_card_transition(conn, card_id, old_stage, "done", reason, agent_name)
    log_event(conn, "card_retired", f"card#{card_id} retired as stale", agent_name=agent_name or None, payload={"card_id": card_id, "reason": reason})


def review_ready_cards(conn: sqlite3.Connection, limit: int = 10) -> int:
    """Accept structured reports in review and route cards to integration or done."""

    moved = 0
    rows = conn.execute(
        """
        SELECT * FROM worklanes
        WHERE stage = 'review'
        ORDER BY priority ASC, review_ready_at IS NULL, review_ready_at ASC, id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        report = conn.execute(
            "SELECT * FROM agent_reports WHERE card_id = ? OR worklane_id = ? ORDER BY id DESC LIMIT 1",
            (row["id"], row["id"]),
        ).fetchone()
        if report is None:
            continue
        if row["integration_required"]:
            move_card_stage(conn, int(row["id"]), "integration", "ready_for_integration", "Review accepted structured report", reason="review_passed")
        else:
            complete_card(conn, int(row["id"]), "Review accepted structured report")
        moved += 1
    return moved


def record_agent_report(conn: sqlite3.Connection, report: Mapping[str, Any]) -> int:
    """Store a structured agent report and route card stages deterministically."""

    now = utc_now()
    agent_name = str(report.get("agent_id") or report.get("integrator_id") or report.get("agent_name") or "")
    card_value = report.get("card_id") or report.get("worklane_id")
    lane_id = int(card_value) if str(card_value or "").isdigit() else None
    reported_stage = str(report.get("stage") or "")
    status = str(report.get("status") or "")
    agent = conn.execute("SELECT role FROM agents WHERE name = ?", (agent_name,)).fetchone()
    cur = conn.execute(
        """
        INSERT INTO agent_reports(created_at, agent_name, card_id, worklane_id, role, stage, status, report_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now, agent_name, lane_id, lane_id, agent["role"] if agent else "", reported_stage, status, json.dumps(report, sort_keys=True)),
    )
    accepted = bool(lane_id and reported_stage and status)
    if accepted:
        lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
        if lane is None:
            accepted = False
        elif lane["stage"] == "development" and status in {"ready_for_review", "needs_verification", "ready_for_integration", "completed", "complete"}:
            move_card_stage(conn, lane_id, "review", "needs_verification", str(report.get("summary") or ""), reason="agent_report", agent_name=agent_name)
        elif lane["stage"] == "development" and is_non_actionable_report_status(status):
            retire_card(conn, lane_id, str(report.get("summary") or status), reason=status, agent_name=agent_name)
            if agent_name:
                conn.execute(
                    "UPDATE agents SET current_status = 'running', ended_at = NULL, last_seen_at = ?, notes = ? WHERE name = ?",
                    (now, f"Awaiting reassignment after non-actionable card#{lane_id}: {status}", agent_name),
                )
        elif lane["stage"] == "review" and status in {"review_passed", "accepted", "ready_for_integration", "done"}:
            if lane["integration_required"]:
                move_card_stage(conn, lane_id, "integration", "ready_for_integration", str(report.get("summary") or ""), reason="review_report", agent_name=agent_name)
            else:
                complete_card(conn, lane_id, str(report.get("summary") or ""))
        elif status in {"blocked", "failed"}:
            conn.execute(
                "UPDATE worklanes SET status = ?, notes = ?, last_activity_at = ? WHERE id = ?",
                (status, str(report.get("summary") or ""), now, lane_id),
            )
    elif lane_id and status:
        log_event(
            conn,
            "agent_report_rejected",
            "agent_report did not include required card_id/worklane_id, stage, and status; card state was not changed",
            agent_name=agent_name,
            payload={"report_id": int(cur.lastrowid), "card_id": lane_id, "status": status, "stage": reported_stage},
        )
    log_event(
        conn,
        "agent_report",
        f"{agent_name or 'agent'} reported {status or 'status'}",
        agent_name=agent_name,
        payload={"report_id": int(cur.lastrowid), "worklane_id": lane_id, "card_id": lane_id, "accepted": accepted},
    )
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

    normalized_role = "Coordinator" if role == "Manager" else role
    card_id = find_or_create_card(
        conn,
        source_key=f"spawn:{normalized_role}:{title}",
        title=title,
        role_type=normalized_role,
        description=prompt,
        notes=notes,
        priority=25 if normalized_role != "Developer" else 100,
        integration_required=role_requires_integration(normalized_role),
    )
    cur = conn.execute(
        """
        INSERT INTO spawn_requests(ts, requester, role, title, prompt, card_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), requester, role, title, prompt, card_id, notes),
    )
    log_event(conn, "spawn_request", f"{requester or 'agent'} requested {normalized_role}: {title}", payload={"card_id": card_id})
    return int(cur.lastrowid)


def find_or_create_card(conn: sqlite3.Connection, source_key: str, title: str, **fields: Any) -> int:
    """Return an unresolved card for a deterministic source, creating one if needed."""

    if source_key:
        existing = conn.execute(
            """
            SELECT id FROM worklanes
            WHERE source_key = ? AND stage != 'done' AND status NOT IN ('abandoned', 'cancelled', 'stale')
            ORDER BY id LIMIT 1
            """,
            (source_key,),
        ).fetchone()
        if existing:
            return int(existing["id"])
    fields.setdefault("source_key", source_key)
    return create_card(conn, title, **fields)


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
