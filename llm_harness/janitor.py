"""Harness-owned cleanup that avoids deleting unintegrated work."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from . import db


def run_janitor(conn: Any, root: str | Path, max_prompt_age_hours: int = 24) -> dict[str, int]:
    """Clean stale temporary files while preserving branches and worktrees."""

    paths = db.paths_for(root)
    db.ensure_dirs(paths)
    removed_tmp = _empty_directory(paths.tmp)
    removed_prompts = _remove_old_files(paths.prompts, max_prompt_age_hours * 3600)
    db.log_event(
        conn,
        "janitor",
        f"Cleaned {removed_tmp} tmp entries and {removed_prompts} old prompt files",
        payload={"tmp_entries": removed_tmp, "prompt_files": removed_prompts},
    )
    return {"tmp_entries": removed_tmp, "prompt_files": removed_prompts}


def _empty_directory(path: Path) -> int:
    removed = 0
    for child in path.iterdir() if path.exists() else []:
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _remove_old_files(path: Path, max_age_seconds: int) -> int:
    now = time.time()
    removed = 0
    for child in path.glob("*.md") if path.exists() else []:
        try:
            if now - child.stat().st_mtime > max_age_seconds:
                child.unlink()
                removed += 1
        except OSError:
            continue
    return removed
