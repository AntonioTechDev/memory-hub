from __future__ import annotations

import json
import os
import pty
import select
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .autopilot import GoalContract, ProviderUsage, TaskContract, parse_usage
from .claude_worker import _terminate_group
from .core import memory_home, redact, redact_text

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "done_when": {"type": "array", "items": {"type": "string"}},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "non_goals": {"type": "array", "items": {"type": "string"}},
                "complexity": {"type": "string", "enum": ["xs", "s", "m", "l", "xl"]},
                "risk": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": [
                "objective",
                "done_when",
                "constraints",
                "non_goals",
                "complexity",
                "risk",
            ],
            "additionalProperties": False,
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "validations": {"type": "array", "items": {"type": "string"}},
                    "allowed_paths": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "profile": {
                        "type": "string",
                        "enum": ["fast", "builder", "senior", "lead"],
                    },
                    "risk": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "parallel_safe": {"type": "boolean"},
                },
                "required": [
                    "id",
                    "title",
                    "objective",
                    "acceptance_criteria",
                    "validations",
                    "allowed_paths",
                    "constraints",
                    "depends_on",
                    "profile",
                    "risk",
                    "parallel_safe",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "tasks"],
    "additionalProperties": False,
}

WORKER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "next_action": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "validations": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "next_action", "files", "validations", "blockers"],
    "additionalProperties": False,
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
        "answers": {"type": "array", "items": {"type": "string"}},
        "required_fixes": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "summary", "answers", "required_fixes", "residual_risks"],
    "additionalProperties": False,
}


def planner_prompt(objective: str, cwd: Path, fallback_goal: GoalContract) -> str:
    return f"""You are the stateless technical lead for Memory Hub Autopilot.

Convert the user's goal into the smallest executable plan that can be independently validated.
Inspect the trusted repository at {cwd}. Read its AGENTS.md/CLAUDE.md, build scripts, CI and tests.
Do not edit files. Return JSON matching the schema.

USER GOAL
{objective}

DETERMINISTIC FALLBACK CLASSIFICATION
{json.dumps(fallback_goal.to_dict(), ensure_ascii=False, indent=2)}

Rules:
- xs must be exactly one task; s at most two; m at most five; l at most eight.
- Every task must map to a goal completion criterion and have concrete validation.
- Use executable repository-native validation commands when discoverable.
- Mark parallel_safe only for genuinely independent scopes and list allowed_paths.
- Prefer the smallest patch. No speculative cleanup, dependencies, abstractions or deployment.
- Use fast for trivial local edits, builder for bounded implementation, senior for cross-cutting
  debugging/refactors, and lead only for planning or exceptionally risky implementation.
- Ask no questions. If ambiguity is material, create a bounded discovery task whose output is a
  concrete decision or mark the relevant implementation task constrained by that discovery.
"""


def reviewer_prompt(goal: GoalContract, tasks: list[TaskContract]) -> str:
    return f"""You are the independent final reviewer for Memory Hub Autopilot.

Inspect the current repository, Git diff and available test evidence. Do not edit files. Return
JSON matching the schema. Pass only when every goal criterion is proven. Explicitly answer:
1) requested outcome; 2) criteria coverage; 3) minimal diff; 4) regressions; 5) meaningful tests;
6) scope; 7) unnecessary abstractions/dependencies; 8) integration safety; 9) residual risks.

GOAL
{json.dumps(goal.to_dict(), ensure_ascii=False, indent=2)}

TASK CONTRACTS
{json.dumps([task.to_dict() for task in tasks], ensure_ascii=False, indent=2)}
"""


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _extract_claude(stdout: str) -> dict[str, Any]:
    outer = json.loads(stdout)
    if not isinstance(outer, dict):
        raise ValueError("Claude output is not an object")
    if outer.get("is_error") is True:
        raise ValueError(str(outer.get("result") or "Claude reported an error"))
    candidate = outer.get("structured_output", outer.get("result"))
    if isinstance(candidate, str):
        candidate = json.loads(candidate)
    if not isinstance(candidate, dict):
        raise ValueError("Claude did not return structured JSON")
    return candidate


def _validate_shape(value: dict[str, Any], schema: dict[str, Any]) -> None:
    for field in schema.get("required", []):
        if field not in value:
            raise ValueError(f"structured output is missing {field}")
    if schema.get("additionalProperties") is False:
        unknown = set(value) - set(schema.get("properties", {}))
        if unknown:
            raise ValueError(f"structured output has unknown fields: {sorted(unknown)}")


def provider_command(
    *,
    provider: str,
    cwd: Path,
    prompt: str,
    schema_path: Path,
    output_path: Path,
    model: str,
    effort: str,
    writable: bool,
    codex_binary: str = "codex",
    claude_binary: str = "claude",
) -> tuple[list[str], str | None]:
    if provider == "codex":
        command = [
            codex_binary,
            "exec",
            "--ephemeral",
            "--color",
            "never",
            "--sandbox",
            "workspace-write" if writable else "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-C",
            str(cwd),
        ]
        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--config", f"model_reasoning_effort={effort}"])
        command.append(prompt)
        return command, None
    if provider == "claude":
        command = [
            claude_binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema_path.read_text(encoding="utf-8"),
            "--no-session-persistence",
            "--permission-mode",
            "acceptEdits" if writable else "dontAsk",
            "--effort",
            effort or "medium",
            "--name",
            "memoryhub-autopilot",
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command, "claude"
    raise ValueError(f"unsupported provider: {provider}")


def run_json_agent(
    *,
    provider: str,
    cwd: Path,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str,
    writable: bool,
    timeout_seconds: int,
    codex_binary: str = "codex",
    claude_binary: str = "claude",
    on_start: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    if not 1 <= timeout_seconds <= 14_400:
        raise ValueError("timeout must be between 1 and 14400 seconds")
    runtime = memory_home() / "autopilot" / "runtime"
    _private_dir(runtime)
    run_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=runtime) as raw_temp:
        temp = Path(raw_temp)
        schema_path = temp / "schema.json"
        output_path = temp / "output.json"
        schema_path.write_text(json.dumps(schema, separators=(",", ":")), encoding="utf-8")
        command, parser = provider_command(
            provider=provider,
            cwd=cwd,
            prompt=prompt,
            schema_path=schema_path,
            output_path=output_path,
            model=model,
            effort=effort,
            writable=writable,
            codex_binary=codex_binary,
            claude_binary=claude_binary,
        )
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env={**os.environ, "MEMORYHUB_SUPPRESS_HOOKS": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            return {
                "status": "launch-error",
                "provider": provider,
                "error": redact_text(str(error)),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "cleanup": {"reaped": True, "group_alive": False},
            }
        if on_start is not None:
            try:
                on_start(process.pid)
            except Exception as error:
                cleanup = _terminate_group(process, 2.0)
                return {
                    "status": "launch-error",
                    "provider": provider,
                    "error": redact_text(f"could not persist provider pid: {error}"),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "cleanup": cleanup,
                }
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout = ""
            stderr = ""
        cleanup = _terminate_group(process, 2.0)
        elapsed = round(time.monotonic() - started, 3)
        if timed_out:
            return {
                "status": "timeout",
                "provider": provider,
                "error": f"provider exceeded {timeout_seconds}s",
                "elapsed_seconds": elapsed,
                "cleanup": cleanup,
            }
        combined = f"{stdout}\n{stderr}".strip()
        rate_limited = any(
            token in combined.lower()
            for token in ("rate limit", "usage limit", "quota exceeded", "limit reached")
        )
        if process.returncode != 0:
            return {
                "status": "rate_limited" if rate_limited else "failed",
                "provider": provider,
                "returncode": process.returncode,
                "error": redact_text(combined or "provider exited without output", limit=8000),
                "elapsed_seconds": elapsed,
                "cleanup": cleanup,
            }
        try:
            raw_output = stdout if parser == "claude" else output_path.read_text(encoding="utf-8")
            structured = _extract_claude(raw_output) if parser == "claude" else json.loads(raw_output)
            if not isinstance(structured, dict):
                raise ValueError("structured output is not an object")
            _validate_shape(structured, schema)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {
                "status": "invalid-output",
                "provider": provider,
                "error": redact_text(str(error)),
                "raw_excerpt": redact_text(combined, limit=8000),
                "elapsed_seconds": elapsed,
                "cleanup": cleanup,
            }
        return {
            "status": "success",
            "provider": provider,
            "structured": redact(structured),
            "elapsed_seconds": elapsed,
            "cleanup": cleanup,
        }


def query_usage(
    provider: str,
    *,
    timeout_seconds: int = 12,
    codex_binary: str = "codex",
    claude_binary: str = "claude",
) -> ProviderUsage:
    binary = codex_binary if provider == "codex" else claude_binary
    if not shutil.which(binary) and not Path(binary).is_file():
        return ProviderUsage(provider, status="unavailable", source="binary lookup")
    if provider == "codex":
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            assert process.stdin is not None and process.stdout is not None
            requests = [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "memoryhub", "version": "0.5.1"},
                        "capabilities": None,
                    },
                },
                {"method": "initialized", "params": {}},
                {"id": 2, "method": "account/rateLimits/read", "params": None},
            ]
            for request in requests:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
            deadline = time.monotonic() + timeout_seconds
            payload: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.2)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("id") == 2:
                    payload = message.get("result")
                    break
            if not isinstance(payload, dict):
                raise ValueError("Codex app-server returned no rate-limit snapshot")
            buckets = payload.get("rateLimitsByLimitId") or {
                "codex": payload.get("rateLimits", {})
            }
            normalized: dict[str, Any] = {}
            constrained = False
            limited = False
            reset_values: list[int] = []
            for name, snapshot in buckets.items():
                if not isinstance(snapshot, dict):
                    continue
                primary = snapshot.get("primary") or {}
                used = int(primary.get("usedPercent") or 0)
                reset_epoch = primary.get("resetsAt")
                if isinstance(reset_epoch, int):
                    reset_values.append(reset_epoch)
                normalized[str(name)] = {
                    "used_percent": used,
                    "remaining_percent": max(0, 100 - used),
                    "window_minutes": primary.get("windowDurationMins"),
                    "resets_at": reset_epoch,
                }
                limited = limited or bool(snapshot.get("rateLimitReachedType")) or used >= 100
                constrained = constrained or used >= 85
            status = "rate_limited" if limited else "constrained" if constrained else "available"
            reset_at = None
            if reset_values:
                reset_at = datetime.fromtimestamp(min(reset_values), tz=timezone.utc).isoformat()
            return ProviderUsage(
                provider="codex",
                status=status,
                remaining=normalized,
                reset_at=reset_at,
                source="codex app-server account/rateLimits/read",
                raw_excerpt=json.dumps(redact(payload), ensure_ascii=False)[:4000],
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return ProviderUsage(
                "codex", status="unknown", source="codex app-server", raw_excerpt=str(error)
            )
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    try:
        result = subprocess.run(
            [
                binary,
                "-p",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "/usage",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        raw = f"{result.stdout}\n{result.stderr}".strip()
        try:
            outer = json.loads(result.stdout)
            if isinstance(outer, dict) and isinstance(outer.get("result"), str):
                raw = str(outer["result"])
        except json.JSONDecodeError:
            pass
        snapshot = parse_usage(provider, raw, returncode=result.returncode)
        snapshot.source = "claude print /usage"
        return snapshot
    except (OSError, subprocess.TimeoutExpired):
        pass

    command = [binary, "--ax-screen-reader"]
    try:
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(command[0], command, {**os.environ, "TERM": "xterm-256color", "NO_COLOR": "1"})
        chunks: list[bytes] = []
        started = time.monotonic()
        usage_sent = False
        quit_sent = False
        returncode = 0
        while time.monotonic() - started < timeout_seconds:
            elapsed = time.monotonic() - started
            if not usage_sent and elapsed >= 0.8:
                os.write(fd, b"/usage\r")
                usage_sent = True
            if usage_sent and not quit_sent and elapsed >= min(3.0, timeout_seconds * 0.7):
                os.write(fd, b"\x1b/quit\r")
                quit_sent = True
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunks.append(os.read(fd, 65_536))
                except OSError:
                    break
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                returncode = os.waitstatus_to_exitcode(status)
                break
        else:
            returncode = 0 if chunks else 1
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        raw = b"".join(chunks).decode("utf-8", errors="replace")
        return parse_usage(provider, raw, returncode=returncode)
    except OSError as error:
        return ProviderUsage(
            provider,
            status="unknown",
            source=f"{provider} /usage",
            raw_excerpt=redact_text(str(error)),
        )
