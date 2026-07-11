from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .core import MemoryStore

PROTOCOL_VERSION = "2024-11-05"


TOOLS = [
    {
        "name": "memory_context",
        "description": "Load the active local task handoff for a workspace.",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {"cwd": {"type": "string"}, "task_id": {"type": "string"}},
        },
    },
    {
        "name": "memory_checkpoint",
        "description": "Save objective, state, evidence and the exact next action for another agent.",
        "annotations": {"readOnlyHint": False, "openWorldHint": False, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["actor"],
            "properties": {
                "actor": {"type": "string"},
                "cwd": {"type": "string"},
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "status": {"enum": ["in_progress", "blocked", "done", "archived"]},
                "summary": {"type": "string"},
                "next_action": {"type": "string"},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "blockers": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "validations": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "memory_tasks",
        "description": "List local tasks, optionally across all workspaces on this machine.",
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "all_workspaces": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "memory_resume",
        "description": "Mark a known local task active and load its handoff.",
        "annotations": {"readOnlyHint": False, "openWorldHint": False, "destructiveHint": False},
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "actor": {"type": "string"},
            },
        },
    },
]


def response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        value["error"] = error
    else:
        value["result"] = result
    return value


def text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    store = MemoryStore()
    if name == "memory_context":
        return text_result(
            store.render_context(cwd=arguments.get("cwd"), task_id=arguments.get("task_id"))
        )
    if name == "memory_checkpoint":
        task_id = store.checkpoint(
            actor=str(arguments["actor"]),
            cwd=arguments.get("cwd"),
            task_id=arguments.get("task_id"),
            title=arguments.get("title"),
            objective=arguments.get("objective"),
            status=arguments.get("status"),
            summary=arguments.get("summary"),
            next_action=arguments.get("next_action"),
            items={
                "decision": arguments.get("decisions", []),
                "blocker": arguments.get("blockers", []),
                "file": arguments.get("files", []),
                "validation": arguments.get("validations", []),
            },
        )
        return text_result(f"Checkpoint saved for {task_id}")
    if name == "memory_tasks":
        tasks = store.list_tasks(
            cwd=arguments.get("cwd"),
            all_workspaces=bool(arguments.get("all_workspaces")),
            limit=min(int(arguments.get("limit", 20)), 100),
        )
        return text_result(json.dumps(tasks, ensure_ascii=False, indent=2))
    if name == "memory_resume":
        task_id = str(arguments["task_id"])
        store.resume_task(task_id, str(arguments.get("actor", "agent")))
        return text_result(store.render_context(task_id=task_id))
    raise ValueError(f"unknown tool: {name}")


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memoryhub", "version": __version__},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return response(request_id, {})
    if method == "tools/list":
        return response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            return response(
                request_id,
                call_tool(str(params.get("name")), params.get("arguments") or {}),
            )
        except Exception as error:
            return response(request_id, error={"code": -32603, "message": str(error)})
    return response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def serve() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            answer = handle(request)
        except Exception as error:
            answer = response(None, error={"code": -32700, "message": str(error)})
        if answer is not None:
            sys.stdout.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
