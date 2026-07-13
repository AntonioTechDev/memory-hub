from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .core import MemoryStore, redact, redact_text, utc_now, workspace_identity

JOB_STATES = {
    "draft",
    "planning",
    "running",
    "validating",
    "paused",
    "blocked",
    "failed",
    "completed",
    "cancelled",
}
TASK_STATES = {
    "pending",
    "ready",
    "running",
    "validating",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}
TERMINAL_JOB_STATES = {"blocked", "failed", "completed", "cancelled"}
TERMINAL_TASK_STATES = {"completed", "blocked", "failed", "cancelled"}
USAGE_STATES = {"available", "constrained", "rate_limited", "unavailable", "unknown"}
PROFILES = {"fast", "builder", "senior", "lead"}
COMPLEXITIES = {"xs", "s", "m", "l", "xl"}


@dataclass(slots=True)
class GoalContract:
    objective: str
    done_when: list[str]
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    complexity: str = "s"
    risk: str = "medium"

    def validate(self) -> None:
        if not self.objective.strip():
            raise ValueError("goal objective cannot be empty")
        if not self.done_when:
            raise ValueError("goal must have at least one completion criterion")
        if self.complexity not in COMPLEXITIES:
            raise ValueError(f"invalid goal complexity: {self.complexity}")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"invalid goal risk: {self.risk}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return redact(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> GoalContract:
        result = cls(
            objective=str(value.get("objective", "")),
            done_when=_strings(value.get("done_when")),
            constraints=_strings(value.get("constraints")),
            non_goals=_strings(value.get("non_goals")),
            complexity=str(value.get("complexity", "s")),
            risk=str(value.get("risk", "medium")),
        )
        result.validate()
        return result


@dataclass(slots=True)
class TaskContract:
    id: str
    title: str
    objective: str
    acceptance_criteria: list[str]
    validations: list[str]
    allowed_paths: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    profile: str = "builder"
    risk: str = "medium"
    parallel_safe: bool = False

    def validate(self) -> None:
        if not re.fullmatch(r"t[1-9][0-9]*", self.id):
            raise ValueError(f"invalid task id: {self.id}")
        if not self.title.strip() or not self.objective.strip():
            raise ValueError(f"task {self.id} requires title and objective")
        if not self.acceptance_criteria:
            raise ValueError(f"task {self.id} requires acceptance criteria")
        if not self.validations:
            raise ValueError(f"task {self.id} requires validation evidence")
        if self.profile not in PROFILES:
            raise ValueError(f"invalid task profile: {self.profile}")
        if self.id in self.depends_on:
            raise ValueError(f"task {self.id} cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return redact(asdict(self))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskContract:
        task_id = str(value.get("id", "")).lower()
        result = cls(
            id=task_id,
            title=str(value.get("title", "")),
            objective=str(value.get("objective", "")),
            acceptance_criteria=_strings(value.get("acceptance_criteria")),
            validations=_strings(value.get("validations")),
            allowed_paths=_strings(value.get("allowed_paths")),
            constraints=_strings(value.get("constraints")),
            depends_on=[item.lower() for item in _strings(value.get("depends_on"))],
            profile=str(value.get("profile", "builder")),
            risk=str(value.get("risk", "medium")),
            parallel_safe=bool(value.get("parallel_safe", False)),
        )
        result.validate()
        return result


@dataclass(slots=True)
class ProviderUsage:
    provider: str
    status: str = "unknown"
    remaining: dict[str, Any] = field(default_factory=dict)
    reset_at: str | None = None
    source: str = ""
    raw_excerpt: str = ""
    observed_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        if self.provider not in {"codex", "claude"}:
            raise ValueError(f"unsupported provider: {self.provider}")
        if self.status not in USAGE_STATES:
            raise ValueError(f"invalid usage status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return redact(asdict(self))


@dataclass(slots=True)
class Route:
    provider: str
    model: str
    effort: str
    profile: str
    reason: str


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [redact_text(str(item).strip()) for item in value if str(item).strip()]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def classify_goal(objective: str) -> tuple[str, str]:
    text = objective.lower()
    critical = ("production", "pagament", "billing", "auth", "sicurezza", "security")
    large = ("refactor", "migraz", "intero", "end-to-end", "architett", "multi")
    trivial = ("colore", "typo", "testo", "rinomina", "rename", "css", "copy")
    if any(token in text for token in critical):
        return ("l" if any(token in text for token in large) else "m", "high")
    if any(token in text for token in large) or len(objective) > 600:
        return ("l", "high")
    if any(token in text for token in trivial) and len(objective) < 240:
        return ("xs", "low")
    if len(objective) < 180:
        return ("s", "medium")
    return ("m", "medium")


def discover_validations(cwd: Path, *, complexity: str, risk: str) -> list[str]:
    commands: list[str] = []
    package = cwd / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        for name in ("test", "typecheck", "lint", "build"):
            if name in scripts:
                commands.append(f"npm run {name}")
                if complexity in {"xs", "s"} and name == "test":
                    break
    if (cwd / "pyproject.toml").is_file() or (cwd / "tests").is_dir():
        if (cwd / "tests").is_dir():
            commands.append("python3 -m unittest discover -s tests -v")
        else:
            commands.append("python3 -m compileall -q .")
    if (cwd / "Cargo.toml").is_file():
        commands.append("cargo test")
    if (cwd / "go.mod").is_file():
        commands.append("go test ./...")
    if not commands:
        commands.append("Inspect the focused diff and run the repository's nearest relevant check")
    if risk in {"high", "critical"}:
        commands.append("Perform an independent regression and security-focused review")
    return list(dict.fromkeys(commands))


def default_goal(objective: str, cwd: Path) -> GoalContract:
    complexity, risk = classify_goal(objective)
    validations = discover_validations(cwd, complexity=complexity, risk=risk)
    return GoalContract(
        objective=redact_text(objective.strip()),
        done_when=[
            "The requested observable behavior is implemented",
            "Required validation completes successfully",
            "The final diff stays within the approved scope",
        ],
        constraints=[
            "Preserve existing user changes and public behavior outside the goal",
            "Do not deploy, push, buy API credits, or perform irreversible remote actions",
            f"Use validation evidence appropriate to the repository: {', '.join(validations)}",
        ],
        non_goals=["Speculative refactors or abstractions unrelated to the goal"],
        complexity=complexity,
        risk=risk,
    )


def default_tasks(goal: GoalContract, cwd: Path) -> list[TaskContract]:
    validations = discover_validations(cwd, complexity=goal.complexity, risk=goal.risk)
    profile = {
        "xs": "fast",
        "s": "builder",
        "m": "senior",
        "l": "senior",
        "xl": "lead",
    }[goal.complexity]
    return [
        TaskContract(
            id="t1",
            title=goal.objective.splitlines()[0][:100],
            objective=goal.objective,
            acceptance_criteria=list(goal.done_when),
            validations=validations,
            constraints=[*goal.constraints, *goal.non_goals],
            profile=profile,
            risk=goal.risk,
            parallel_safe=False,
        )
    ]


def validate_plan(goal: GoalContract, tasks: list[TaskContract]) -> None:
    goal.validate()
    if not tasks:
        raise ValueError("plan must contain at least one task")
    maximum = {"xs": 1, "s": 2, "m": 5, "l": 8, "xl": 12}[goal.complexity]
    if len(tasks) > maximum:
        raise ValueError(
            f"plan over-engineers a {goal.complexity} goal: {len(tasks)} tasks exceeds {maximum}"
        )
    ids = {task.id for task in tasks}
    if len(ids) != len(tasks):
        raise ValueError("plan contains duplicate task ids")
    for task in tasks:
        task.validate()
        unknown = set(task.depends_on) - ids
        if unknown:
            raise ValueError(f"task {task.id} has unknown dependencies: {sorted(unknown)}")
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {task.id: task for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("plan contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in ids:
        visit(task_id)


def paths_overlap(left: TaskContract, right: TaskContract) -> bool:
    if not left.allowed_paths or not right.allowed_paths:
        return True
    for one in left.allowed_paths:
        clean_one = one.strip().strip("./")
        for two in right.allowed_paths:
            clean_two = two.strip().strip("./")
            if clean_one == clean_two:
                return True
            if clean_one.startswith(f"{clean_two}/") or clean_two.startswith(f"{clean_one}/"):
                return True
    return False


def parallel_batch(tasks: Iterable[TaskContract], limit: int = 2) -> list[TaskContract]:
    selected: list[TaskContract] = []
    for task in tasks:
        if len(selected) >= max(1, min(2, limit)):
            break
        if selected and (
            not task.parallel_safe
            or any(not item.parallel_safe or paths_overlap(item, task) for item in selected)
        ):
            continue
        selected.append(task)
    return selected


MODEL_DEFAULTS: dict[str, dict[str, tuple[str, str]]] = {
    "codex": {
        "fast": ("", "low"),
        "builder": ("", "medium"),
        "senior": ("", "high"),
        "lead": ("", "xhigh"),
    },
    "claude": {
        "fast": ("sonnet", "low"),
        "builder": ("sonnet", "medium"),
        "senior": ("opus", "high"),
        "lead": ("opus", "high"),
    },
}


def execution_settings(provider: str, profile: str) -> tuple[str, str]:
    if provider not in MODEL_DEFAULTS or profile not in PROFILES:
        raise ValueError(f"unsupported execution profile: {provider}/{profile}")
    default_model, default_effort = MODEL_DEFAULTS[provider][profile]
    prefix = f"MEMORYHUB_{provider.upper()}_{profile.upper()}"
    return (
        os.environ.get(f"{prefix}_MODEL", default_model),
        os.environ.get(f"{prefix}_EFFORT", default_effort),
    )


def route_task(
    contract: TaskContract,
    usage: dict[str, ProviderUsage],
    *,
    preferred: str = "auto",
    previous_provider: str | None = None,
    performance: dict[str, dict[str, Any]] | None = None,
) -> Route:
    candidates = ["codex", "claude"]
    if preferred in candidates:
        candidates.remove(preferred)
        candidates.insert(0, preferred)
    available = [
        provider
        for provider in candidates
        if usage.get(provider, ProviderUsage(provider)).status
        not in {"rate_limited", "unavailable"}
    ]
    if not available:
        raise ValueError("no provider is currently available")
    if preferred == "auto" and performance:
        proven = [
            provider
            for provider in available
            if int(performance.get(provider, {}).get("sample", 0)) >= 5
        ]
        if proven:
            best = max(
                proven,
                key=lambda provider: float(
                    performance.get(provider, {}).get("success_rate", 0.0)
                ),
            )
            available.remove(best)
            available.insert(0, best)
    if previous_provider in available and len(available) > 1:
        available.remove(str(previous_provider))
        available.append(str(previous_provider))
    provider = available[0]
    model, effort = execution_settings(provider, contract.profile)
    status = usage.get(provider, ProviderUsage(provider)).status
    reason = f"{contract.profile} profile; {provider} usage={status}"
    if previous_provider and provider != previous_provider:
        reason += f"; fallback from {previous_provider}"
    return Route(provider, model, effort, contract.profile, reason)


def parse_usage(provider: str, raw: str, *, returncode: int = 0) -> ProviderUsage:
    clean = redact_text(raw, limit=4000)
    lower = clean.lower()
    status = "unknown"
    if returncode != 0 or any(token in lower for token in ("not logged in", "unauthorized", "auth failed")):
        status = "unavailable"
    elif any(token in lower for token in ("rate limit", "usage limit", "limit reached", "quota exceeded")):
        status = "rate_limited" if any(
            token in lower for token in ("reached", "exceeded", "0%", "no usage")
        ) else "constrained"
    elif clean.strip() and any(
        token in lower for token in ("usage", "remaining", "limit", "reset", "%")
    ):
        status = "available"
    current_usage = re.findall(r"(?im)^Current [^:\n]+:\s*(\d{1,3})\s*%\s*used", clean)
    percentages = [int(value) for value in current_usage] or [
        int(value) for value in re.findall(r"(?<!\d)(\d{1,3})\s*%", clean)
    ]
    remaining: dict[str, Any] = {}
    if percentages:
        remaining["used_percentages"] = percentages[:10]
        remaining["remaining_percentages"] = [max(0, 100 - value) for value in percentages[:10]]
    reset_match = re.search(
        r"(?i)(?:\bresets?(?:\s+(?:at|in))?|ripristin\w*)\s*[:\-]?\s*([^\n;]{1,80})",
        clean,
    )
    reset_at = reset_match.group(1).strip() if reset_match else None
    if reset_at and ". " in reset_at:
        reset_at = reset_at.split(". ", 1)[0]
    return ProviderUsage(
        provider=provider,
        status=status,
        remaining=remaining,
        reset_at=reset_at,
        source=f"{provider} /usage",
        raw_excerpt=clean,
    )


def build_worker_prompt(
    goal: GoalContract,
    task: TaskContract,
    *,
    memory_context: str,
    attempt: int,
    previous_result: dict[str, Any] | None = None,
) -> str:
    contract = task.to_dict()
    return f"""You are one bounded implementation worker inside Memory Hub Autopilot.

Implement only this task. Inspect current files and Git before editing. Preserve pre-existing
work. Do not commit, push, pull, checkout, reset, stash, rebase, deploy, buy credits, spawn
background agents, or wait for user input. Prefer the smallest change that satisfies the
acceptance criteria; do not add speculative abstractions or unrelated cleanup.

GOAL CONTRACT
{json.dumps(goal.to_dict(), ensure_ascii=False, indent=2)}

TASK CONTRACT — attempt {attempt}
{json.dumps(contract, ensure_ascii=False, indent=2)}

PREVIOUS ATTEMPT EVIDENCE
{json.dumps(redact(previous_result or {}), ensure_ascii=False, indent=2)}

SHARED OPERATIONAL MEMORY (index only; verify against Git, files and tests)
{memory_context}

Run every applicable deterministic validation from the contract. If a command is descriptive
rather than executable, perform the nearest repository-native proof and report it exactly.
Return schema-valid JSON. A self-report is evidence for the runner, never final acceptance.
"""


class AutopilotStore:
    def __init__(self, memory: MemoryStore | None = None) -> None:
        self.memory = memory or MemoryStore()

    def initialize(self) -> None:
        self.memory.initialize()

    def create_job(
        self,
        *,
        cwd: Path,
        goal: GoalContract,
        max_workers: int = 1,
        max_attempts: int = 2,
        lead_provider: str = "auto",
    ) -> str:
        self.initialize()
        goal.validate()
        if max_workers not in {1, 2}:
            raise ValueError("max_workers must be 1 or 2")
        if max_attempts not in {1, 2, 3}:
            raise ValueError("max_attempts must be between 1 and 3")
        if lead_provider not in {"auto", "codex", "claude"}:
            raise ValueError("lead_provider must be auto, codex or claude")
        workspace = self.memory.ensure_workspace(cwd)
        task_id = self.memory.checkpoint(
            actor="autopilot",
            cwd=cwd,
            objective=goal.objective,
            status="in_progress",
            summary="Autopilot job created; planning pending.",
            next_action="Autopilot must create and validate the goal plan.",
            items={"validation": goal.done_when, "decision": goal.constraints},
        )
        job_id = f"ap_{uuid.uuid4().hex[:16]}"
        timestamp = utc_now()
        base_ref = _git(cwd, "rev-parse", "HEAD")
        base_branch = _git(cwd, "branch", "--show-current")
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO autopilot_jobs(
                    id, task_id, workspace_id, objective, goal_json, status,
                    max_workers, max_attempts, lead_provider, base_ref, base_branch,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'planning', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    task_id,
                    workspace["id"],
                    redact_text(goal.objective),
                    json.dumps(goal.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    max_workers,
                    max_attempts,
                    lead_provider,
                    base_ref,
                    base_branch,
                    timestamp,
                    timestamp,
                ),
            )
        return job_id

    def save_plan(self, job_id: str, tasks: list[TaskContract]) -> None:
        job = self.get_job(job_id)
        goal = GoalContract.from_dict(json.loads(str(job["goal_json"])))
        validate_plan(goal, tasks)
        timestamp = utc_now()
        with self.memory.connect() as db:
            existing = db.execute(
                "SELECT COUNT(*) FROM autopilot_tasks WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            if existing:
                raise ValueError("job plan is immutable after tasks are created")
            for ordinal, task in enumerate(tasks, start=1):
                db.execute(
                    """
                    INSERT INTO autopilot_tasks(
                        id, job_id, ordinal, title, objective, status, contract_json,
                        depends_on_json, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        task.id,
                        job_id,
                        ordinal,
                        redact_text(task.title),
                        redact_text(task.objective),
                        json.dumps(task.to_dict(), ensure_ascii=False, separators=(",", ":")),
                        json.dumps(task.depends_on, ensure_ascii=False, separators=(",", ":")),
                        timestamp,
                        timestamp,
                    ),
                )
            db.execute(
                """
                UPDATE autopilot_jobs
                SET status='running', revision=revision+1, updated_at=? WHERE id=?
                """,
                (timestamp, job_id),
            )
        self.refresh_ready(job_id)

    def get_job(self, job_id: str) -> sqlite3.Row:
        self.initialize()
        with self.memory.connect() as db:
            row = db.execute("SELECT * FROM autopilot_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown autopilot job: {job_id}")
        return row

    def job_workspace_path(self, job_id: str) -> Path:
        job = self.get_job(job_id)
        with self.memory.connect() as db:
            row = db.execute(
                "SELECT path FROM workspaces WHERE id=?", (job["workspace_id"],)
            ).fetchone()
        if row is None:
            raise ValueError(f"workspace missing for job {job_id}")
        return Path(str(row["path"])).expanduser().resolve()

    def list_jobs(self, *, cwd: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT j.*, w.name workspace_name FROM autopilot_jobs j JOIN workspaces w ON w.id=j.workspace_id"
        params: list[Any] = []
        if cwd is not None:
            query += " WHERE j.workspace_id=?"
            params.append(workspace_identity(cwd)["id"])
        query += " ORDER BY j.updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self.memory.connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def tasks(self, job_id: str) -> list[dict[str, Any]]:
        self.get_job(job_id)
        with self.memory.connect() as db:
            rows = db.execute(
                "SELECT * FROM autopilot_tasks WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def refresh_ready(self, job_id: str) -> None:
        rows = self.tasks(job_id)
        completed = {row["id"] for row in rows if row["status"] == "completed"}
        timestamp = utc_now()
        with self.memory.connect() as db:
            for row in rows:
                if row["status"] != "pending":
                    continue
                dependencies = set(json.loads(str(row["depends_on_json"])))
                if dependencies <= completed:
                    db.execute(
                        "UPDATE autopilot_tasks SET status='ready', updated_at=? WHERE job_id=? AND id=?",
                        (timestamp, job_id, row["id"]),
                    )

    def ready_contracts(self, job_id: str) -> list[TaskContract]:
        self.refresh_ready(job_id)
        return [
            TaskContract.from_dict(json.loads(str(row["contract_json"])))
            for row in self.tasks(job_id)
            if row["status"] == "ready"
        ]

    def claim_task(self, job_id: str, task_id: str, owner: str, ttl_seconds: int) -> bool:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="milliseconds"
        )
        timestamp = utc_now()
        with self.memory.connect() as db:
            cursor = db.execute(
                """
                UPDATE autopilot_tasks
                SET status='running', lease_owner=?, lease_expires_at=?,
                    attempt_count=attempt_count+1, updated_at=?
                WHERE job_id=? AND id=? AND status='ready'
                  AND (lease_expires_at IS NULL OR lease_expires_at<?)
                """,
                (owner, expires, timestamp, job_id, task_id, timestamp),
            )
            return cursor.rowcount == 1

    def release_expired_leases(self, job_id: str) -> int:
        timestamp = utc_now()
        with self.memory.connect() as db:
            cursor = db.execute(
                """
                UPDATE autopilot_tasks
                SET status='ready', lease_owner='', lease_expires_at=NULL, updated_at=?
                WHERE job_id=? AND status='running' AND lease_expires_at<?
                """,
                (timestamp, job_id, timestamp),
            )
            return cursor.rowcount

    def recover_running_tasks(self, job_id: str, *, reason: str) -> int:
        timestamp = utc_now()
        with self.memory.connect() as db:
            cursor = db.execute(
                """
                UPDATE autopilot_tasks
                SET status='ready', lease_owner='', lease_expires_at=NULL,
                    result_json=?, updated_at=?
                WHERE job_id=? AND status IN ('running', 'validating')
                """,
                (
                    json.dumps({"recovered": redact_text(reason)}, separators=(",", ":")),
                    timestamp,
                    job_id,
                ),
            )
            return cursor.rowcount

    def update_task(
        self,
        job_id: str,
        task_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        if status not in TASK_STATES:
            raise ValueError(f"invalid task status: {status}")
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, utc_now()]
        if status not in {"running", "validating"}:
            assignments.extend(["lease_owner=''", "lease_expires_at=NULL"])
        for name, value in {
            "provider": provider,
            "model": model,
            "effort": effort,
            "worktree_path": worktree_path,
        }.items():
            if value is not None:
                assignments.append(f"{name}=?")
                values.append(redact_text(value))
        if result is not None:
            assignments.append("result_json=?")
            values.append(json.dumps(redact(result), ensure_ascii=False, separators=(",", ":")))
        values.extend([job_id, task_id])
        with self.memory.connect() as db:
            cursor = db.execute(
                f"UPDATE autopilot_tasks SET {', '.join(assignments)} WHERE job_id=? AND id=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown task {task_id} for job {job_id}")
        self.refresh_ready(job_id)

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        runner_pid: int | None = None,
        error: str | None = None,
        integration_branch: str | None = None,
        integration_path: str | None = None,
    ) -> None:
        if status is not None and status not in JOB_STATES:
            raise ValueError(f"invalid job status: {status}")
        assignments = ["updated_at=?", "revision=revision+1", "runner_heartbeat_at=?"]
        timestamp = utc_now()
        values: list[Any] = [timestamp, timestamp]
        for name, value in {
            "status": status,
            "runner_pid": runner_pid,
            "last_error": redact_text(error or "") if error is not None else None,
            "integration_branch": integration_branch,
            "integration_path": integration_path,
        }.items():
            if value is not None:
                assignments.append(f"{name}=?")
                values.append(value)
        values.append(job_id)
        with self.memory.connect() as db:
            cursor = db.execute(
                f"UPDATE autopilot_jobs SET {', '.join(assignments)} WHERE id=?", values
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown autopilot job: {job_id}")

    def create_run(
        self,
        *,
        job_id: str,
        task_id: str | None,
        role: str,
        route: Route,
        pid: int | None = None,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        timestamp = utc_now()
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO autopilot_runs(
                    id, job_id, autopilot_task_id, role, provider, model, effort,
                    status, pid, started_at, heartbeat_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    task_id,
                    role,
                    route.provider,
                    route.model,
                    route.effort,
                    pid,
                    timestamp,
                    timestamp,
                ),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        timestamp = utc_now()
        with self.memory.connect() as db:
            cursor = db.execute(
                """
                UPDATE autopilot_runs
                SET status=?, result_json=?, error=?, heartbeat_at=?, ended_at=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    json.dumps(redact(result or {}), ensure_ascii=False, separators=(",", ":")),
                    redact_text(error),
                    timestamp,
                    timestamp,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"run is missing or already finished: {run_id}")

    def update_run_pid(self, run_id: str, pid: int) -> None:
        with self.memory.connect() as db:
            cursor = db.execute(
                "UPDATE autopilot_runs SET pid=?, heartbeat_at=? WHERE id=? AND status='running'",
                (pid, utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"run is missing or already finished: {run_id}")

    def active_runs(self, job_id: str) -> list[dict[str, Any]]:
        self.get_job(job_id)
        with self.memory.connect() as db:
            rows = db.execute(
                "SELECT * FROM autopilot_runs WHERE job_id=? AND status='running'",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_usage(self, snapshot: ProviderUsage) -> None:
        self.initialize()
        snapshot.validate()
        with self.memory.connect() as db:
            db.execute(
                """
                INSERT INTO provider_usage(
                    provider, status, remaining_json, reset_at, source, raw_excerpt, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    status=excluded.status,
                    remaining_json=excluded.remaining_json,
                    reset_at=excluded.reset_at,
                    source=excluded.source,
                    raw_excerpt=excluded.raw_excerpt,
                    observed_at=excluded.observed_at
                """,
                (
                    snapshot.provider,
                    snapshot.status,
                    json.dumps(redact(snapshot.remaining), ensure_ascii=False, separators=(",", ":")),
                    snapshot.reset_at,
                    redact_text(snapshot.source),
                    redact_text(snapshot.raw_excerpt, limit=4000),
                    snapshot.observed_at,
                ),
            )

    def usage(self) -> dict[str, ProviderUsage]:
        self.initialize()
        with self.memory.connect() as db:
            rows = db.execute("SELECT * FROM provider_usage").fetchall()
        result: dict[str, ProviderUsage] = {}
        for row in rows:
            result[str(row["provider"])] = ProviderUsage(
                provider=str(row["provider"]),
                status=str(row["status"]),
                remaining=json.loads(str(row["remaining_json"])),
                reset_at=row["reset_at"],
                source=str(row["source"]),
                raw_excerpt=str(row["raw_excerpt"]),
                observed_at=str(row["observed_at"]),
            )
        for provider in ("codex", "claude"):
            result.setdefault(provider, ProviderUsage(provider))
        return result

    def performance(self, profile: str) -> dict[str, dict[str, Any]]:
        if profile not in PROFILES:
            raise ValueError(f"invalid profile: {profile}")
        self.initialize()
        with self.memory.connect() as db:
            rows = db.execute(
                """
                SELECT r.provider, r.status, t.contract_json
                FROM autopilot_runs r
                JOIN autopilot_tasks t
                  ON t.job_id=r.job_id AND t.id=r.autopilot_task_id
                WHERE r.role='worker'
                ORDER BY r.started_at DESC LIMIT 200
                """
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            try:
                task_profile = json.loads(str(row["contract_json"])).get("profile")
            except json.JSONDecodeError:
                continue
            if task_profile != profile:
                continue
            provider = str(row["provider"])
            entry = counts.setdefault(provider, {"sample": 0, "success": 0})
            entry["sample"] += 1
            entry["success"] += int(str(row["status"]) == "success")
        return {
            provider: {
                **entry,
                "success_rate": entry["success"] / entry["sample"] if entry["sample"] else 0.0,
            }
            for provider, entry in counts.items()
        }

    def status(self, job_id: str) -> dict[str, Any]:
        job = dict(self.get_job(job_id))
        job["goal"] = json.loads(str(job.pop("goal_json")))
        tasks = self.tasks(job_id)
        for task in tasks:
            task["contract"] = json.loads(str(task.pop("contract_json")))
            task["depends_on"] = json.loads(str(task.pop("depends_on_json")))
            task["result"] = json.loads(str(task.pop("result_json")))
        job["tasks"] = tasks
        job["usage"] = {key: value.to_dict() for key, value in self.usage().items()}
        return redact(job)
