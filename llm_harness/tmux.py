"""Small tmux wrapper so every agent remains inspectable by the user."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TmuxPane:
    """Location of a running command in tmux."""

    session: str
    window: str
    pane: str


class TmuxUnavailable(RuntimeError):
    """Raised when tmux-dependent work is requested without tmux installed."""


class Tmux:
    """Command runner for tmux operations that the scheduler can fake in tests."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def available(self) -> bool:
        """Return whether tmux can be invoked on this machine."""

        return shutil_which("tmux") is not None

    def run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run tmux and capture output for deterministic bookkeeping."""

        return self.runner(["tmux", *args], check=check, text=True, capture_output=True)

    def current_or_create_session(self, root: str | Path) -> str:
        """Use the user's current tmux session, or create a detached harness one."""

        if not self.available():
            raise TmuxUnavailable("tmux is not installed or not on PATH")
        if os.environ.get("TMUX"):
            result = self.run(["display-message", "-p", "#S"])
            return result.stdout.strip()
        session = _session_name(root)
        existing = self.run(["has-session", "-t", session], check=False)
        if existing.returncode != 0:
            self.run(["new-session", "-d", "-s", session, "-n", "harness", "bash", "-lc", "printf 'llm harness session ready\\n'; exec bash"])
        return session

    def current_session(self) -> str:
        """Return the current tmux session name, or empty when outside tmux."""

        if not os.environ.get("TMUX"):
            return ""
        result = self.run(["display-message", "-p", "#S"], check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def list_sessions(self) -> list[str]:
        """List known tmux sessions without creating one."""

        result = self.run(["list-sessions", "-F", "#{session_name}"], check=False)
        return result.stdout.splitlines() if result.returncode == 0 else []

    def list_windows(self, session: str) -> dict[str, str]:
        """Return window names and their current pane paths for one session."""

        result = self.run(["list-windows", "-t", f"{session}:", "-F", "#{window_name}\t#{pane_current_path}"], check=False)
        windows: dict[str, str] = {}
        if result.returncode != 0:
            return windows
        for line in result.stdout.splitlines():
            name, _, path = line.partition("\t")
            if name:
                windows[name] = path
        return windows

    def ensure_window(self, session: str, window: str, command: str) -> TmuxPane:
        """Start a named tmux window unless it already exists."""

        session_target = f"{session}:"
        windows = self.run(["list-windows", "-t", session_target, "-F", "#{window_name}"]).stdout.splitlines()
        if window not in windows:
            self.run(["new-window", "-t", session_target, "-n", window, shell_command("bash", "-lc", command)])
        pane = self.run(["display-message", "-p", "-t", f"{session}:{window}", "#{pane_id}"]).stdout.strip()
        return TmuxPane(session=session, window=window, pane=pane)

    def send_prompt(self, target: str, message: str) -> None:
        """Inject text into an existing tmux pane/window and press Enter."""

        self.run(["send-keys", "-t", target, message, "C-m"])

    def capture(self, target: str, lines: int = 200) -> str:
        """Read recent pane output for idle and suspicious sleep checks."""

        result = self.run(["capture-pane", "-p", "-t", target, "-S", f"-{lines}"], check=False)
        return result.stdout if result.returncode == 0 else ""

    def target_exists(self, target: str) -> bool:
        """Return whether a pane/window target still exists in tmux."""

        return self.run(["display-message", "-p", "-t", target, "#{pane_id}"], check=False).returncode == 0

    def kill_window(self, session: str, window: str) -> bool:
        """Kill one tmux window if it still exists."""

        return self.run(["kill-window", "-t", f"{session}:{window}"], check=False).returncode == 0

    def kill_session(self, session: str) -> bool:
        """Kill a harness-created tmux session."""

        return self.run(["kill-session", "-t", session], check=False).returncode == 0

    def switch_to(self, session: str, window: str) -> None:
        """Show the status window when the user is already inside tmux."""

        if os.environ.get("TMUX"):
            self.run(["switch-client", "-t", f"{session}:{window}"], check=False)


def shutil_which(command: str) -> str | None:
    """Local wrapper keeps tmux availability easy to monkeypatch in tests."""

    from shutil import which

    return which(command)


def _session_name(root: str | Path) -> str:
    """Derive a tmux-safe session name from the repository directory."""

    base = Path(root).resolve().name or "repo"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", base)
    return f"llm-harness-{safe}"


def shell_command(*parts: str) -> str:
    """Quote a shell command for watch/status helper windows."""

    return " ".join(shlex.quote(part) for part in parts)
