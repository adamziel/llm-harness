"""Repository search MCP backend with ripgrep first and SQLite fallback."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".sh",
    ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go",
}


def refresh_index(conn: sqlite3.Connection, root: str | Path, worktree: str = "main") -> int:
    """Index small text files so agents have a deterministic search fallback."""

    root_path = Path(root).resolve()
    count = 0
    for path in root_path.rglob("*"):
        if not path.is_file() or _skip(path, root_path):
            continue
        if path.suffix and path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            stat = path.stat()
            if stat.st_size > 1_000_000:
                continue
            content = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(root_path))
        conn.execute(
            """
            INSERT INTO code_index(worktree, path, mtime, content) VALUES (?, ?, ?, ?)
            ON CONFLICT(worktree, path) DO UPDATE SET mtime = excluded.mtime, content = excluded.content
            """,
            (worktree, rel, stat.st_mtime, content),
        )
        count += 1
    conn.commit()
    return count


def code_search(conn: sqlite3.Connection, root: str | Path, query: str, worktree: str = "main", limit: int = 20) -> list[dict[str, Any]]:
    """Search code using ripgrep when available, otherwise the SQLite index."""

    rg = _ripgrep(root, query, limit)
    if rg:
        return rg
    rows = conn.execute(
        """
        SELECT path, content FROM code_index
        WHERE worktree = ? AND content LIKE ?
        ORDER BY path LIMIT ?
        """,
        (worktree, f"%{query}%", limit),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        line_no, line = _first_matching_line(row["content"], query)
        results.append({"path": row["path"], "line": line_no, "text": line, "source": "sqlite-index"})
    return results


def _ripgrep(root: str | Path, query: str, limit: int) -> list[dict[str, Any]]:
    """Use ripgrep's open-source index/search behavior when installed."""

    try:
        completed = subprocess.run(
            ["rg", "--line-number", "--no-heading", "--color", "never", "--", query, str(root)],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode not in (0, 1):
        return []
    results = []
    root_path = Path(root).resolve()
    for line in completed.stdout.splitlines()[:limit]:
        path, sep, rest = line.partition(":")
        if not sep:
            continue
        line_no, sep, text = rest.partition(":")
        try:
            rel = str(Path(path).resolve().relative_to(root_path))
        except ValueError:
            rel = path
        results.append({"path": rel, "line": int(line_no or 0), "text": text, "source": "ripgrep"})
    return results


def _first_matching_line(content: str, query: str) -> tuple[int, str]:
    for index, line in enumerate(content.splitlines(), start=1):
        if query in line:
            return index, line.strip()
    return 0, ""


def _skip(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    ignored = {".git", ".harness", "__pycache__", ".pytest_cache", "node_modules", ".venv"}
    return any(part in ignored for part in rel.parts) or os.access(path, os.R_OK) is False
