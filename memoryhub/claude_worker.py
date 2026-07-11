from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .core import MemoryStore, memory_home, redact, redact_text, workspace_identity

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "next_action": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "validations": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "summary",
        "next_action",
        "files",
        "validations",
        "blockers",
    ],
    "additionalProperties": False,
}

MAX_LOG_TEXT = 64_000


class WorkspaceBusyError(ValueError):
    pass


class DelegationInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(f"delegation interrupted by signal {signum}")
        self.signum = signum


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


@contextmanager
def workspace_lock(workspace_id: str) -> Iterator[Path]:
    lock_dir = memory_home() / "delegations" / "locks"
    _private_directory(lock_dir)
    path = lock_dir / f"{workspace_id}.lock"
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceBusyError(
                "another Claude delegation is already active in this workspace"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
        handle.flush()
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git_state(cwd: Path) -> dict[str, str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        return {}
    state: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        raw_path = line[3:]
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        path = raw_path.strip('"')
        target = cwd / path
        digest = hashlib.sha256()
        digest.update(status.encode("utf-8"))
        try:
            digest.update(target.read_bytes())
        except (OSError, IsADirectoryError):
            digest.update(b"[missing-or-directory]")
        state[path] = digest.hexdigest()
    return state


def _changed_by_worker(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _inside_allowed(path: str, allowed: list[str]) -> bool:
    normalized = path.strip("./")
    for prefix in allowed:
        clean = prefix.strip().strip("./")
        if clean and (normalized == clean or normalized.startswith(f"{clean}/")):
            return True
    return False


def _prompt(
    *,
    objective: str,
    constraints: list[str],
    validations: list[str],
    allowed_paths: list[str],
    task_id: str | None,
    memory_context: str,
) -> str:
    contract = {
        "objective": objective,
        "constraints": constraints,
        "required_validations": validations,
        "allowed_paths": allowed_paths or ["entire current repository"],
        "memoryhub_task_id": task_id,
    }
    return f"""You are the implementation worker in a sequential Codex -> Claude handoff.

Implement only the bounded contract below in the current working tree. Codex is not editing
concurrently and will review your diff after you exit.

Hard rules:
- Inspect the repository and existing dirty state before editing; preserve all pre-existing work.
- Do not create background agents or processes. Do not use worktrees.
- Do not commit, push, pull, checkout, reset, stash, rebase, or change branches.
- Stay inside allowed_paths when they are specific.
- Run the required validations. Never claim a test passed without running it.
- If blocked, stop safely and report the concrete blocker. Do not wait for user input.
- Finish by returning data matching the requested JSON schema.

TASK CONTRACT
{json.dumps(contract, ensure_ascii=False, indent=2)}

SHARED OPERATIONAL MEMORY (index only; verify it)
{memory_context}
"""


def _parse_structured(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError as error:
        return None, f"Claude output is not JSON: {error}"
    if not isinstance(outer, dict):
        return None, "Claude output must be a JSON object"
    if outer.get("is_error") is True:
        return None, str(outer.get("result") or "Claude reported an error")
    candidate = outer.get("structured_output")
    if candidate is None:
        candidate = outer.get("result")
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                return None, "Claude did not return structured output"
    if not isinstance(candidate, dict):
        return None, "Claude structured output must be an object"
    for field in RESULT_SCHEMA["required"]:
        if field not in candidate:
            return None, f"Claude structured output is missing {field}"
    if candidate.get("status") not in {"done", "blocked"}:
        return None, "Claude structured output has an invalid status"
    for field in ("files", "validations", "blockers"):
        if not isinstance(candidate.get(field), list) or not all(
            isinstance(item, str) for item in candidate[field]
        ):
            return None, f"Claude structured output field {field} must be a string array"
    for field in ("summary", "next_action"):
        if not isinstance(candidate.get(field), str):
            return None, f"Claude structured output field {field} must be a string"
    return candidate, None


def _terminate_group(process: subprocess.Popen[str], grace_seconds: float) -> dict[str, Any]:
    cleanup = {"term_sent": False, "kill_sent": False, "reaped": False, "group_alive": False}

    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            cleanup["term_sent"] = True
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                cleanup["kill_sent"] = True
            except ProcessLookupError:
                pass
            process.wait(timeout=max(1.0, grace_seconds))
    else:
        process.wait()
    cleanup["reaped"] = True

    # Claude may exit after spawning a descendant. Clean the residual process
    # group even on a nominally successful run so nothing is left detached.
    if group_exists():
        try:
            os.killpg(process.pid, signal.SIGTERM)
            cleanup["term_sent"] = True
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace_seconds
        while group_exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if group_exists():
            try:
                os.killpg(process.pid, signal.SIGKILL)
                cleanup["kill_sent"] = True
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 1.0
    while group_exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    cleanup["group_alive"] = group_exists()
    return cleanup


def _write_log(run_id: str, result: dict[str, Any]) -> Path:
    directory = memory_home() / "delegations" / "runs"
    _private_directory(directory)
    path = directory / f"{run_id}.json"
    path.write_text(
        json.dumps(redact(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _checkpoint_result(
    store: MemoryStore,
    *,
    cwd: Path,
    objective: str,
    task_id: str | None,
    result: dict[str, Any],
) -> str:
    structured = result.get("structured") or {}
    status = result["status"]
    summary = str(structured.get("summary") or result.get("error") or status)
    if status == "success":
        next_action = "Codex must review Claude's diff and independently rerun relevant validation."
    else:
        next_action = "Codex must inspect the delegation report and decide whether to fix or retry."
    items = {
        "file": [str(item) for item in structured.get("files", [])],
        "validation": [str(item) for item in structured.get("validations", [])],
        "blocker": [str(item) for item in structured.get("blockers", [])],
    }
    if status != "success" and result.get("error"):
        items["blocker"].append(str(result["error"]))
    return store.checkpoint(
        actor="claude-code",
        cwd=cwd,
        task_id=task_id,
        objective=objective if task_id is None else None,
        status="in_progress",
        summary=f"Claude worker {status}: {summary}",
        next_action=next_action,
        items=items,
    )


def run_delegation(
    *,
    objective: str,
    cwd: Path,
    constraints: list[str] | None = None,
    validations: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    timeout_seconds: int = 900,
    grace_seconds: float = 3.0,
    claude_binary: str = "claude",
    model: str = "opus",
    effort: str = "high",
    permission_mode: str = "acceptEdits",
    task_id: str | None = None,
    max_budget_usd: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not objective.strip():
        raise ValueError("objective cannot be empty")
    if not 1 <= timeout_seconds <= 7200:
        raise ValueError("timeout must be between 1 and 7200 seconds")
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"workspace does not exist: {cwd}")
    constraints = constraints or []
    validations = validations or []
    allowed_paths = allowed_paths or []
    workspace = workspace_identity(cwd)
    store = MemoryStore()
    store.initialize()
    active = store.get_task(task_id) if task_id else store.active_task(workspace["id"])
    effective_task_id = str(active["id"]) if active is not None else None
    memory_context = store.render_context(cwd=cwd, task_id=effective_task_id)
    prompt = _prompt(
        objective=objective,
        constraints=constraints,
        validations=validations,
        allowed_paths=allowed_paths,
        task_id=effective_task_id,
        memory_context=memory_context,
    )
    command = [
        claude_binary,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(RESULT_SCHEMA, separators=(",", ":")),
        "--no-session-persistence",
        "--permission-mode",
        permission_mode,
        "--model",
        model,
        "--effort",
        effort,
        "--name",
        "memoryhub-codex-delegation",
    ]
    if max_budget_usd is not None:
        command.extend(["--max-budget-usd", str(max_budget_usd)])
    run_id = f"delegate_{uuid.uuid4().hex}"
    base_result: dict[str, Any] = {
        "run_id": run_id,
        "status": "dry-run" if dry_run else "starting",
        "workspace": str(cwd),
        "workspace_id": workspace["id"],
        "task_id": effective_task_id,
        "command": command,
        "contract": {
            "objective": objective,
            "constraints": constraints,
            "validations": validations,
            "allowed_paths": allowed_paths,
        },
        "timeout_seconds": timeout_seconds,
    }
    if dry_run:
        base_result["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        path = _write_log(run_id, base_result)
        base_result["log_path"] = str(path)
        return base_result

    with workspace_lock(workspace["id"]):
        before = _git_state(cwd)
        process: subprocess.Popen[str] | None = None
        cleanup: dict[str, Any] = {
            "term_sent": False,
            "kill_sent": False,
            "reaped": False,
            "group_alive": False,
        }
        stdout = ""
        stderr = ""
        status = "failed"
        error: str | None = None
        returncode: int | None = None
        previous_handlers: dict[int, Any] = {}

        def interrupt(signum: int, _frame: Any) -> None:
            raise DelegationInterrupted(signum)

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, interrupt)
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    start_new_session=True,
                )
                try:
                    process.communicate(input=prompt, timeout=timeout_seconds)
                    returncode = process.returncode
                    cleanup = _terminate_group(process, grace_seconds)
                except subprocess.TimeoutExpired:
                    status = "timeout"
                    error = f"Claude exceeded the hard timeout of {timeout_seconds} seconds"
                    cleanup = _terminate_group(process, grace_seconds)
                    returncode = process.returncode
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read().decode("utf-8", errors="replace")
                stderr = stderr_file.read().decode("utf-8", errors="replace")
        except DelegationInterrupted as interrupted:
            status = "interrupted"
            error = str(interrupted)
            if process is not None:
                cleanup = _terminate_group(process, grace_seconds)
                returncode = process.returncode
        except OSError as launch_error:
            status = "launch-error"
            error = str(launch_error)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if process is not None and process.poll() is None:
                cleanup = _terminate_group(process, grace_seconds)
                returncode = process.returncode

        structured: dict[str, Any] | None = None
        if status not in {"timeout", "interrupted", "launch-error"}:
            if returncode != 0:
                status = "failed"
                error = f"Claude exited with status {returncode}"
            else:
                structured, parse_error = _parse_structured(stdout)
                if parse_error:
                    status = "invalid-output"
                    error = parse_error
                elif structured and structured["status"] == "blocked":
                    status = "blocked"
                    error = structured["next_action"] or "Claude reported a blocker"
                else:
                    status = "success"

        after = _git_state(cwd)
        changed_paths = _changed_by_worker(before, after)
        scope_violations = (
            [path for path in changed_paths if not _inside_allowed(path, allowed_paths)]
            if allowed_paths
            else []
        )
        if scope_violations and status == "success":
            status = "scope-violation"
            error = f"Claude changed paths outside the contract: {', '.join(scope_violations)}"
        if cleanup["group_alive"]:
            status = "cleanup-failed"
            error = "Claude process group is still alive after forced cleanup"

        result = {
            **base_result,
            "status": status,
            "returncode": returncode,
            "error": error,
            "structured": structured,
            "changed_paths": changed_paths,
            "scope_violations": scope_violations,
            "preexisting_dirty_paths": sorted(before),
            "cleanup": cleanup,
            "stdout_tail": redact_text(stdout[-MAX_LOG_TEXT:]),
            "stderr_tail": redact_text(stderr[-MAX_LOG_TEXT:]),
        }
        result["task_id"] = _checkpoint_result(
            store,
            cwd=cwd,
            objective=objective,
            task_id=effective_task_id,
            result=result,
        )
        path = _write_log(run_id, result)
        result["log_path"] = str(path)
        return redact(result)


def exit_code(status: str) -> int:
    return {
        "success": 0,
        "dry-run": 0,
        "timeout": 124,
        "interrupted": 130,
        "blocked": 4,
        "scope-violation": 5,
        "invalid-output": 6,
        "cleanup-failed": 7,
        "launch-error": 127,
    }.get(status, 1)
