"""Deterministic integration loop for completed harness worklanes."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from . import db

INTEGRATION_STATUSES = ("needs_verification", "ready_for_integration")
INTEGRATION_WORKTREE = "integrator-mainline"
INTEGRATION_LIMIT = 5


def integrate_once(conn: sqlite3.Connection, root: str | Path, limit: int = INTEGRATION_LIMIT) -> dict[str, int]:
    """Merge and push ready lanes from a clean harness-owned integration worktree."""

    root_path = Path(root).resolve()
    result = {"integrated": 0, "failed": 0, "skipped": 0}
    if not _git_ok(root_path, ["rev-parse", "--is-inside-work-tree"]):
        db.log_event(conn, "integration_skipped", "Repository is not a git worktree")
        return result
    mainline = _mainline_branch(root_path)
    if not mainline:
        db.log_event(conn, "integration_skipped", "Could not determine remote mainline branch")
        return result
    if not _git_ok(root_path, ["remote", "get-url", "origin"]):
        db.log_event(conn, "integration_skipped", "No origin remote is configured")
        return result
    fetch = _git(root_path, ["fetch", "origin", "--prune"])
    if fetch.returncode != 0:
        db.log_event(conn, "integration_skipped", f"git fetch failed: {_output(fetch)}")
        return result
    try:
        worktree = _ensure_integration_worktree(root_path, mainline)
    except RuntimeError as exc:
        db.log_event(conn, "integration_skipped", str(exc))
        return result

    lanes = _ready_lanes(conn, limit)
    if not lanes:
        db.log_event(conn, "integration_idle", "No needs_verification or ready_for_integration lanes with branches")
        return result
    for lane in lanes:
        outcome = _integrate_lane(conn, root_path, worktree, mainline, lane)
        result[outcome] += 1
    conn.commit()
    return result


def _ready_lanes(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in INTEGRATION_STATUSES)
    return list(
        conn.execute(
            f"""
            SELECT * FROM worklanes
            WHERE status IN ({placeholders}) AND branch_name != ''
            ORDER BY priority ASC, ready_for_integration_at IS NULL, ready_for_integration_at ASC, id ASC
            LIMIT ?
            """,
            (*INTEGRATION_STATUSES, limit),
        )
    )


def _integrate_lane(conn: sqlite3.Connection, root: Path, worktree: Path, mainline: str, lane: sqlite3.Row) -> str:
    lane_id = int(lane["id"])
    branch = str(lane["branch_name"])
    candidate = _candidate_ref(root, branch)
    attempt_id = _record_attempt(conn, lane_id, branch)
    if not candidate:
        _finish_attempt(conn, attempt_id, "integration_failed", "", "candidate branch not found")
        db.update_worklane_status(conn, lane_id, "integration_failed", f"Candidate branch not found: {branch}")
        return "failed"

    _reset_worktree(worktree, mainline)
    if _git_stdout(worktree, ["rev-list", "--count", f"HEAD..{candidate}"]) == "0":
        _finish_attempt(conn, attempt_id, "integrated", "already_merged", "")
        db.update_worklane_status(conn, lane_id, "integrated", f"{branch} already merged")
        _delete_integrated_branch(root, branch)
        return "integrated"

    merge = _git(worktree, ["merge", "--no-ff", "--no-commit", candidate])
    if merge.returncode != 0:
        _abort_merge(worktree)
        reason = _output(merge)
        _finish_attempt(conn, attempt_id, "integration_failed", "merge_conflicts", reason)
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        return "failed"

    smoke = _git(worktree, ["diff", "--check", "HEAD"])
    if smoke.returncode != 0:
        _abort_merge(worktree)
        reason = _output(smoke)
        _finish_attempt(conn, attempt_id, "integration_failed", "smoke_failed", reason, tests=["git diff --check HEAD"])
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        return "failed"

    title = re.sub(r"\s+", " ", str(lane["title"])).strip()[:60]
    commit = _git(worktree, ["commit", "-m", f"Integrate worklane #{lane_id}: {title}"])
    if commit.returncode != 0:
        _abort_merge(worktree)
        reason = _output(commit)
        _finish_attempt(conn, attempt_id, "integration_failed", "commit_failed", reason, tests=["git diff --check HEAD"])
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        return "failed"

    push = _git(worktree, ["push", "origin", f"HEAD:{mainline}"])
    if push.returncode != 0:
        reason = _output(push)
        _finish_attempt(conn, attempt_id, "integration_failed", "push_failed", reason, tests=["git diff --check HEAD"])
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        return "failed"

    sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    conn.execute(
        """
        INSERT OR IGNORE INTO commits(sha, branch, worklane_id, agent_name, created_at, summary)
        VALUES (?, ?, ?, 'deterministic-integrator', ?, ?)
        """,
        (sha, mainline, lane_id, db.utc_now(), f"Integrated {branch}"),
    )
    _finish_attempt(conn, attempt_id, "integrated", f"pushed {sha} to origin/{mainline}", "", tests=["git diff --check HEAD"])
    db.update_worklane_status(conn, lane_id, "integrated", f"Integrated {branch} as {sha}")
    db.log_event(conn, "integration_pushed", f"Integrated worklane#{lane_id} and pushed origin/{mainline}", payload={"branch": branch, "sha": sha})
    _delete_integrated_branch(root, branch)
    _reset_worktree(worktree, mainline)
    return "integrated"


def _record_attempt(conn: sqlite3.Connection, lane_id: int, branch: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO integration_attempts(worklane_id, attempt_branch, status, started_at)
        VALUES (?, ?, 'integrating', ?)
        """,
        (lane_id, branch, db.utc_now()),
    )
    return int(cur.lastrowid)


def _finish_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    status: str,
    merge_result: str,
    failure_reason: str,
    tests: list[str] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE integration_attempts
        SET status = ?, merge_result = ?, tests_json = ?, failure_reason = ?, ended_at = ?
        WHERE id = ?
        """,
        (status, merge_result, json.dumps(tests or []), failure_reason, db.utc_now(), attempt_id),
    )


def _ensure_integration_worktree(root: Path, mainline: str) -> Path:
    worktree = root / ".harness" / "worktrees" / INTEGRATION_WORKTREE
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if not (worktree / ".git").exists():
        add = _git(root, ["worktree", "add", "--force", "--detach", str(worktree), f"origin/{mainline}"])
        if add.returncode != 0:
            raise RuntimeError(f"Failed to create integration worktree: {_output(add)}")
    _reset_worktree(worktree, mainline)
    return worktree


def _reset_worktree(worktree: Path, mainline: str) -> None:
    _git(worktree, ["merge", "--abort"])
    _git(worktree, ["checkout", "--detach", f"origin/{mainline}"])
    _git(worktree, ["reset", "--hard", f"origin/{mainline}"])
    _git(worktree, ["clean", "-fd"])


def _candidate_ref(root: Path, branch: str) -> str:
    name = branch.removeprefix("refs/heads/").removeprefix("origin/")
    remote_ref = f"origin/{name}"
    if _git_ok(root, ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"]):
        return remote_ref
    if _git_ok(root, ["rev-parse", "--verify", f"{branch}^{{commit}}"]):
        return branch
    return ""


def _mainline_branch(root: Path) -> str:
    origin_head = _git_stdout(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if origin_head.startswith("origin/"):
        return origin_head.removeprefix("origin/")
    branch = _git_stdout(root, ["branch", "--show-current"])
    if branch in {"main", "master", "trunk"}:
        return branch
    for candidate in ("main", "master", "trunk"):
        if _git_ok(root, ["show-ref", "--verify", f"refs/remotes/origin/{candidate}"]):
            return candidate
    return ""


def _delete_integrated_branch(root: Path, branch: str) -> None:
    name = branch.removeprefix("refs/heads/").removeprefix("origin/")
    if name.startswith(("work/", "worklane/")):
        _git(root, ["push", "origin", "--delete", name])


def _abort_merge(worktree: Path) -> None:
    _git(worktree, ["merge", "--abort"])
    _git(worktree, ["reset", "--hard"])


def _git_ok(root: Path, args: list[str]) -> bool:
    return _git(root, args).returncode == 0


def _git_stdout(root: Path, args: list[str]) -> str:
    result = _git(root, args)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False, timeout=120)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()
