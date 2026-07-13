from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .autopilot import (
    AutopilotStore,
    GoalContract,
    ProviderUsage,
    Route,
    TaskContract,
    build_worker_prompt,
    default_tasks,
    execution_settings,
    parallel_batch,
    route_task,
    validate_plan,
)
from .autopilot_provider import (
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    WORKER_SCHEMA,
    planner_prompt,
    query_usage,
    reviewer_prompt,
    run_json_agent,
)
from .core import memory_home, redact, redact_text, utc_now

SAFE_VALIDATION_PROGRAMS = {
    "python",
    "python3",
    "pytest",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "cargo",
    "go",
    "make",
    "node",
    "git",
}
SAFE_SCRIPT_TOKENS = {
    "test", "tests", "lint", "typecheck", "check", "build", "validate",
    "validation", "smoke", "codegen", "census", "matrix", "orchestration",
}
UNSAFE_SCRIPT_TOKENS = {"deploy", "publish", "release", "upload", "migrate", "seed"}
SAFE_PYTHON_MODULES = {"unittest", "pytest", "compileall", "py_compile"}
SAFE_VALIDATION_ENV = {"CI", "NODE_ENV", "PYTHONPATH"}


def _safe_relative_path(value: str, suffixes: set[str] | None = None) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and (suffixes is None or path.suffix.lower() in suffixes)
    )


def _safe_script_name(value: str) -> bool:
    tokens = {item for item in re.split(r"[:._-]+", value.lower()) if item}
    return bool(tokens & SAFE_SCRIPT_TOKENS) and not bool(tokens & UNSAFE_SCRIPT_TOKENS)


def _safe_argument(value: str) -> bool:
    candidate = value.split("=", 1)[1] if value.startswith("--") and "=" in value else value
    if not candidate or candidate.startswith("-"):
        return "\x00" not in value
    path = Path(candidate)
    return not path.is_absolute() and ".." not in path.parts and "\x00" not in value


def _split_validation_parts(parts: list[str]) -> tuple[list[str], dict[str, str]]:
    env: dict[str, str] = {}
    index = 0
    while index < len(parts) and "=" in parts[index]:
        key, value = parts[index].split("=", 1)
        if key not in SAFE_VALIDATION_ENV or "\x00" in value:
            return [], {}
        if key == "PYTHONPATH" and not all(
            _safe_relative_path(item) for item in value.split(os.pathsep) if item
        ):
            return [], {}
        env[key] = value
        index += 1
    return parts[index:], env


def validation_command_allowed(parts: list[str]) -> bool:
    parts, _ = _split_validation_parts(parts)
    if not parts or parts[0] not in SAFE_VALIDATION_PROGRAMS:
        return False
    program = parts[0]
    if program in {"python", "python3"}:
        if len(parts) >= 3 and parts[1] == "-m":
            module = parts[2]
            args = parts[3:]
            if module == "py_compile":
                return bool(args) and all(
                    _safe_relative_path(item, {".py"}) for item in args
                )
            if module == "compileall":
                return bool(args) and all(
                    item in {"-f", "-q", "-v"} or _safe_relative_path(item)
                    for item in args
                )
            return module in {"unittest", "pytest"} and all(
                _safe_argument(item) for item in args
            )
        if len(parts) >= 2:
            path = Path(parts[1])
            return (
                not path.is_absolute()
                and ".." not in path.parts
                and path.suffix == ".py"
                and (path.name.startswith("test_") or path.name.startswith("run_"))
                and path.parts[0] in {"tests", "scripts"}
                and all(_safe_argument(item) for item in parts[2:])
            )
        return False
    if program == "pytest":
        return all(_safe_argument(item) for item in parts[1:])
    if program in {"npm", "pnpm", "yarn", "bun"}:
        args = parts[1:]
        while len(args) >= 2 and args[0] in {"--filter", "-F"}:
            if not re.fullmatch(r"[A-Za-z0-9@_./*{}-]+", args[1]):
                return False
            if ".." in Path(args[1]).parts:
                return False
            args = args[2:]
        if len(args) >= 2 and args[0] == "run":
            return _safe_script_name(args[1]) and all(_safe_argument(item) for item in args[2:])
        if args and _safe_script_name(args[0]):
            return all(_safe_argument(item) for item in args[1:])
        if len(args) >= 3 and args[:2] == ["exec", "tsx"]:
            return _safe_relative_path(args[2], {".ts", ".tsx"}) and _safe_script_name(
                Path(args[2]).stem
            ) and all(_safe_argument(item) for item in args[3:])
        return False
    if program == "make":
        return len(parts) >= 2 and all(_safe_script_name(target) for target in parts[1:])
    if program == "cargo":
        return (
            len(parts) >= 2
            and parts[1] in {"test", "check", "clippy", "build"}
            and all(_safe_argument(item) for item in parts[2:])
        )
    if program == "go":
        return (
            len(parts) >= 2
            and parts[1] in {"test", "vet", "build"}
            and all(_safe_argument(item) for item in parts[2:])
        )
    if program == "node":
        if len(parts) >= 3 and parts[1] == "--check":
            return all(_safe_relative_path(item, {".js", ".mjs", ".cjs"}) for item in parts[2:])
        if len(parts) >= 2:
            path = Path(parts[1])
            return (
                _safe_relative_path(parts[1], {".js", ".mjs", ".cjs"})
                and bool(path.parts)
                and path.parts[0] in {"scripts", "tests"}
                and _safe_script_name(path.stem)
                and all(_safe_argument(item) for item in parts[2:])
            )
        return False
    if program == "git":
        if len(parts) >= 2 and parts[1] == "status":
            return all(item in {"--short", "--branch", "--porcelain"} for item in parts[2:])
        if len(parts) >= 2 and parts[1] == "diff":
            safe_options = {
                "--", "--cached", "--check", "--exit-code", "--name-only",
                "--quiet", "--staged", "--stat",
            }
            return all(
                item in safe_options if item.startswith("-") else _safe_relative_path(item)
                for item in parts[2:]
            )
        return False
    return False


class AutopilotError(RuntimeError):
    pass


def _emit(event: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"at": utc_now(), "event": event, **redact(payload)},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _git(cwd: Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise AutopilotError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _link_local_dependencies(source: Path, target: Path, *, limit: int = 200) -> int:
    """Reuse ignored dependency trees without copying or allowing them into commits."""
    linked = 0
    dependency_names = {"node_modules", ".venv", "venv"}
    for root, directories, _ in os.walk(source):
        directories[:] = [
            name
            for name in directories
            if name != ".git" and name not in dependency_names
        ]
        root_path = Path(root)
        for name in dependency_names:
            candidate = root_path / name
            if not candidate.is_dir():
                continue
            relative = candidate.relative_to(source)
            destination = target / relative
            if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
                continue
            destination.symlink_to(candidate.resolve(), target_is_directory=True)
            linked += 1
            if linked >= limit:
                return linked
    return linked


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _terminate_orphan_group(pid: int, grace_seconds: float = 2.0) -> bool:
    if not _pid_alive(pid):
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _changed_paths(cwd: Path, base: str = "HEAD") -> list[str]:
    output = _git(cwd, "diff", "--name-only", base, check=False)
    untracked = _git(cwd, "ls-files", "--others", "--exclude-standard", check=False)
    return sorted(set(filter(None, [*output.splitlines(), *untracked.splitlines()])))


def _path_allowed(path: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    clean_path = path.strip().strip("./")
    for raw_pattern in allowed:
        pattern = raw_pattern.strip().strip("./")
        if not pattern:
            continue
        if any(character in pattern for character in "*?["):
            if fnmatch.fnmatchcase(clean_path, pattern):
                return True
            if pattern.endswith("/**") and clean_path == pattern[:-3].rstrip("/"):
                return True
        elif clean_path == pattern or clean_path.startswith(f"{pattern}/"):
            return True
    return False


def run_validations(cwd: Path, commands: list[str], *, timeout: int = 1200) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for command in commands:
        try:
            parts = shlex.split(command)
        except ValueError as error:
            evidence.append({"command": command, "status": "skipped", "reason": str(error)})
            continue
        command_parts, command_env = _split_validation_parts(parts)
        if not validation_command_allowed(parts):
            evidence.append(
                {
                    "command": command,
                    "status": "descriptive",
                    "reason": "not an allow-listed read/build/test validation",
                }
            )
            continue
        started = time.monotonic()
        try:
            result = subprocess.run(
                command_parts,
                cwd=cwd,
                env={**os.environ, **command_env},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            evidence.append(
                {
                    "command": command,
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_code": result.returncode,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "output": redact_text(
                        f"{result.stdout}\n{result.stderr}".strip(), limit=8000
                    ),
                }
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            evidence.append(
                {
                    "command": command,
                    "status": "failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "output": redact_text(str(error), limit=8000),
                }
            )
    return evidence


def validations_passed(evidence: list[dict[str, Any]]) -> bool:
    executable = [item for item in evidence if item["status"] != "descriptive"]
    return bool(executable) and all(item["status"] == "passed" for item in executable)


def is_provider_infrastructure_block(structured: dict[str, Any]) -> bool:
    if structured.get("status") != "blocked":
        return False
    text = "\n".join(
        [
            str(structured.get("summary", "")),
            *[str(item) for item in structured.get("validations", [])],
            *[str(item) for item in structured.get("blockers", [])],
        ]
    ).lower()
    return any(
        token in text
        for token in (
            "sandbox initialization",
            "bwrap:",
            "workspace was not writable",
            "workspace write failed",
            "failed rtm_newaddr",
            "this command requires approval",
            "required approval",
            "pending user approval",
            "approval-capable",
            "no approval prompt",
        )
    )


class AutopilotRunner:
    def __init__(
        self,
        *,
        store: AutopilotStore | None = None,
        codex_binary: str = "codex",
        claude_binary: str = "claude",
        provider_timeout: int = 3600,
        validation_timeout: int = 1200,
        refresh_provider_usage: bool = True,
    ) -> None:
        self.store = store or AutopilotStore()
        self.memory = self.store.memory
        self.codex_binary = codex_binary
        self.claude_binary = claude_binary
        self.provider_timeout = provider_timeout
        self.validation_timeout = validation_timeout
        self.refresh_provider_usage = refresh_provider_usage
        self._git_lock = threading.Lock()

    def _provider_args(self) -> dict[str, str]:
        return {"codex_binary": self.codex_binary, "claude_binary": self.claude_binary}

    def refresh_usage(self) -> dict[str, ProviderUsage]:
        if self.refresh_provider_usage:
            for provider in ("codex", "claude"):
                snapshot = query_usage(provider, **self._provider_args())
                self.store.save_usage(snapshot)
        return self.store.usage()

    def reap_orphan_provider_runs(self, job_id: str) -> int:
        reaped = 0
        for run in self.store.active_runs(job_id):
            pid = int(run["pid"]) if run["pid"] else None
            if pid and pid != os.getpid() and not _terminate_orphan_group(pid):
                raise AutopilotError(f"could not terminate orphan provider group {pid}")
            try:
                self.store.finish_run(
                    str(run["id"]),
                    status="interrupted",
                    result={"recovered": True, "orphan_pid": pid},
                    error="provider run orphaned by runner interruption",
                )
            except ValueError:
                # The previous runner may have finalized it between the snapshot and reap.
                continue
            reaped += 1
        return reaped

    def plan(self, job_id: str, cwd: Path) -> tuple[GoalContract, list[TaskContract], dict[str, Any]]:
        _emit("planning_started", job_id=job_id)
        job = self.store.get_job(job_id)
        fallback_goal = GoalContract.from_dict(json.loads(str(job["goal_json"])))
        usage = self.refresh_usage()
        provider = str(job["lead_provider"])
        if provider == "auto":
            provider = "codex" if usage["codex"].status not in {"unavailable", "rate_limited"} else "claude"
        route = Route(
            provider=provider,
            model=execution_settings(provider, "lead")[0],
            effort=execution_settings(provider, "lead")[1],
            profile="lead",
            reason="lead planning",
        )
        run_id = self.store.create_run(
            job_id=job_id, task_id=None, role="orchestrator", route=route, pid=None
        )
        result = run_json_agent(
            provider=route.provider,
            cwd=cwd,
            prompt=planner_prompt(str(job["objective"]), cwd, fallback_goal),
            schema=PLAN_SCHEMA,
            model=route.model,
            effort=route.effort,
            writable=False,
            timeout_seconds=min(self.provider_timeout, 1800),
            on_start=lambda pid: self.store.update_run_pid(run_id, pid),
            **self._provider_args(),
        )
        self.store.finish_run(
            run_id,
            status=result["status"],
            result=result,
            error=str(result.get("error", "")),
        )
        if result["status"] == "rate_limited":
            self.store.save_usage(
                ProviderUsage(provider, status="rate_limited", source="orchestrator error")
            )
        if result["status"] == "success":
            try:
                structured = result["structured"]
                goal = GoalContract.from_dict(structured["goal"])
                tasks = [TaskContract.from_dict(item) for item in structured["tasks"]]
                validate_plan(goal, tasks)
                _emit("plan_ready", job_id=job_id, task_count=len(tasks))
                return goal, tasks, result
            except (KeyError, TypeError, ValueError) as error:
                result = {**result, "status": "invalid-plan", "error": str(error)}
        # Planning failures fail soft to one conservative task. A worker still
        # has bounded scope, and the final reviewer prevents false completion.
        tasks = default_tasks(fallback_goal, cwd)
        _emit("plan_fallback", job_id=job_id, task_count=len(tasks), status=result["status"])
        return fallback_goal, tasks, result

    def _job_root(self, job_id: str) -> Path:
        path = memory_home() / "autopilot" / "jobs" / job_id
        _private_dir(path)
        return path

    def ensure_integration_worktree(self, job_id: str, cwd: Path) -> tuple[Path, str]:
        job = self.store.get_job(job_id)
        stored = Path(str(job["integration_path"])) if job["integration_path"] else None
        branch = str(job["integration_branch"] or f"memoryhub/autopilot-{job_id[3:11]}")
        if stored and stored.is_dir() and (_git(stored, "rev-parse", "--is-inside-work-tree", check=False) == "true"):
            _link_local_dependencies(self.store.job_workspace_path(job_id), stored)
            return stored, branch
        if _git(cwd, "status", "--porcelain"):
            raise AutopilotError("Autopilot requires a clean starting worktree")
        base_ref = str(job["base_ref"] or _git(cwd, "rev-parse", "HEAD"))
        path = self._job_root(job_id) / "integration"
        with self._git_lock:
            if path.exists():
                _git(cwd, "worktree", "remove", "--force", str(path), check=False)
            branch_exists = bool(_git(cwd, "show-ref", "--verify", f"refs/heads/{branch}", check=False))
            args = ["worktree", "add"]
            if not branch_exists:
                args.extend(["-b", branch])
            else:
                args.append(str(path))
                args.append(branch)
                _git(cwd, *args)
                self.store.update_job(
                    job_id, integration_branch=branch, integration_path=str(path)
                )
                _link_local_dependencies(self.store.job_workspace_path(job_id), path)
                return path, branch
            args.extend([str(path), base_ref])
            _git(cwd, *args)
        self.store.update_job(job_id, integration_branch=branch, integration_path=str(path))
        _link_local_dependencies(self.store.job_workspace_path(job_id), path)
        return path, branch

    def _prepare_task_worktree(
        self, job_id: str, integration: Path, contract: TaskContract, attempt: int
    ) -> tuple[Path, str]:
        root = self._job_root(job_id) / "tasks"
        _private_dir(root)
        path = root / f"{contract.id}-a{attempt}"
        branch = f"memoryhub/{job_id[3:11]}-{contract.id}-a{attempt}"
        with self._git_lock:
            if path.exists():
                _git(integration, "worktree", "remove", "--force", str(path), check=False)
            _git(integration, "branch", "-D", branch, check=False)
            _git(integration, "worktree", "add", "-b", branch, str(path), "HEAD")
        _link_local_dependencies(self.store.job_workspace_path(job_id), path)
        return path, branch

    def _cleanup_task_worktree(self, integration: Path, path: Path, branch: str) -> None:
        with self._git_lock:
            _git(integration, "worktree", "remove", "--force", str(path), check=False)
            _git(integration, "branch", "-D", branch, check=False)

    def _commit_task(self, worktree: Path, contract: TaskContract) -> str | None:
        if not _git(worktree, "status", "--porcelain"):
            return None
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "-c",
            "user.name=Memory Hub Autopilot",
            "-c",
            "user.email=memoryhub@local",
            "commit",
            "-m",
            f"autopilot: {contract.title[:60]}",
        )
        return _git(worktree, "rev-parse", "HEAD")

    def _execute_task(
        self,
        *,
        job_id: str,
        task_row: dict[str, Any],
        contract: TaskContract,
        goal: GoalContract,
        route: Route,
        worktree: Path,
        branch: str,
    ) -> dict[str, Any]:
        attempt = int(task_row["attempt_count"]) + 1
        _emit(
            "task_started",
            job_id=job_id,
            task_id=contract.id,
            attempt=attempt,
            provider=route.provider,
            model=route.model,
            effort=route.effort,
        )
        owner = f"runner-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        if not self.store.claim_task(
            job_id, contract.id, owner, self.provider_timeout + self.validation_timeout + 120
        ):
            return {"status": "claim-failed", "task_id": contract.id}
        self.store.update_task(
            job_id,
            contract.id,
            status="running",
            provider=route.provider,
            model=route.model,
            effort=route.effort,
            worktree_path=str(worktree),
        )
        previous = json.loads(str(task_row.get("result_json") or "{}"))
        prompt = build_worker_prompt(
            goal,
            contract,
            memory_context=self.memory.render_context(
                cwd=self.store.job_workspace_path(job_id),
                task_id=str(self.store.get_job(job_id)["task_id"]),
            ),
            attempt=attempt,
            previous_result=previous,
        )
        run_id = self.store.create_run(
            job_id=job_id,
            task_id=contract.id,
            role="worker",
            route=route,
            pid=None,
        )
        result = run_json_agent(
            provider=route.provider,
            cwd=worktree,
            prompt=prompt,
            schema=WORKER_SCHEMA,
            model=route.model,
            effort=route.effort,
            writable=True,
            timeout_seconds=self.provider_timeout,
            on_start=lambda pid: self.store.update_run_pid(run_id, pid),
            **self._provider_args(),
        )
        changed = _changed_paths(worktree)
        violations = [path for path in changed if not _path_allowed(path, contract.allowed_paths)]
        evidence: list[dict[str, Any]] = []
        worker_done = result["status"] == "success" and result["structured"].get("status") == "done"
        runner_can_recover = (
            result["status"] == "success"
            and bool(changed)
            and is_provider_infrastructure_block(result["structured"])
        )
        if (worker_done or runner_can_recover) and not violations:
            self.store.update_task(job_id, contract.id, status="validating", result=result)
            evidence = run_validations(
                worktree, contract.validations, timeout=self.validation_timeout
            )
            changed = _changed_paths(worktree)
            violations = [
                path for path in changed if not _path_allowed(path, contract.allowed_paths)
            ]
        passed = (
            result["status"] == "success"
            and (worker_done or runner_can_recover)
            and not violations
            and validations_passed(evidence)
        )
        commit = self._commit_task(worktree, contract) if passed else None
        outcome = redact(
            {
                **result,
                "task_id": contract.id,
                "route": route.__dict__ if hasattr(route, "__dict__") else {
                    "provider": route.provider,
                    "model": route.model,
                    "effort": route.effort,
                    "profile": route.profile,
                    "reason": route.reason,
                },
                "changed_paths": changed,
                "scope_violations": violations,
                "validation_evidence": evidence,
                "recovered_by_runner_validation": bool(passed and runner_can_recover),
                "commit": commit,
                "branch": branch,
            }
        )
        final_status = "success" if passed else result["status"]
        if violations:
            final_status = "scope-violation"
        elif (
            not passed
            and result["status"] == "success"
            and result["structured"].get("status") == "blocked"
        ):
            final_status = (
                "provider-failed"
                if is_provider_infrastructure_block(result["structured"])
                else "blocked"
            )
        elif result["status"] == "success" and not validations_passed(evidence):
            final_status = "validation-failed"
        self.store.finish_run(
            run_id,
            status=final_status,
            result=outcome,
            error=str(outcome.get("error", "")),
        )
        outcome["status"] = final_status
        _emit(
            "task_finished",
            job_id=job_id,
            task_id=contract.id,
            attempt=attempt,
            status=final_status,
            changed_paths=changed,
            scope_violations=violations,
        )
        return outcome

    def _checkpoint_task(self, job_id: str, contract: TaskContract, outcome: dict[str, Any]) -> None:
        structured = outcome.get("structured") or {}
        success = outcome["status"] == "success"
        self.memory.checkpoint(
            actor=f"autopilot-{outcome.get('provider', 'worker')}",
            cwd=self.store.job_workspace_path(job_id),
            task_id=str(self.store.get_job(job_id)["task_id"]),
            status="in_progress",
            summary=f"Autopilot {contract.id} {'completed' if success else 'failed'}: "
            f"{structured.get('summary') or outcome.get('error') or outcome['status']}",
            next_action=(
                "Autopilot must integrate the validated task commit."
                if success
                else "Autopilot must route a fresh attempt or stop at the retry gate."
            ),
            items={
                "file": [str(item) for item in outcome.get("changed_paths", [])],
                "validation": [
                    f"{item.get('command')}: {item.get('status')}"
                    for item in outcome.get("validation_evidence", [])
                ],
                "blocker": [
                    *[str(item) for item in structured.get("blockers", [])],
                    *[f"scope violation: {item}" for item in outcome.get("scope_violations", [])],
                ],
            },
        )

    def _integrate(self, integration: Path, commit: str | None) -> None:
        if not commit:
            return
        with self._git_lock:
            try:
                _git(
                    integration,
                    "-c",
                    "user.name=Memory Hub Autopilot",
                    "-c",
                    "user.email=memoryhub@local",
                    "cherry-pick",
                    commit,
                )
            except AutopilotError:
                _git(integration, "cherry-pick", "--abort", check=False)
                raise

    def _task_rows(self, job_id: str) -> dict[str, dict[str, Any]]:
        return {str(row["id"]): row for row in self.store.tasks(job_id)}

    def _review(self, job_id: str, integration: Path, goal: GoalContract) -> dict[str, Any]:
        _emit("validation_started", job_id=job_id)
        tasks = [
            TaskContract.from_dict(json.loads(str(row["contract_json"])))
            for row in self.store.tasks(job_id)
        ]
        usage = self.store.usage()
        primary = "codex" if usage["codex"].status not in {"unavailable", "rate_limited"} else "claude"
        providers = [primary, "claude" if primary == "codex" else "codex"]
        all_commands = list(dict.fromkeys(command for task in tasks for command in task.validations))
        evidence = run_validations(integration, all_commands, timeout=self.validation_timeout)
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "status": "failed",
            "provider": primary,
            "error": "no eligible final reviewer",
        }
        for provider in providers:
            if usage[provider].status in {"unavailable", "rate_limited"}:
                continue
            lead_model, lead_effort = execution_settings(provider, "lead")
            route = Route(provider, lead_model, lead_effort, "lead", "final review")
            run_id = self.store.create_run(
                job_id=job_id, task_id=None, role="validator", route=route, pid=None
            )
            result = run_json_agent(
                provider=provider,
                cwd=integration,
                prompt=reviewer_prompt(goal, tasks)
                + "\nDETERMINISTIC VALIDATION EVIDENCE\n"
                + json.dumps(evidence, ensure_ascii=False, indent=2),
                schema=REVIEW_SCHEMA,
                model=route.model,
                effort=route.effort,
                writable=False,
                timeout_seconds=min(self.provider_timeout, 1800),
                on_start=lambda pid, current_run=run_id: self.store.update_run_pid(
                    current_run, pid
                ),
                **self._provider_args(),
            )
            attempts.append(result)
            infrastructure_failure = result["status"] != "success"
            self.store.finish_run(
                run_id,
                status="provider-failed" if infrastructure_failure else "success",
                result=result,
                error=str(result.get("error", "")),
            )
            if not infrastructure_failure:
                break
            if result["status"] == "rate_limited":
                self.store.save_usage(
                    ProviderUsage(provider, status="rate_limited", source="validator error")
                )
        passed = (
            validations_passed(evidence)
            and result["status"] == "success"
            and bool(result["structured"].get("passed"))
            and not result["structured"].get("required_fixes")
        )
        outcome = {
            **result,
            "review_attempts": attempts,
            "validation_evidence": evidence,
            "passed": passed,
        }
        _emit("validation_finished", job_id=job_id, passed=passed, provider=result.get("provider"))
        return outcome

    def _apply_to_source(self, job_id: str, source: Path, integration: Path, branch: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if _git(source, "status", "--porcelain"):
            return {"applied": False, "reason": "source worktree became dirty", "branch": branch}
        if _git(source, "rev-parse", "HEAD") != str(job["base_ref"]):
            return {"applied": False, "reason": "source branch moved", "branch": branch}
        try:
            _git(source, "merge", "--ff-only", branch)
        except AutopilotError as error:
            return {"applied": False, "reason": str(error), "branch": branch}
        with self._git_lock:
            _git(source, "worktree", "remove", "--force", str(integration), check=False)
            _git(source, "branch", "-D", branch, check=False)
        return {"applied": True, "branch": str(job["base_branch"]), "commit": _git(source, "rev-parse", "HEAD")}

    def run_job(self, job_id: str, cwd: Path) -> dict[str, Any]:
        requested_cwd = cwd.expanduser().resolve()
        job = self.store.get_job(job_id)
        cwd = self.store.job_workspace_path(job_id)
        if requested_cwd != cwd:
            _emit(
                "source_path_corrected",
                job_id=job_id,
                requested=str(requested_cwd),
                source=str(cwd),
            )
        old_pid = int(job["runner_pid"]) if job["runner_pid"] else None
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            raise AutopilotError(f"job already has live runner pid {old_pid}")
        orphan_count = self.reap_orphan_provider_runs(job_id)
        if orphan_count or (old_pid and old_pid != os.getpid() and not _pid_alive(old_pid)):
            self.store.recover_running_tasks(job_id, reason=f"runner {old_pid} disappeared")
        self.store.update_job(job_id, status=str(job["status"]), runner_pid=os.getpid())
        integration, branch = self.ensure_integration_worktree(job_id, cwd)
        if not self.store.tasks(job_id):
            goal, tasks, planning_result = self.plan(job_id, integration)
            with self.memory.connect() as db:
                db.execute(
                    "UPDATE autopilot_jobs SET goal_json=?, updated_at=? WHERE id=?",
                    (json.dumps(goal.to_dict(), ensure_ascii=False, separators=(",", ":")), utc_now(), job_id),
                )
            self.store.save_plan(job_id, tasks)
            self.memory.checkpoint(
                actor="autopilot-orchestrator",
                cwd=self.store.job_workspace_path(job_id),
                task_id=str(job["task_id"]),
                status="in_progress",
                summary=f"Autopilot plan ready with {len(tasks)} task(s).",
                next_action="Autopilot must execute the first ready task contract.",
                items={
                    "decision": [f"{task.id}: {task.title}" for task in tasks],
                    "validation": goal.done_when,
                    "blocker": [str(planning_result.get("error"))]
                    if planning_result.get("error")
                    else [],
                },
            )
        goal = GoalContract.from_dict(json.loads(str(self.store.get_job(job_id)["goal_json"])))
        while True:
            self.store.update_job(job_id, status="running", runner_pid=os.getpid())
            rows = self._task_rows(job_id)
            if rows and all(row["status"] == "completed" for row in rows.values()):
                break
            ready = self.store.ready_contracts(job_id)
            if not ready:
                failed = [row for row in rows.values() if row["status"] in {"failed", "blocked"}]
                if failed:
                    self.store.update_job(job_id, status="blocked", error="no runnable task remains")
                    _emit("job_blocked", job_id=job_id, reason="no runnable task remains")
                    return self.store.status(job_id)
                raise AutopilotError("job has no ready task and is not complete")
            job = self.store.get_job(job_id)
            batch = parallel_batch(ready, int(job["max_workers"]))
            _emit("batch_started", job_id=job_id, task_ids=[item.id for item in batch])
            usage = self.refresh_usage()
            prepared: list[tuple[TaskContract, dict[str, Any], Route, Path, str]] = []
            assigned: list[str] = []
            for contract in batch:
                row = rows[contract.id]
                if int(row["attempt_count"]) >= int(job["max_attempts"]):
                    self.store.update_task(
                        job_id,
                        contract.id,
                        status="blocked",
                        result={"status": "retry-limit", "reason": "attempt limit reached"},
                    )
                    _emit(
                        "task_retry_exhausted",
                        job_id=job_id,
                        task_id=contract.id,
                        attempts=int(row["attempt_count"]),
                    )
                    continue
                previous_provider = str(row.get("provider") or "") or None
                preferred = str(job["lead_provider"])
                if preferred == "auto" and assigned:
                    preferred = "claude" if assigned[0] == "codex" else "codex"
                route = route_task(
                    contract,
                    usage,
                    preferred=preferred,
                    previous_provider=previous_provider,
                    performance=self.store.performance(contract.profile),
                )
                assigned.append(route.provider)
                worktree, task_branch = self._prepare_task_worktree(
                    job_id, integration, contract, int(row["attempt_count"]) + 1
                )
                prepared.append((contract, row, route, worktree, task_branch))
            if not prepared:
                self.store.update_job(job_id, status="blocked", error="task retry gate exhausted")
                _emit("job_blocked", job_id=job_id, reason="task retry gate exhausted")
                return self.store.status(job_id)
            outcomes: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
                futures = {
                    pool.submit(
                        self._execute_task,
                        job_id=job_id,
                        task_row=row,
                        contract=contract,
                        goal=goal,
                        route=route,
                        worktree=worktree,
                        branch=task_branch,
                    ): (contract, worktree, task_branch)
                    for contract, row, route, worktree, task_branch in prepared
                }
                for future in as_completed(futures):
                    contract, worktree, task_branch = futures[future]
                    try:
                        outcomes[contract.id] = future.result()
                    except Exception as error:
                        outcomes[contract.id] = {
                            "status": "runner-error",
                            "task_id": contract.id,
                            "error": redact_text(str(error)),
                            "branch": task_branch,
                        }
            for contract, row, route, worktree, task_branch in prepared:
                outcome = outcomes[contract.id]
                self._checkpoint_task(job_id, contract, outcome)
                if outcome["status"] == "success":
                    try:
                        self._integrate(integration, outcome.get("commit"))
                    except AutopilotError as error:
                        outcome = {**outcome, "status": "integration-failed", "error": str(error)}
                self._cleanup_task_worktree(integration, worktree, task_branch)
                if outcome["status"] == "success":
                    self.store.update_task(job_id, contract.id, status="completed", result=outcome)
                    continue
                attempt_count = int(row["attempt_count"]) + 1
                if outcome["status"] == "rate_limited":
                    self.store.save_usage(
                        ProviderUsage(route.provider, status="rate_limited", source="worker error")
                    )
                if attempt_count < int(job["max_attempts"]) and outcome["status"] not in {"blocked"}:
                    self.store.update_task(job_id, contract.id, status="ready", result=outcome)
                    _emit(
                        "task_retry_scheduled",
                        job_id=job_id,
                        task_id=contract.id,
                        next_attempt=attempt_count + 1,
                    )
                else:
                    self.store.update_task(job_id, contract.id, status="blocked", result=outcome)
            rows = self._task_rows(job_id)
            if any(row["status"] == "blocked" for row in rows.values()):
                self.store.update_job(job_id, status="blocked", error="task retry gate exhausted")
                _emit("job_blocked", job_id=job_id, reason="task retry gate exhausted")
                return self.store.status(job_id)
        self.store.update_job(job_id, status="validating", runner_pid=os.getpid())
        review = self._review(job_id, integration, goal)
        if not review["passed"]:
            self.store.update_job(job_id, status="blocked", error="final validation gate failed")
            _emit("job_blocked", job_id=job_id, reason="final validation gate failed")
            return self.store.status(job_id)
        applied = self._apply_to_source(job_id, cwd, integration, branch)
        self.store.update_job(job_id, status="completed", error="")
        self.memory.checkpoint(
            actor="autopilot-validator",
            cwd=cwd,
            task_id=str(job["task_id"]),
            status="completed",
            summary="Autopilot completed the goal and passed the independent validation gate.",
            next_action=(
                "Review the completed Autopilot result and start a new goal when needed."
                if applied["applied"]
                else f"Review and integrate branch {applied['branch']}: {applied['reason']}"
            ),
            items={
                "validation": [
                    f"{item.get('command')}: {item.get('status')}"
                    for item in review.get("validation_evidence", [])
                ],
                "decision": [json.dumps(applied, ensure_ascii=False)],
            },
        )
        result = self.store.status(job_id)
        result["apply_result"] = applied
        _emit("job_completed", job_id=job_id, apply_result=applied)
        return result


def supervise_job(
    *,
    job_id: str,
    cwd: Path,
    max_restarts: int = 3,
    runner_factory: type[AutopilotRunner] = AutopilotRunner,
) -> dict[str, Any]:
    last_error = ""
    for attempt in range(1, max_restarts + 1):
        try:
            return runner_factory().run_job(job_id, cwd)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            last_error = redact_text(str(error))
            _emit("runner_restart", job_id=job_id, attempt=attempt, error=last_error)
            store = AutopilotStore()
            store.update_job(job_id, status="paused", error=f"runner restart {attempt}: {last_error}")
            store.recover_running_tasks(job_id, reason=last_error)
            if attempt < max_restarts:
                time.sleep(min(attempt, 3))
    store = AutopilotStore()
    store.update_job(job_id, status="failed", error=last_error)
    _emit("job_failed", job_id=job_id, error=last_error)
    return store.status(job_id)
