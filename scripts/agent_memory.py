#!/usr/bin/env python3
"""Deterministic, dependency-free handoff ledger for coding agents."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tencentdb_backend import BackendError, TencentDBBackend

VERSION = 1
MAX_EVENT_FIELD = 4000
MAX_EVENT_LOG_BYTES = 5 * 1024 * 1024

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|m0-[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9]{12,})\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|token)\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|password|secret|cookie)\b\s*[:=]\s*[\"']?[^\s,;\"']+"), r"\1=[REDACTED]"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def find_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".agent-memory").is_dir():
            return candidate
    return current


def paths(root: Path) -> tuple[Path, Path, Path, Path]:
    memory = root / ".agent-memory"
    return memory, memory / "state.json", memory / "HANDOFF.md", memory / "state.lock"


def default_state(project_id: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "project_id": project_id,
        "revision": 0,
        "updated_at": None,
        "updated_by": None,
        "task": {
            "status": "not_started",
            "objective": "",
            "summary": "",
            "next_action": "",
            "decisions": [],
            "blockers": [],
            "files": [],
            "validations": [],
            "risks": [],
            "checkpoint_at": None,
        },
        "vcs": {},
        "runtime": {
            "last_event_at": None,
            "last_event_actor": None,
            "last_response_excerpt": "",
        },
    }


@contextmanager
def locked(root: Path) -> Iterator[None]:
    memory, _, _, lock_path = paths(root)
    memory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_text(path: Path, value: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_state(root: Path) -> dict[str, Any]:
    _, state_path, _, _ = paths(root)
    if not state_path.exists():
        return default_state(root.name)
    with state_path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != VERSION:
        raise ValueError(f"Unsupported memory schema: {state.get('version')}")
    return state


def redact_text(value: str, *, truncate: bool = True) -> str:
    result = value
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result[:MAX_EVENT_FIELD] if truncate else result


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value[:100]]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            if re.search(r"(?i)(api.?key|token|password|secret|cookie|authorization)", str(key)):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = redact(item)
        return clean
    return value


def run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def vcs_snapshot(root: Path) -> dict[str, Any]:
    status = run_git(root, "status", "--short")
    return {
        "branch": run_git(root, "branch", "--show-current") or None,
        "head": run_git(root, "rev-parse", "--short", "HEAD") or None,
        "dirty": bool(status),
        "status": status.splitlines()[:100],
    }


def bullet(items: list[str], empty: str = "None.") -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def render_handoff(state: dict[str, Any]) -> str:
    task = state["task"]
    runtime = state["runtime"]
    checkpoint_at = task.get("checkpoint_at")
    last_event_at = runtime.get("last_event_at")
    stale = bool(last_event_at and (not checkpoint_at or last_event_at > checkpoint_at))
    stale_note = (
        "> **Checkpoint possibly stale:** agent activity occurred after the last explicit checkpoint.\n\n"
        if stale
        else ""
    )
    vcs = state.get("vcs", {})
    return f"""# Agent handoff

{stale_note}- Project: `{state['project_id']}`
- Revision: {state['revision']}
- Status: `{task['status']}`
- Updated: {state.get('updated_at') or 'never'}
- Actor: {state.get('updated_by') or 'unknown'}
- Git: branch `{vcs.get('branch') or 'n/a'}`, head `{vcs.get('head') or 'n/a'}`, dirty `{vcs.get('dirty', False)}`

## Objective

{task.get('objective') or 'Not set.'}

## Current state

{task.get('summary') or 'No checkpoint has been written yet.'}

## Next action

{task.get('next_action') or 'Not set — the next agent must reconstruct it from evidence.'}

## Decisions

{bullet(task.get('decisions', []))}

## Blockers

{bullet(task.get('blockers', []))}

## Files in scope

{bullet(task.get('files', []))}

## Validation evidence

{bullet(task.get('validations', []))}

## Risks

{bullet(task.get('risks', []))}

## Last observed response (fallback only)

{runtime.get('last_response_excerpt') or 'None.'}
"""


def save_state(root: Path, state: dict[str, Any]) -> None:
    _, state_path, handoff_path, _ = paths(root)
    atomic_json(state_path, state)
    atomic_text(handoff_path, render_handoff(state))


def append_event(root: Path, event: dict[str, Any]) -> None:
    event_path = root / ".agent-memory" / "private" / "events.jsonl"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    if event_path.exists() and event_path.stat().st_size >= MAX_EVENT_LOG_BYTES:
        rotated = event_path.with_suffix(".jsonl.1")
        os.replace(event_path, rotated)
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact(event), ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_hook_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"payload": value}
    except json.JSONDecodeError:
        return {"raw": raw}


def select_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return redact_text(value.strip())
    return ""


def cmd_init(args: argparse.Namespace) -> int:
    root = find_root()
    with locked(root):
        _, state_path, _, _ = paths(root)
        if state_path.exists() and not args.force:
            state = load_state(root)
        else:
            state = default_state(args.project_id or root.name)
            save_state(root, state)
        append_event(root, {"at": now(), "type": "init", "actor": args.actor, "project_id": state["project_id"]})
    print(f"Initialized agent memory for {state['project_id']} at {root / '.agent-memory'}")
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    root = find_root()
    with locked(root):
        state = load_state(root)
        task = state["task"]
        scalar = {
            "status": args.status,
            "objective": args.objective,
            "summary": args.summary,
            "next_action": args.next_action,
        }
        for key, value in scalar.items():
            if value is not None:
                task[key] = redact_text(value)
        if args.clear_blockers:
            task["blockers"] = []
        if args.clear_risks:
            task["risks"] = []
        list_fields = {
            "decisions": args.decision,
            "blockers": args.blocker,
            "files": args.file,
            "validations": args.validation,
            "risks": args.risk,
        }
        for key, values in list_fields.items():
            if values:
                task[key] = list(dict.fromkeys(redact_text(item) for item in values))
        timestamp = now()
        task["checkpoint_at"] = timestamp
        state["revision"] = int(state.get("revision", 0)) + 1
        state["updated_at"] = timestamp
        state["updated_by"] = args.actor
        state["vcs"] = vcs_snapshot(root)
        state["runtime"]["last_response_excerpt"] = ""
        state["runtime"].pop("last_user_prompt", None)
        save_state(root, state)
        append_event(
            root,
            {
                "at": timestamp,
                "type": "checkpoint",
                "actor": args.actor,
                "revision": state["revision"],
                "status": task["status"],
                "summary": task["summary"],
                "next_action": task["next_action"],
            },
        )
    print(f"Checkpoint {state['revision']} saved by {args.actor}")
    return 0


def cmd_context(_: argparse.Namespace) -> int:
    root = find_root()
    print(render_handoff(load_state(root)), end="")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    root = find_root()
    payload = read_hook_payload()
    timestamp = now()
    event: dict[str, Any] = {
        "at": timestamp,
        "type": args.event,
        "actor": args.actor,
        "session_id": payload.get("session_id") or payload.get("thread-id") or payload.get("thread_id"),
    }
    if args.event == "user-prompt":
        event["content"] = select_text(payload, ("prompt", "user_prompt", "message", "input"))
    elif args.event == "stop":
        event["content"] = select_text(payload, ("last_assistant_message", "last-assistant-message", "message"))
    elif args.event == "tool":
        event["tool"] = payload.get("tool_name") or payload.get("tool")
        event["tool_input"] = payload.get("tool_input") or payload.get("input")
        event["tool_response"] = payload.get("tool_response") or payload.get("output")
    elif args.event == "pre-compact":
        event["transcript_path"] = payload.get("transcript_path")

    with locked(root):
        state = load_state(root)
        append_event(root, event)
        runtime = state["runtime"]
        if args.event in {"tool", "stop", "pre-compact"}:
            runtime["last_event_at"] = timestamp
            runtime["last_event_actor"] = args.actor
        if args.event == "stop" and event.get("content"):
            runtime["last_response_excerpt"] = str(event["content"])[:1000]
        state["vcs"] = vcs_snapshot(root)
        save_state(root, state)

    if args.event == "session-start":
        print("=== SHARED AGENT HANDOFF (verify against repository evidence) ===")
        print(render_handoff(state), end="")
    backend_context = use_semantic_backend(root, state, args.event, args.actor, payload, event)
    if backend_context:
        print("\n=== TENCENTDB SEMANTIC RECALL (untrusted context; verify evidence) ===")
        print(backend_context)
    return 0


def backend_identity(state: dict[str, Any], actor: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    session_key = str(state["project_id"])
    user_id = os.environ.get("AGENT_MEMORY_USER_ID", "automa-owner")
    session_id = str(
        payload.get("session_id")
        or payload.get("thread-id")
        or payload.get("thread_id")
        or f"{actor}-unknown"
    )
    return session_key, user_id, session_id


def record_backend_error(root: Path, actor: str, operation: str, error: Exception) -> None:
    safe_error = redact_text(str(error))
    with locked(root):
        append_event(
            root,
            {
                "at": now(),
                "type": "backend-error",
                "actor": actor,
                "backend": "tencentdb",
                "operation": operation,
                "error": safe_error,
            },
        )
    print(f"agent-memory: TencentDB {operation} skipped: {safe_error}", file=sys.stderr)


def latest_user_prompt(root: Path, session_id: str | None, actor: str) -> str:
    event_path = root / ".agent-memory" / "private" / "events.jsonl"
    if not event_path.exists():
        return ""
    for line in reversed(event_path.read_text(encoding="utf-8").splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if candidate.get("type") != "user-prompt":
            continue
        if session_id and candidate.get("session_id") != session_id:
            continue
        if not session_id and candidate.get("actor") != actor:
            continue
        return str(candidate.get("content") or "")
    return ""


def use_semantic_backend(
    root: Path,
    state: dict[str, Any],
    event_name: str,
    actor: str,
    payload: dict[str, Any],
    event: dict[str, Any],
) -> str:
    try:
        backend = TencentDBBackend.from_env()
    except BackendError as error:
        record_backend_error(root, actor, "config", error)
        return ""
    if backend is None:
        return ""

    session_key, user_id, session_id = backend_identity(state, actor, payload)

    def recall_with_l0_fallback(query: str) -> str:
        context = backend.recall(query, session_key, user_id).strip()
        if context:
            return context
        result = backend.search_conversations(query, session_key, limit=5)
        if int(result.get("total") or 0) <= 0:
            return ""
        return str(result.get("results") or "").strip()

    try:
        if event_name == "session-start":
            task = state["task"]
            query = "\n".join(
                part
                for part in (
                    task.get("objective", ""),
                    task.get("summary", ""),
                    task.get("next_action", ""),
                )
                if part
            ) or f"active work for project {state['project_id']}"
            return recall_with_l0_fallback(query)
        if event_name == "user-prompt" and event.get("content"):
            return recall_with_l0_fallback(str(event["content"]))
        if event_name == "stop" and event.get("content"):
            user_content = latest_user_prompt(root, event.get("session_id"), actor)
            if user_content:
                backend.capture(user_content, str(event["content"]), session_key, session_id, user_id)
        if event_name == "pre-compact":
            backend.end_session(session_key, user_id)
    except BackendError as error:
        record_backend_error(root, actor, event_name, error)
    return ""


def cmd_history(args: argparse.Namespace) -> int:
    root = find_root()
    event_path = root / ".agent-memory" / "private" / "events.jsonl"
    if not event_path.exists():
        return 0
    lines = event_path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.limit :]:
        print(line)
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    root = find_root()
    state = load_state(root)
    errors: list[str] = []
    task = state["task"]
    if state.get("version") != VERSION:
        errors.append("unsupported schema version")
    if task.get("status") == "in_progress" and not task.get("next_action"):
        errors.append("in_progress task has no next_action")
    serialized = json.dumps(state, ensure_ascii=False)
    if serialized != redact_text(serialized, truncate=False):
        errors.append("state appears to contain a secret")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"OK: schema v{VERSION}, revision {state['revision']}, project {state['project_id']}")
    return 0


def cmd_backend_health(_: argparse.Namespace) -> int:
    backend = TencentDBBackend.from_env()
    if backend is None:
        print("ERROR: AGENT_MEMORY_BACKEND is not set to tencentdb", file=sys.stderr)
        return 1
    result = backend.health()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"ok", "degraded"} else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--project-id")
    init.add_argument("--actor", default="human")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--actor", required=True)
    checkpoint.add_argument("--status", choices=["not_started", "in_progress", "blocked", "done"])
    checkpoint.add_argument("--objective")
    checkpoint.add_argument("--summary")
    checkpoint.add_argument("--next-action")
    checkpoint.add_argument("--decision", action="append", default=[])
    checkpoint.add_argument("--blocker", action="append", default=[])
    checkpoint.add_argument("--file", action="append", default=[])
    checkpoint.add_argument("--validation", action="append", default=[])
    checkpoint.add_argument("--risk", action="append", default=[])
    checkpoint.add_argument("--clear-blockers", action="store_true")
    checkpoint.add_argument("--clear-risks", action="store_true")
    checkpoint.set_defaults(func=cmd_checkpoint)

    context = commands.add_parser("context")
    context.set_defaults(func=cmd_context)

    hook = commands.add_parser("hook")
    hook.add_argument("--event", required=True, choices=["session-start", "user-prompt", "tool", "stop", "pre-compact"])
    hook.add_argument("--actor", required=True)
    hook.set_defaults(func=cmd_hook)

    history = commands.add_parser("history")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(func=cmd_history)

    validate = commands.add_parser("validate")
    validate.set_defaults(func=cmd_validate)

    backend_health = commands.add_parser("backend-health")
    backend_health.set_defaults(func=cmd_backend_health)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.func(args))
    except (OSError, ValueError, json.JSONDecodeError, BackendError) as error:
        print(f"agent-memory: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
