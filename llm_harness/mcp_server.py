"""Minimal stdio MCP server exposing scheduler-owned deterministic tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import db
from .indexer import code_search, refresh_index

TOOLS = [
    {
        "name": "memory_record_event",
        "description": "Append an event to the harness SQLite memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "message": {"type": "string"},
                "agent_name": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["type", "message"],
        },
    },
    {
        "name": "memory_query",
        "description": "Run a read-only SELECT/WITH/PRAGMA query against harness SQLite memory.",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}, "params": {"type": "array"}},
            "required": ["sql"],
        },
    },
    {
        "name": "memory_update_agent",
        "description": "Update an agent's current_status and notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "status": {"type": "string"},
                "notes": {"type": "string"},
                "ended": {"type": "boolean"},
            },
            "required": ["name", "status"],
        },
    },
    {
        "name": "spawn_agent",
        "description": "Request a new Codex agent through the central harness scheduler.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "requester": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["role", "title", "prompt"],
        },
    },
    {
        "name": "code_search",
        "description": "Search the repository or worktree using ripgrep or the SQLite code index fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "worktree": {"type": "string"},
                "limit": {"type": "integer"},
                "refresh": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
]


class HarnessMCP:
    """Handle the MCP messages agents use for shared memory and spawn requests."""

    def __init__(self, root: str | Path, db_path: str | Path):
        self.root = Path(root).resolve()
        self.db_path = Path(db_path).resolve()
        self.paths = db.paths_for(self.root)
        db.ensure_dirs(self.paths)
        with db.connect(self.db_path) as conn:
            db.init_db(conn)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch JSON-RPC-style MCP methods and return a response if needed."""

        method = message.get("method")
        msg_id = message.get("id")
        try:
            if method == "initialize":
                return self._response(msg_id, {"protocolVersion": "2024-11-05", "serverInfo": {"name": "llm-harness", "version": __version__}, "capabilities": {"tools": {}}})
            if method == "tools/list":
                return self._response(msg_id, {"tools": TOOLS})
            if method == "tools/call":
                params = message.get("params") or {}
                result = self.call_tool(str(params.get("name", "")), params.get("arguments") or {})
                return self._response(msg_id, result)
            if method == "notifications/initialized":
                return None
            return self._error(msg_id, -32601, f"Unknown method: {method}")
        except Exception as exc:  # MCP errors must not crash the agent process.
            return self._error(msg_id, -32000, str(exc))

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one deterministic tool and encode the result as MCP content."""

        with db.connect(self.db_path) as conn:
            db.init_db(conn)
            if name == "memory_record_event":
                event_id = db.log_event(conn, str(args["type"]), str(args["message"]), args.get("agent_name"), args.get("payload") or {})
                return _text({"event_id": event_id})
            if name == "memory_query":
                rows = db.read_only_query(conn, str(args["sql"]), args.get("params") or [])
                return _text(rows)
            if name == "memory_update_agent":
                db.update_agent_status(conn, str(args["name"]), str(args["status"]), args.get("notes"), bool(args.get("ended", False)))
                return _text({"ok": True})
            if name == "spawn_agent":
                request_id = db.queue_spawn_request(
                    conn,
                    role=str(args["role"]),
                    title=str(args["title"]),
                    prompt=str(args["prompt"]),
                    requester=str(args.get("requester", "")),
                    notes=str(args.get("notes", "")),
                )
                return _text({"spawn_request_id": request_id, "status": "queued"})
            if name == "code_search":
                worktree = str(args.get("worktree") or "main")
                if bool(args.get("refresh", False)):
                    refresh_index(conn, self.root, worktree)
                rows = code_search(conn, self.root, str(args["query"]), worktree, int(args.get("limit", 20)))
                return _text(rows)
        raise ValueError(f"Unknown tool: {name}")

    @staticmethod
    def _response(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text(value: Any) -> dict[str, Any]:
    """Encode Python values as the text content shape every MCP client accepts."""

    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True)}]}


def serve(root: str | Path, db_path: str | Path, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Run the stdio MCP loop one JSON object per line."""

    server = HarnessMCP(root, db_path)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            response = server.handle(json.loads(line))
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response), file=stdout, flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint used by `harness mcp` and tests."""

    parser = argparse.ArgumentParser(description="Run the llm-harness MCP server")
    parser.add_argument("--root", default=os.environ.get("HARNESS_ROOT", os.getcwd()))
    parser.add_argument("--db", default=os.environ.get("HARNESS_DB", str(Path(os.getcwd()) / ".harness" / "harness.sqlite3")))
    args = parser.parse_args(argv)
    serve(args.root, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
