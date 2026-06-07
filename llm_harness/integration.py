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
            WHERE (stage = 'integration' OR status IN ({placeholders}))
              AND status != 'integration_failed'
              AND branch_name != ''
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
        _queue_conflict_card(conn, lane, "candidate_missing", f"Candidate branch not found: {branch}")
        return "failed"

    if branch_integration_state(root, branch, mainline) == "merged":
        sha = _git_stdout(root, ["rev-parse", f"origin/{mainline}"])
        _finish_attempt(conn, attempt_id, "integrated", "already_merged", "", remote_ref=f"origin/{mainline}", remote_sha=sha, push_result="already on remote")
        db.complete_card(conn, lane_id, f"{branch} already merged on origin/{mainline} at {sha}")
        _delete_integrated_branch(root, branch)
        return "integrated"

    preflight = _preflight_lane(root, mainline, candidate)
    if preflight:
        failure_type, reason, tests = preflight
        _finish_attempt(conn, attempt_id, "integration_failed", failure_type, reason, tests=tests)
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        _queue_conflict_card(conn, lane, failure_type, reason)
        return "failed"

    _reset_worktree(worktree, mainline)
    merge = _git(worktree, ["merge", "--no-ff", "--no-commit", candidate])
    if merge.returncode != 0:
        _abort_merge(worktree)
        reason = _output(merge)
        _finish_attempt(conn, attempt_id, "integration_failed", "merge_conflicts", reason)
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        _queue_conflict_card(conn, lane, "merge_conflicts", reason)
        return "failed"

    smoke = _git(worktree, ["diff", "--check", "HEAD"])
    if smoke.returncode != 0:
        _abort_merge(worktree)
        reason = _output(smoke)
        _finish_attempt(conn, attempt_id, "integration_failed", "smoke_failed", reason, tests=["git diff --check HEAD"])
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        _queue_conflict_card(conn, lane, "smoke_failed", reason)
        return "failed"

    title = re.sub(r"\s+", " ", str(lane["title"])).strip()[:60]
    commit = _git(worktree, ["commit", "-m", f"Integrate worklane #{lane_id}: {title}"])
    if commit.returncode != 0:
        _abort_merge(worktree)
        reason = _output(commit)
        _finish_attempt(conn, attempt_id, "integration_failed", "commit_failed", reason, tests=["git diff --check HEAD"])
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        _queue_conflict_card(conn, lane, "commit_failed", reason)
        return "failed"

    push = _git(worktree, ["push", "origin", f"HEAD:{mainline}"])
    if push.returncode != 0:
        reason = _output(push)
        _finish_attempt(conn, attempt_id, "integration_failed", "push_failed", reason, tests=["git diff --check HEAD"], push_result=reason)
        db.update_worklane_status(conn, lane_id, "integration_failed", reason)
        _queue_conflict_card(conn, lane, "push_failed", reason)
        return "failed"

    sha = _git_stdout(worktree, ["rev-parse", "HEAD"])
    conn.execute(
        """
        INSERT OR IGNORE INTO commits(sha, branch, worklane_id, agent_name, created_at, summary)
        VALUES (?, ?, ?, 'deterministic-integrator', ?, ?)
        """,
        (sha, mainline, lane_id, db.utc_now(), f"Integrated {branch}"),
    )
    _finish_attempt(conn, attempt_id, "integrated", f"pushed {sha} to origin/{mainline}", "", tests=["git diff --check HEAD"], remote_ref=f"origin/{mainline}", remote_sha=sha, push_result=_output(push) or "pushed")
    db.complete_card(conn, lane_id, f"Integrated {branch} as {sha} and pushed origin/{mainline}")
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


def _queue_conflict_card(conn: sqlite3.Connection, lane: sqlite3.Row, failure_type: str, reason: str) -> None:
    """Create one planned conflict-resolution card for a failed integration lane."""

    lane_id = int(lane["id"])
    branch = str(lane["branch_name"])
    title = f"Resolve integration failure for card #{lane_id}: {lane['title']}"
    db.find_or_create_card(
        conn,
        source_key=f"integration-failure:{lane_id}:{failure_type}:{_source_key_token('origin/' + _candidate_ref_name(branch))}",
        title=title,
        role_type="Conflict Resolver",
        description=(
            f"Integration failed for card #{lane_id} ({lane['title']}) with {failure_type}.\n"
            f"Branch: {branch}\n"
            f"Reason:\n{reason}"
        ),
        goal="Repair the candidate branch and return the original card to integration.",
        acceptance_criteria="Original card can be integrated by the deterministic integration loop.",
        priority=max(0, int(lane["priority"]) - 1),
        integration_required=True,
    )


def _preflight_lane(root: Path, mainline: str, candidate: str) -> tuple[str, str, list[str]] | None:
    """Return an integration failure discovered before mutating the worktree."""

    merge_tree = _git(root, ["merge-tree", f"origin/{mainline}", candidate])
    if merge_tree.returncode != 0 or "CONFLICT" in merge_tree.stdout or "<<<<<<<" in merge_tree.stdout:
        return ("preflight_merge_conflicts", _output(merge_tree) or "merge-tree reported conflicts", ["git merge-tree"])
    diff_check = _git(root, ["diff", "--check", f"origin/{mainline}...{candidate}"])
    if diff_check.returncode != 0:
        return ("preflight_diff_check_failed", _output(diff_check), ["git diff --check"])
    return None


def branch_integration_state(root: str | Path, branch: str, mainline: str | None = None) -> str:
    """Return missing, merged, pending, or unknown for a candidate integration branch."""

    root_path = Path(root).resolve()
    resolved_mainline = mainline or _mainline_branch(root_path)
    if not resolved_mainline:
        return "unknown"
    candidate = _candidate_ref(root_path, branch)
    if not candidate:
        return "missing"
    if _git_stdout(root_path, ["rev-list", "--count", f"origin/{resolved_mainline}..{candidate}"]) == "0":
        return "merged"
    return "pending"


def _finish_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    status: str,
    merge_result: str,
    failure_reason: str,
    tests: list[str] | None = None,
    remote_ref: str = "",
    remote_sha: str = "",
    push_result: str = "",
) -> None:
    conn.execute(
        """
        UPDATE integration_attempts
        SET status = ?, merge_result = ?, tests_json = ?, failure_reason = ?,
            remote_ref = ?, remote_sha = ?, push_result = ?, ended_at = ?
        WHERE id = ?
        """,
        (status, merge_result, json.dumps(tests or []), failure_reason, remote_ref, remote_sha, push_result, db.utc_now(), attempt_id),
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
    name = _candidate_ref_name(branch)
    remote_ref = f"origin/{name}"
    if _git_ok(root, ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"]):
        return remote_ref
    if _git_ok(root, ["rev-parse", "--verify", f"{branch}^{{commit}}"]):
        return branch
    return ""


def _candidate_ref_name(branch: str) -> str:
    """Normalize local or remote branch spelling to the remote branch name."""

    return branch.removeprefix("refs/heads/").removeprefix("origin/")


def _source_key_token(value: str) -> str:
    """Keep source keys readable while avoiding separator collisions."""

    return re.sub(r"[^A-Za-z0-9_.@/-]+", "-", value).strip("-")


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
