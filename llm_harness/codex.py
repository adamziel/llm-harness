"""Codex command construction with the model and yolo mode pinned by policy."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

CODEX_MODEL = "gpt-5.5"
CODEX_REASONING_EFFORT = "xhigh"
CODEX_MODEL_LABEL = "gpt-5.5 xhigh fast"
CODEX_YOLO_FLAG = "--yolo"
MCP_SERVER_NAME = "llm-harness"


class UnsafeCodexCommand(ValueError):
    """Raised when a generated command would violate the harness policy."""


def build_codex_command(
    prompt_file: str | Path,
    cwd: str | Path,
    harness_root: str | Path | None = None,
    db_path: str | Path | None = None,
    harness_command: str | Path | None = None,
) -> str:
    """Return the tmux shell command used for every Codex worker.

    The model, reasoning effort, and yolo flag are not configurable because the
    spec requires all Codex sessions to run in yolo mode with gpt 5.5 xhigh fast
    and never downgrade.  The Codex model ID is `gpt-5.5`; `xhigh` is passed as
    `model_reasoning_effort` because it is not part of the model ID.
    """

    prompt_path = Path(prompt_file).resolve()
    cwd_path = Path(cwd).resolve()
    root_path = Path(harness_root or cwd_path).resolve()
    db_path = Path(db_path or root_path / ".harness" / "harness.turso").resolve()
    harness = Path(harness_command or root_path / "harness").resolve()
    config_args = " ".join(shlex.quote(arg) for arg in codex_mcp_config_args(root_path, db_path, harness))
    reasoning_config = f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"'
    command = (
        f"cd {shlex.quote(str(cwd_path))} && "
        f"codex {CODEX_YOLO_FLAG} --model {shlex.quote(CODEX_MODEL)} "
        f"-c {shlex.quote(reasoning_config)} "
        f"{config_args} "
        f"\"$(cat {shlex.quote(str(prompt_path))})\""
    )
    assert_codex_command_safe(command)
    return command


def codex_mcp_config_args(root: str | Path, db_path: str | Path, harness: str | Path) -> list[str]:
    """Build Codex CLI arguments so every worker gets harness MCP tools."""

    root_path = Path(root).resolve()
    db_path = Path(db_path).resolve()
    harness_path = Path(harness).resolve()
    config = [
        f"mcp_servers.{MCP_SERVER_NAME}.command={_toml_string(str(harness_path))}",
        f"mcp_servers.{MCP_SERVER_NAME}.args={_toml_array(['--root', str(root_path), 'mcp'])}",
        f"mcp_servers.{MCP_SERVER_NAME}.env={_toml_inline_table({'HARNESS_ROOT': str(root_path), 'HARNESS_DB': str(db_path)})}",
    ]
    args: list[str] = []
    for item in config:
        args.extend(["-c", item])
    return args


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return "[" + ",".join(_toml_string(value) for value in values) + "]"


def _toml_inline_table(values: dict[str, str]) -> str:
    return "{" + ",".join(f"{key}={_toml_string(value)}" for key, value in values.items()) + "}"


def assert_codex_command_safe(command: str) -> None:
    """Guard against future changes that accidentally drop yolo mode or model pin."""

    if CODEX_YOLO_FLAG not in command:
        raise UnsafeCodexCommand("Codex command must include --yolo")
    if f"--model {CODEX_MODEL}" not in command:
        raise UnsafeCodexCommand(f"Codex command must pin --model {CODEX_MODEL}")
    if f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"' not in command:
        raise UnsafeCodexCommand(f"Codex command must pin model_reasoning_effort={CODEX_REASONING_EFFORT}")
    if f"mcp_servers.{MCP_SERVER_NAME}.command=" not in command:
        raise UnsafeCodexCommand("Codex command must expose harness MCP tools")
