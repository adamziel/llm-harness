"""Codex command construction with the model and yolo mode pinned by policy."""

from __future__ import annotations

import shlex
from pathlib import Path

CODEX_MODEL = "gpt-5.5-xhigh-fast"
CODEX_YOLO_FLAG = "--yolo"


class UnsafeCodexCommand(ValueError):
    """Raised when a generated command would violate the harness policy."""


def build_codex_command(prompt_file: str | Path, cwd: str | Path) -> str:
    """Return the tmux shell command used for every Codex worker.

    The model and yolo flag are not configurable because the spec requires all
    Codex sessions to run in yolo mode with gpt 5.5 xhigh fast and never
    downgrade.  The prompt is loaded from disk so crash summaries can be appended
    before a restarted agent is launched.
    """

    prompt_path = Path(prompt_file).resolve()
    cwd_path = Path(cwd).resolve()
    command = (
        f"cd {shlex.quote(str(cwd_path))} && "
        f"codex {CODEX_YOLO_FLAG} --model {shlex.quote(CODEX_MODEL)} "
        f"\"$(cat {shlex.quote(str(prompt_path))})\""
    )
    assert_codex_command_safe(command)
    return command


def assert_codex_command_safe(command: str) -> None:
    """Guard against future changes that accidentally drop yolo mode or model pin."""

    if CODEX_YOLO_FLAG not in command:
        raise UnsafeCodexCommand("Codex command must include --yolo")
    if f"--model {CODEX_MODEL}" not in command:
        raise UnsafeCodexCommand(f"Codex command must pin --model {CODEX_MODEL}")
