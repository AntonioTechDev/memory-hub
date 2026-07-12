from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
MAX_TEXT = 16_000

SECRET_PATTERNS = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|m0-[A-Za-z0-9_-]{12,}|gh[opusr]_[A-Za-z0-9]{12,})\b"),
        "[REDACTED_TOKEN]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s/]+@"),
        r"\1[REDACTED]@",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|token)\s+)[^\s\"']+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|password|secret|cookie)\b\s*[:=]\s*[\"']?[^\s,;\"']+"),
        r"\1=[REDACTED]",
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_duration_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", value)
    if not match:
        raise ValueError(f"invalid duration: {value}")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return amount * multipliers[unit]


def iso_before(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds"
    )


def today_start() -> str:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="milliseconds")


def seconds_since(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def memory_home() -> Path:
    explicit = os.environ.get("MEMORYHUB_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return (Path(data_home).expanduser() / "memoryhub").resolve()
    return (Path.home() / ".local" / "share" / "memoryhub").resolve()


def database_path() -> Path:
    return memory_home() / "memory.db"


def redact_text(value: str, *, limit: int = MAX_TEXT) -> str:
    result = value
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result[:limit]


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value[:200]]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            if re.search(r"(?i)(api.?key|token|password|secret|cookie|authorization)", str(key)):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = redact(item)
        return clean
    return value


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def workspace_identity(cwd: str | Path | None = None) -> dict[str, str]:
    path = Path(cwd or Path.cwd()).expanduser().resolve()
    root_text = _git(path, "rev-parse", "--show-toplevel")
    root = Path(root_text).resolve() if root_text else path
    remote = _git(root, "remote", "get-url", "origin")
    stable_source = f"git:{remote}" if remote else f"path:{root}"
    workspace_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:24]
    return {
        "id": workspace_id,
        "path": str(root),
        "name": root.name,
        "git_remote": redact_text(remote, limit=2000),
    }


def session_id(payload: dict[str, Any], actor: str) -> str:
    value = (
        payload.get("session_id")
        or payload.get("thread-id")
        or payload.get("thread_id")
        or payload.get("conversation_id")
    )
    return redact_text(str(value or f"{actor}-unknown"), limit=500)


def payload_cwd(payload: dict[str, Any]) -> str:
    value = payload.get("cwd") or payload.get("working_directory") or payload.get("workspace")
    return str(value) if value else str(Path.cwd())


def select_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return redact_text(value.strip())
    return ""


class MemoryStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (db_path or database_path()).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL keeps committed transactions safe across process crashes
        # while avoiding a full fsync for every tiny hook event. A host power
        # loss may lose the newest transaction, but cannot corrupt the database.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    name TEXT NOT NULL,
                    git_remote TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    summary TEXT NOT NULL DEFAULT '',
                    next_action TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_workspace_updated
                    ON tasks(workspace_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS task_items (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(task_id, kind, position)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    task_id TEXT REFERENCES tasks(id),
                    started_at TEXT NOT NULL,
                    last_event_at TEXT NOT NULL,
                    ended_at TEXT,
                    PRIMARY KEY(id, actor)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    task_id TEXT REFERENCES tasks(id),
                    content_text TEXT NOT NULL DEFAULT '',
                    content_json TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_events_task_at ON events(task_id, at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_workspace_at ON events(workspace_id, at DESC);
                """
            )
            current = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if current and int(current["value"]) != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema version: {current['value']}")
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def ensure_workspace(self, cwd: str | Path | None = None) -> dict[str, str]:
        workspace = workspace_identity(cwd)
        timestamp = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO workspaces(id, path, name, git_remote, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    path=excluded.path,
                    name=excluded.name,
                    git_remote=excluded.git_remote,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace["id"],
                    workspace["path"],
                    workspace["name"],
                    workspace["git_remote"],
                    timestamp,
                    timestamp,
                ),
            )
        return workspace

    def active_task(
        self,
        workspace_id: str,
        *,
        actor: str | None = None,
        current_session_id: str | None = None,
    ) -> sqlite3.Row | None:
        with self.connect() as db:
            if actor and current_session_id:
                row = db.execute(
                    """
                    SELECT t.* FROM sessions s
                    JOIN tasks t ON t.id=s.task_id
                    WHERE s.id=? AND s.actor=? AND s.workspace_id=?
                    """,
                    (current_session_id, actor, workspace_id),
                ).fetchone()
                if row:
                    return row
            return db.execute(
                """
                SELECT * FROM tasks
                WHERE workspace_id=? AND status IN ('in_progress', 'blocked')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (workspace_id,),
            ).fetchone()

    def create_task(self, workspace_id: str, actor: str, objective: str) -> str:
        timestamp = utc_now()
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        clean_objective = redact_text(objective.strip())
        title = clean_objective.splitlines()[0][:120] or "Untitled task"
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO tasks(
                    id, workspace_id, title, objective, status, updated_by,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'in_progress', ?, ?, ?)
                """,
                (task_id, workspace_id, title, clean_objective, actor, timestamp, timestamp),
            )
        return task_id

    def bind_session(
        self,
        current_session_id: str,
        actor: str,
        workspace_id: str,
        task_id: str | None,
        *,
        ended: bool = False,
    ) -> None:
        timestamp = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sessions(id, actor, workspace_id, task_id, started_at, last_event_at, ended_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, actor) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    task_id=COALESCE(excluded.task_id, sessions.task_id),
                    last_event_at=excluded.last_event_at,
                    ended_at=excluded.ended_at
                """,
                (
                    current_session_id,
                    actor,
                    workspace_id,
                    task_id,
                    timestamp,
                    timestamp,
                    timestamp if ended else None,
                ),
            )

    def append_event(
        self,
        *,
        event_type: str,
        actor: str,
        current_session_id: str,
        workspace_id: str,
        task_id: str | None,
        content_text: str,
        payload: dict[str, Any],
    ) -> str:
        clean_payload = redact(payload)
        explicit_id = clean_payload.get("event_id") or clean_payload.get("hook_event_id")
        dedupe_key = f"{actor}:{explicit_id}" if explicit_id else None
        event_id = f"evt_{uuid.uuid4().hex}"
        timestamp = utc_now()
        with self.connect() as db:
            try:
                db.execute(
                    """
                    INSERT INTO events(
                        id, at, type, actor, session_id, workspace_id, task_id,
                        content_text, content_json, dedupe_key
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        timestamp,
                        event_type,
                        actor,
                        current_session_id,
                        workspace_id,
                        task_id,
                        redact_text(content_text),
                        json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")),
                        dedupe_key,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT id FROM events WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
                return str(existing["id"]) if existing else event_id
        return event_id

    @staticmethod
    def _snapshot_hash(snapshot: dict[str, Any]) -> str:
        encoded = json.dumps(
            redact(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def compaction_snapshot(
        self, workspace_id: str, task_id: str | None
    ) -> dict[str, Any]:
        task = self.get_task(task_id) if task_id else None
        if task is None:
            return {
                "workspace_id": workspace_id,
                "task_id": None,
                "state": "no-active-task",
            }
        task_key = str(task["id"])
        return redact(
            {
                "workspace_id": workspace_id,
                "task_id": task_key,
                "title": str(task["title"]),
                "objective": str(task["objective"]),
                "status": str(task["status"]),
                "summary": str(task["summary"]),
                "next_action": str(task["next_action"]),
                "revision": int(task["revision"]),
                "updated_by": str(task["updated_by"]),
                "items": self.task_items(task_key),
            }
        )

    def _latest_pre_compaction(
        self, *, actor: str, current_session_id: str, workspace_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT content_json FROM events
                WHERE type='pre-compact' AND actor=? AND session_id=? AND workspace_id=?
                ORDER BY at DESC LIMIT 20
                """,
                (actor, current_session_id, workspace_id),
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["content_json"]))
            except json.JSONDecodeError:
                continue
            metadata = payload.get("memoryhub_compaction")
            if isinstance(metadata, dict) and metadata.get("phase") == "pre":
                return metadata
        return None

    def capture_hook(self, event_type: str, actor: str, payload: dict[str, Any]) -> str:
        self.initialize()
        workspace = self.ensure_workspace(payload_cwd(payload))
        current_session_id = session_id(payload, actor)
        task = self.active_task(
            workspace["id"], actor=actor, current_session_id=current_session_id
        )
        text = ""
        if event_type == "user-prompt":
            text = select_text(payload, ("prompt", "user_prompt", "message", "input"))
            if task is None and text:
                task_id = self.create_task(workspace["id"], actor, text)
                task = self.get_task(task_id)
        elif event_type == "stop":
            text = select_text(
                payload,
                ("last_assistant_message", "last-assistant-message", "message", "output"),
            )
        elif event_type == "tool":
            tool = payload.get("tool_name") or payload.get("tool") or "tool"
            text = f"{tool}: {payload.get('tool_response') or payload.get('output') or ''}"
        task_id = str(task["id"]) if task else None
        event_payload = dict(payload)
        if event_type == "pre-compact":
            snapshot = self.compaction_snapshot(workspace["id"], task_id)
            snapshot_id = f"cmp_{uuid.uuid4().hex}"
            event_payload["memoryhub_compaction"] = {
                "phase": "pre",
                "snapshot_id": snapshot_id,
                "snapshot_hash": self._snapshot_hash(snapshot),
                "snapshot": snapshot,
            }
            text = (
                f"compaction snapshot {snapshot_id}; task={task_id or 'none'}; "
                f"next={snapshot.get('next_action') or 'not set'}"
            )
        elif event_type == "post-compact":
            previous = self._latest_pre_compaction(
                actor=actor,
                current_session_id=current_session_id,
                workspace_id=workspace["id"],
            )
            current = self.compaction_snapshot(workspace["id"], task_id)
            current_hash = self._snapshot_hash(current)
            expected_hash = str(previous.get("snapshot_hash", "")) if previous else ""
            verified = bool(previous and expected_hash == current_hash)
            reason = (
                "snapshot-restored"
                if verified
                else "missing-pre-snapshot"
                if previous is None
                else "operational-memory-changed"
            )
            event_payload["memoryhub_compaction"] = {
                "phase": "post",
                "snapshot_id": previous.get("snapshot_id") if previous else None,
                "expected_hash": expected_hash,
                "actual_hash": current_hash,
                "verified": verified,
                "reason": reason,
            }
            text = (
                f"compaction verification: {reason}; "
                f"task={task_id or 'none'}"
            )
        self.bind_session(
            current_session_id,
            actor,
            workspace["id"],
            task_id,
            ended=event_type == "stop",
        )
        self.append_event(
            event_type=event_type,
            actor=actor,
            current_session_id=current_session_id,
            workspace_id=workspace["id"],
            task_id=task_id,
            content_text=text,
            payload=event_payload,
        )
        return self.render_context(cwd=workspace["path"], task_id=task_id)

    def compaction_report(
        self, *, cwd: str | Path | None = None, all_workspaces: bool = False
    ) -> dict[str, Any]:
        self.initialize()
        workspace = self.ensure_workspace(cwd)
        query = """
            SELECT at, type, actor, session_id, workspace_id, task_id, content_json
            FROM events WHERE type IN ('pre-compact', 'post-compact')
        """
        params: tuple[Any, ...] = ()
        if not all_workspaces:
            query += " AND workspace_id=?"
            params = (workspace["id"],)
        query += " ORDER BY at"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()

        snapshots: dict[str, dict[str, Any]] = {}
        malformed = 0
        legacy = 0
        for row in rows:
            try:
                payload = json.loads(str(row["content_json"]))
            except json.JSONDecodeError:
                malformed += 1
                continue
            if "memoryhub_compaction" not in payload:
                legacy += 1
                continue
            metadata = payload.get("memoryhub_compaction")
            if not isinstance(metadata, dict):
                malformed += 1
                continue
            snapshot_id = metadata.get("snapshot_id")
            if not snapshot_id:
                malformed += 1
                continue
            entry = snapshots.setdefault(
                str(snapshot_id),
                {
                    "snapshot_id": str(snapshot_id),
                    "actor": str(row["actor"]),
                    "session_id": str(row["session_id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "task_id": row["task_id"],
                    "pre_at": None,
                    "post_at": None,
                    "verified": None,
                    "reason": None,
                },
            )
            if row["type"] == "pre-compact":
                entry["pre_at"] = str(row["at"])
            else:
                entry["post_at"] = str(row["at"])
                entry["verified"] = bool(metadata.get("verified"))
                entry["reason"] = metadata.get("reason")

        values = list(snapshots.values())
        paired = sum(bool(item["pre_at"] and item["post_at"]) for item in values)
        verified = sum(item["verified"] is True for item in values)
        failed = sum(item["post_at"] is not None and item["verified"] is not True for item in values)
        pending = sum(item["pre_at"] is not None and item["post_at"] is None for item in values)
        return {
            "ok": failed == 0 and malformed == 0,
            "snapshots": values,
            "counts": {
                "total": len(values),
                "paired": paired,
                "verified": verified,
                "pending": pending,
                "failed": failed,
                "malformed": malformed,
                "legacy": legacy,
            },
        }

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def checkpoint(
        self,
        *,
        actor: str,
        cwd: str | Path | None = None,
        task_id: str | None = None,
        title: str | None = None,
        objective: str | None = None,
        status: str | None = None,
        summary: str | None = None,
        next_action: str | None = None,
        items: dict[str, list[str]] | None = None,
        current_session_id: str | None = None,
    ) -> str:
        self.initialize()
        if status is not None and not (next_action or "").strip():
            raise ValueError("a checkpoint with explicit status requires a concrete next action")
        workspace = self.ensure_workspace(cwd)
        task = self.get_task(task_id) if task_id else self.active_task(workspace["id"])
        if task is not None and str(task["workspace_id"]) != workspace["id"]:
            raise ValueError("task belongs to a different workspace")
        if task is None:
            seed = objective or title or summary or "Operational task"
            task_id = self.create_task(workspace["id"], actor, seed)
            task = self.get_task(task_id)
        assert task is not None
        task_id = str(task["id"])
        updates: dict[str, str] = {}
        for key, value in {
            "title": title,
            "objective": objective,
            "status": status,
            "summary": summary,
            "next_action": next_action,
        }.items():
            if value is not None:
                updates[key] = redact_text(value)
        timestamp = utc_now()
        assignments = [f"{key}=?" for key in updates]
        values: list[Any] = list(updates.values())
        assignments.extend(["updated_by=?", "updated_at=?", "revision=revision+1"])
        values.extend([actor, timestamp, task_id])
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", values)
            if items:
                for kind, entries in items.items():
                    if not entries:
                        continue
                    db.execute("DELETE FROM task_items WHERE task_id=? AND kind=?", (task_id, kind))
                    for position, value in enumerate(entries):
                        db.execute(
                            "INSERT INTO task_items(task_id, kind, position, value) VALUES(?, ?, ?, ?)",
                            (task_id, kind, position, redact_text(value)),
                        )
        if current_session_id:
            self.bind_session(current_session_id, actor, workspace["id"], task_id)
        self.append_event(
            event_type="checkpoint",
            actor=actor,
            current_session_id=current_session_id or f"{actor}-checkpoint",
            workspace_id=workspace["id"],
            task_id=task_id,
            content_text=summary or next_action or objective or "checkpoint",
            payload={
                "task_id": task_id,
                "status": status,
                "summary": summary,
                "next_action": next_action,
            },
        )
        return task_id

    def task_items(self, task_id: str) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        with self.connect() as db:
            rows = db.execute(
                "SELECT kind, value FROM task_items WHERE task_id=? ORDER BY kind, position",
                (task_id,),
            ).fetchall()
        for row in rows:
            result.setdefault(str(row["kind"]), []).append(str(row["value"]))
        return result

    def recent_events(self, task_id: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """
                SELECT at, type, actor, content_text FROM events
                WHERE task_id=? AND content_text<>''
                ORDER BY at DESC LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()[::-1]

    def render_context(
        self,
        *,
        cwd: str | Path | None = None,
        task_id: str | None = None,
    ) -> str:
        self.initialize()
        workspace = self.ensure_workspace(cwd)
        task = self.get_task(task_id) if task_id else self.active_task(workspace["id"])
        if task is None:
            return (
                "# Local operational memory\n\n"
                f"Workspace: {workspace['name']}\n\n"
                "No active task. A task will be created from the next user prompt.\n"
            )
        items = self.task_items(str(task["id"]))
        events = self.recent_events(str(task["id"]))

        def bullets(kind: str) -> str:
            values = items.get(kind, [])
            return "\n".join(f"- {value}" for value in values) if values else "- None."

        recent = "\n".join(
            f"- {row['at']} [{row['actor']}/{row['type']}] {row['content_text'][:1000]}"
            for row in events
        ) or "- None."
        return f"""# Local operational memory

Treat this as context, not ground truth. Verify against files, Git, tests and the current user instruction.

- Task ID: `{task['id']}`
- Workspace: `{workspace['name']}`
- Status: `{task['status']}`
- Revision: {task['revision']}
- Updated by: `{task['updated_by']}` at {task['updated_at']}

## Objective

{task['objective'] or task['title']}

## Current state

{task['summary'] or 'No structured checkpoint yet; inspect recent events.'}

## Next action

{task['next_action'] or 'Not set; reconstruct it from recent events and evidence.'}

## Decisions

{bullets('decision')}

## Blockers

{bullets('blocker')}

## Files

{bullets('file')}

## Validations

{bullets('validation')}

## Recent evidence

{recent}
"""

    def list_tasks(
        self,
        *,
        cwd: str | Path | None = None,
        all_workspaces: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.initialize()
        workspace = self.ensure_workspace(cwd)
        with self.connect() as db:
            if all_workspaces:
                rows = db.execute(
                    """
                    SELECT t.*, w.name AS workspace_name FROM tasks t
                    JOIN workspaces w ON w.id=t.workspace_id
                    ORDER BY t.updated_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT t.*, w.name AS workspace_name FROM tasks t
                    JOIN workspaces w ON w.id=t.workspace_id
                    WHERE t.workspace_id=? ORDER BY t.updated_at DESC LIMIT ?
                    """,
                    (workspace["id"], limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def activity(
        self,
        *,
        cwd: str | Path | None = None,
        limit: int = 20,
        stale_seconds: int = 7200,
    ) -> list[dict[str, Any]]:
        self.initialize()
        workspace_id = self.ensure_workspace(cwd)["id"] if cwd else None
        params: list[Any] = []
        where = ""
        if workspace_id:
            where = "WHERE s.workspace_id=?"
            params.append(workspace_id)
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT
                    s.id AS session_id,
                    s.actor,
                    s.started_at,
                    s.last_event_at,
                    s.ended_at,
                    s.task_id,
                    w.name AS workspace_name,
                    w.path AS workspace_path,
                    t.title AS task_title,
                    t.objective,
                    t.status AS task_status,
                    t.summary,
                    t.next_action,
                    (
                        SELECT e.type FROM events e
                        WHERE e.actor=s.actor AND e.session_id=s.id
                        ORDER BY e.at DESC LIMIT 1
                    ) AS last_event_type,
                    (
                        SELECT e.content_text FROM events e
                        WHERE e.actor=s.actor AND e.session_id=s.id
                        ORDER BY e.at DESC LIMIT 1
                    ) AS last_event_text
                FROM sessions s
                JOIN workspaces w ON w.id=s.workspace_id
                LEFT JOIN tasks t ON t.id=s.task_id
                {where}
                ORDER BY s.last_event_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            age = seconds_since(str(item.get("last_event_at") or ""))
            if item.get("ended_at"):
                state = "ended"
            elif age > stale_seconds:
                state = "stale"
            else:
                state = "active"
            warnings: list[str] = []
            if item.get("task_status") in {"in_progress", "blocked"} and not str(item.get("next_action") or "").strip():
                warnings.append("missing-next-action")
            item["state"] = state
            item["age_seconds"] = age
            item["warnings"] = warnings
            result.append(redact(item))
        return result

    def timeline(
        self,
        *,
        cwd: str | Path | None = None,
        agent: str | None = None,
        task_id: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if cwd:
            workspace_id = self.ensure_workspace(cwd)["id"]
            clauses.append("e.workspace_id=?")
            params.append(workspace_id)
        if agent:
            clauses.append("e.actor=?")
            params.append(agent)
        if task_id:
            clauses.append("e.task_id=?")
            params.append(task_id)
        if since:
            clauses.append("e.at>=?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT
                    e.at,
                    e.type,
                    e.actor,
                    e.session_id,
                    e.task_id,
                    e.content_text,
                    w.name AS workspace_name,
                    w.path AS workspace_path,
                    t.title AS task_title,
                    t.status AS task_status
                FROM events e
                JOIN workspaces w ON w.id=e.workspace_id
                LEFT JOIN tasks t ON t.id=e.task_id
                {where}
                ORDER BY e.at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [redact(dict(row)) for row in rows][::-1]

    def cleanup_report(
        self,
        *,
        cwd: str | Path | None = None,
        stale_seconds: int = 864000,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.initialize()
        cutoff = iso_before(stale_seconds)
        task_where = "t.status IN ('in_progress', 'blocked')"
        session_where = "s.ended_at IS NULL"
        params_tasks: list[Any] = []
        params_sessions: list[Any] = []
        params_missing: list[Any] = []
        if cwd:
            workspace_id = self.ensure_workspace(cwd)["id"]
            task_where += " AND t.workspace_id=?"
            session_where += " AND s.workspace_id=?"
            params_tasks.append(workspace_id)
            params_sessions.append(workspace_id)
            params_missing.append(workspace_id)
        params_tasks.append(cutoff)
        params_sessions.append(cutoff)
        missing_where = "t.status IN ('in_progress', 'blocked') AND TRIM(t.next_action)=''"
        if cwd:
            missing_where += " AND t.workspace_id=?"
        with self.connect() as db:
            stale_tasks = db.execute(
                f"""
                SELECT t.id AS task_id, t.title, t.status, t.updated_at,
                       t.next_action, w.name AS workspace_name, w.path AS workspace_path
                FROM tasks t
                JOIN workspaces w ON w.id=t.workspace_id
                WHERE {task_where} AND t.updated_at<?
                ORDER BY t.updated_at
                LIMIT ?
                """,
                [*params_tasks, limit],
            ).fetchall()
            stale_sessions = db.execute(
                f"""
                SELECT s.id AS session_id, s.actor, s.task_id, s.last_event_at,
                       w.name AS workspace_name, w.path AS workspace_path,
                       t.title AS task_title
                FROM sessions s
                JOIN workspaces w ON w.id=s.workspace_id
                LEFT JOIN tasks t ON t.id=s.task_id
                WHERE {session_where} AND s.last_event_at<?
                ORDER BY s.last_event_at
                LIMIT ?
                """,
                [*params_sessions, limit],
            ).fetchall()
            missing_next_action = db.execute(
                f"""
                SELECT t.id AS task_id, t.title, t.status, t.updated_at,
                       w.name AS workspace_name, w.path AS workspace_path
                FROM tasks t
                JOIN workspaces w ON w.id=t.workspace_id
                WHERE {missing_where}
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                [*params_missing, limit],
            ).fetchall()
        stale_task_items = [redact(dict(row)) for row in stale_tasks]
        stale_session_items = [redact(dict(row)) for row in stale_sessions]
        missing_items = [redact(dict(row)) for row in missing_next_action]
        return {
            "mode": "dry-run",
            "cutoff": cutoff,
            "stale_seconds": stale_seconds,
            "counts": {
                "stale_tasks": len(stale_task_items),
                "stale_sessions": len(stale_session_items),
                "missing_next_action": len(missing_items),
            },
            "stale_tasks": stale_task_items,
            "stale_sessions": stale_session_items,
            "missing_next_action": missing_items,
        }

    def resume_task(self, task_id: str, actor: str = "human") -> None:
        timestamp = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='in_progress', updated_by=?, updated_at=? WHERE id=?",
                (actor, timestamp, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"task not found: {task_id}")

    def event_count(self) -> int:
        self.initialize()
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"])
