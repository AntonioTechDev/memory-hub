from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .core import MemoryStore, iso_before, parse_duration_seconds, redact, today_start


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"payload": value}


def store_from_args(args: argparse.Namespace) -> MemoryStore:
    return MemoryStore(Path(args.db).expanduser() if getattr(args, "db", None) else None)


def cmd_init(args: argparse.Namespace) -> int:
    store = store_from_args(args)
    store.initialize()
    print(f"Memory Hub {__version__} initialized at {store.db_path}")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    if os.environ.get("MEMORYHUB_SUPPRESS_HOOKS") == "1":
        return 0
    payload = read_payload()
    context = store_from_args(args).capture_hook(args.event, args.actor, payload)
    if args.event == "session-start":
        _recover_autopilot_for_workspace(
            Path(str(payload.get("cwd") or payload.get("working_directory") or Path.cwd()))
        )
    if args.event in {"session-start", "post-compact"}:
        print(context, end="")
    return 0


def _recover_autopilot_for_workspace(cwd: Path) -> None:
    try:
        store = autopilot_store_from_args(argparse.Namespace(db=None))
        jobs = store.list_jobs(cwd=cwd.expanduser().resolve(), limit=20)
        for job in jobs:
            if job["status"] not in {"planning", "running", "validating"}:
                continue
            pid = int(job["runner_pid"]) if job["runner_pid"] else None
            if pid:
                try:
                    os.kill(pid, 0)
                    continue
                except ProcessLookupError:
                    pass
            new_pid, _ = _launch_autopilot(str(job["id"]), cwd.expanduser().resolve())
            store.update_job(
                str(job["id"]),
                runner_pid=new_pid,
                error=f"recovered automatically after runner {pid or 'unknown'} disappeared",
            )
    except (OSError, ValueError, sqlite3.Error):
        # Lifecycle memory is fail-open. A broken optional Autopilot recovery
        # must never prevent the coding client from starting.
        return


def cmd_compaction_doctor(args: argparse.Namespace) -> int:
    report = store_from_args(args).compaction_report(
        cwd=args.cwd, all_workspaces=args.all
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        counts = report["counts"]
        print(
            f"{'OK' if report['ok'] else 'FAILED'} compaction continuity: "
            f"{counts['verified']} verified, {counts['pending']} recoverable pending, "
            f"{counts['failed']} failed, {counts['malformed']} malformed"
        )
    return 0 if report["ok"] else 1


def cmd_delegate_claude(args: argparse.Namespace) -> int:
    from .claude_worker import exit_code, run_delegation

    result = run_delegation(
        objective=args.objective,
        cwd=Path(args.cwd or Path.cwd()),
        constraints=args.constraint,
        validations=args.validation,
        allowed_paths=args.allowed_path,
        timeout_seconds=args.timeout,
        grace_seconds=args.kill_grace,
        claude_binary=args.claude_binary,
        model=args.model,
        effort=args.effort,
        permission_mode=args.permission_mode,
        task_id=args.task_id,
        max_budget_usd=args.max_budget_usd,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code(str(result["status"]))


def autopilot_store_from_args(args: argparse.Namespace):
    from .autopilot import AutopilotStore

    return AutopilotStore(store_from_args(args))


def _autopilot_launch_command(job_id: str, cwd: Path) -> list[str]:
    launcher = Path(sys.argv[0]).expanduser()
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return [str(launcher.resolve()), "autopilot", "run", job_id, "--cwd", str(cwd)]
    return [sys.executable, "-m", "memoryhub", "autopilot", "run", job_id, "--cwd", str(cwd)]


def _launch_autopilot(job_id: str, cwd: Path) -> tuple[int, Path]:
    from .core import memory_home

    logs = memory_home() / "autopilot" / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = logs / f"{job_id}.log"
    handle = log_path.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            _autopilot_launch_command(job_id, cwd),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        handle.close()
    log_path.chmod(0o600)
    return process.pid, log_path


def cmd_autopilot_start(args: argparse.Namespace) -> int:
    from .autopilot import default_goal
    from .autopilot_runner import supervise_job

    cwd = Path(args.cwd or Path.cwd()).expanduser().resolve()
    goal = default_goal(args.objective, cwd)
    store = autopilot_store_from_args(args)
    job_id = store.create_job(
        cwd=cwd,
        goal=goal,
        max_workers=args.max_workers,
        max_attempts=args.max_attempts,
        lead_provider=args.lead_provider,
    )
    if args.foreground:
        report = supervise_job(job_id=job_id, cwd=cwd)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "completed" else 1
    pid, log_path = _launch_autopilot(job_id, cwd)
    store.update_job(job_id, runner_pid=pid)
    print(
        json.dumps(
            {
                "job_id": job_id,
                "status": "planning",
                "runner_pid": pid,
                "log": str(log_path),
                "next": f"memoryhub autopilot status {job_id}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_autopilot_run(args: argparse.Namespace) -> int:
    from .autopilot_runner import supervise_job

    report = supervise_job(
        job_id=args.job_id,
        cwd=Path(args.cwd or Path.cwd()).expanduser().resolve(),
        max_restarts=args.max_restarts,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _resolve_autopilot_job(args: argparse.Namespace) -> str:
    if getattr(args, "job_id", None):
        return str(args.job_id)
    jobs = autopilot_store_from_args(args).list_jobs(
        cwd=Path(args.cwd).expanduser().resolve() if getattr(args, "cwd", None) else None,
        limit=1,
    )
    if not jobs:
        raise ValueError("no Autopilot job found")
    return str(jobs[0]["id"])


def cmd_autopilot_status(args: argparse.Namespace) -> int:
    report = autopilot_store_from_args(args).status(_resolve_autopilot_job(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_autopilot_list(args: argparse.Namespace) -> int:
    rows = autopilot_store_from_args(args).list_jobs(
        cwd=Path(args.cwd).expanduser().resolve() if args.cwd else None,
        limit=args.limit,
    )
    print(json.dumps(redact(rows), ensure_ascii=False, indent=2))
    return 0


def cmd_autopilot_usage(args: argparse.Namespace) -> int:
    from .autopilot_provider import query_usage

    store = autopilot_store_from_args(args)
    providers = [args.provider] if args.provider != "all" else ["codex", "claude"]
    for provider in providers:
        store.save_usage(
            query_usage(
                provider,
                timeout_seconds=args.timeout,
                codex_binary=args.codex_binary,
                claude_binary=args.claude_binary,
            )
        )
    print(
        json.dumps(
            {key: value.to_dict() for key, value in store.usage().items()},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_autopilot_recover(args: argparse.Namespace) -> int:
    store = autopilot_store_from_args(args)
    job_id = _resolve_autopilot_job(args)
    job = store.get_job(job_id)
    if str(job["status"]) in {"completed", "cancelled"}:
        raise ValueError(f"job {job_id} is already {job['status']}")
    cwd = (
        Path(args.cwd).expanduser().resolve()
        if args.cwd
        else store.job_workspace_path(job_id)
    )
    pid, log_path = _launch_autopilot(job_id, cwd)
    store.update_job(job_id, status="paused", runner_pid=pid, error="manual recovery started")
    print(json.dumps({"job_id": job_id, "runner_pid": pid, "log": str(log_path)}, indent=2))
    return 0


def cmd_autopilot_stop(args: argparse.Namespace) -> int:
    from .autopilot_runner import AutopilotRunner

    store = autopilot_store_from_args(args)
    job_id = _resolve_autopilot_job(args)
    job = store.get_job(job_id)
    pid = int(job["runner_pid"]) if job["runner_pid"] else None
    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    reaped = AutopilotRunner(
        store=store, refresh_provider_usage=False
    ).reap_orphan_provider_runs(job_id)
    store.recover_running_tasks(job_id, reason="Autopilot stopped by user")
    store.update_job(job_id, status="cancelled", error="stopped by user")
    print(
        json.dumps(
            {"job_id": job_id, "status": "cancelled", "provider_runs_reaped": reaped},
            indent=2,
        )
    )
    return 0


def item_map(args: argparse.Namespace) -> dict[str, list[str]]:
    return {
        "decision": args.decision,
        "blocker": args.blocker,
        "file": args.file,
        "validation": args.validation,
    }


def cmd_checkpoint(args: argparse.Namespace) -> int:
    task_id = store_from_args(args).checkpoint(
        actor=args.actor,
        cwd=args.cwd,
        task_id=args.task_id,
        title=args.title,
        objective=args.objective,
        status=args.status,
        summary=args.summary,
        next_action=args.next_action,
        items=item_map(args),
        current_session_id=args.session_id,
    )
    print(f"Checkpoint saved for {task_id}")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    print(store_from_args(args).render_context(cwd=args.cwd, task_id=args.task_id), end="")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    tasks = store_from_args(args).list_tasks(
        cwd=args.cwd, all_workspaces=args.all, limit=args.limit
    )
    if args.json:
        print(json.dumps(redact(tasks), ensure_ascii=False, indent=2))
        return 0
    if not tasks:
        print("No tasks found.")
        return 0
    for task in tasks:
        print(
            f"{task['id']}\t{task['status']}\t{task['workspace_name']}\t"
            f"{task['title']}\t{task['updated_at']}"
        )
    return 0


def short(value: Any, width: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def age_label(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def time_label(value: Any) -> str:
    return str(value or "").replace("T", " ")[:19]


def cmd_activity(args: argparse.Namespace) -> int:
    store = store_from_args(args)
    rows = store.activity(
        cwd=args.cwd,
        limit=args.limit,
        stale_seconds=parse_duration_seconds(args.stale_after),
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No agent sessions found.")
        return 0
    print("Agent activity")
    print("STATE   AGE   AGENT        WORKSPACE             TASK                   STATUS       NEXT")
    for row in rows:
        warning = f" WARN:{','.join(row['warnings'])}" if row.get("warnings") else ""
        task = row.get("task_title") or row.get("task_id") or "-"
        print(
            f"{row['state']:<7} {age_label(int(row['age_seconds'])):<5} "
            f"{short(row['actor'], 11):<12} {short(row['workspace_name'], 21):<21} "
            f"{short(task, 22):<22} {short(row['task_status'], 11):<11} "
            f"{short(row['next_action'] or row['last_event_text'], args.width)}{warning}"
        )
    return 0


def timeline_since(args: argparse.Namespace) -> str | None:
    if args.today:
        return today_start()
    if not args.since:
        return None
    try:
        return iso_before(parse_duration_seconds(args.since))
    except ValueError:
        return str(args.since)


def cmd_timeline(args: argparse.Namespace) -> int:
    rows = store_from_args(args).timeline(
        cwd=args.cwd,
        agent=args.agent,
        task_id=args.task_id,
        since=timeline_since(args),
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No timeline events found.")
        return 0
    print("Timeline")
    print("TIME                  AGENT        EVENT         WORKSPACE             TASK        DETAIL")
    for row in rows:
        print(
            f"{time_label(row['at']):<20} {short(row['actor'], 11):<12} "
            f"{short(row['type'], 12):<13} {short(row['workspace_name'], 21):<21} "
            f"{short(row['task_id'], 10):<10} {short(row['content_text'], args.width)}"
        )
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    report = store_from_args(args).cleanup_report(
        cwd=args.cwd,
        stale_seconds=parse_duration_seconds(args.stale),
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    counts = report["counts"]
    print(f"Cleanup report (dry-run, stale>{args.stale})")
    print(f"Stale sessions: {counts['stale_sessions']}")
    for item in report["stale_sessions"]:
        print(
            f"- {item['actor']} {item['session_id']} "
            f"{item['workspace_name']} last={item['last_event_at']} "
            f"task={item.get('task_id') or '-'}"
        )
    print(f"Stale tasks: {counts['stale_tasks']}")
    for item in report["stale_tasks"]:
        print(
            f"- {item['task_id']} {item['status']} {item['workspace_name']} "
            f"updated={item['updated_at']} {short(item['title'], args.width)}"
        )
    print(f"Tasks missing next action: {counts['missing_next_action']}")
    for item in report["missing_next_action"]:
        print(
            f"- {item['task_id']} {item['status']} {item['workspace_name']} "
            f"{short(item['title'], args.width)}"
        )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    store = store_from_args(args)
    store.resume_task(args.task_id, args.actor)
    print(store.render_context(task_id=args.task_id), end="")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = store_from_args(args)
    store.initialize()
    with store.connect() as db:
        if args.task_id:
            rows = db.execute(
                """
                SELECT at, type, actor, session_id, content_text
                FROM events WHERE task_id=? ORDER BY at DESC LIMIT ?
                """,
                (args.task_id, args.limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT at, type, actor, session_id, content_text
                FROM events ORDER BY at DESC LIMIT ?
                """,
                (args.limit,),
            ).fetchall()
    for row in reversed(rows):
        print(json.dumps(dict(row), ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    store = store_from_args(args)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        store.initialize()
        with store.connect() as db:
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            schema = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if integrity != "ok":
            errors.append(f"SQLite integrity check failed: {integrity}")
        if not schema:
            errors.append("schema version is missing")
    except Exception as error:  # doctor must report all failures together
        errors.append(str(error))

    try:
        mode = stat.S_IMODE(store.db_path.stat().st_mode)
        if mode & 0o077:
            errors.append(f"database permissions are too broad: {oct(mode)}")
    except OSError as error:
        errors.append(str(error))

    codex = shutil.which("codex")
    claude = shutil.which("claude")
    if not codex:
        warnings.append("Codex was not found in PATH")
    if not claude:
        warnings.append("Claude Code was not found in PATH")

    home = Path(args.target_home).expanduser() if args.target_home else Path.home()
    checks = {
        "Codex hooks": home / ".codex" / "hooks.json",
        "Claude settings": home / ".claude" / "settings.json",
    }
    for label, path in checks.items():
        configured = False
        if path.exists():
            content = path.read_text(encoding="utf-8")
            configured = "memoryhub" in content and "--event" in content
        if not configured:
            warnings.append(f"{label} do not contain Memory Hub integration: {path}")
    for label, path in {
        "Codex Autopilot skill": home / ".codex" / "skills" / "autopilot" / "SKILL.md",
        "Claude Autopilot skill": home / ".claude" / "skills" / "autopilot" / "SKILL.md",
    }.items():
        if not path.is_file():
            warnings.append(f"{label} is missing: {path}")

    print(f"OK database: {store.db_path}")
    print("OK SQLite integrity" if not errors else "FAILED SQLite integrity or permissions")
    print("OK network: no TCP/HTTP listener; MCP uses stdio")
    print(f"Detected Codex: {codex or 'no'}")
    print(f"Detected Claude: {claude or 'no'}")
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from .mcp_server import serve

    return serve()


def cmd_wiki_setup(args: argparse.Namespace) -> int:
    from .wiki_setup import setup

    result = setup(
        Path(args.target_home),
        mcp_entry=args.mcp_entry,
        api_url=args.api_url,
        configure_agents=not args.skip_agent_commands,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"LLM Wiki MCP: {result['mcp_entry']}")
        for agent, skill in result["skills"].items():
            state = "updated" if skill["changed"] else "unchanged"
            print(f"{agent} second-brain skill: {state} ({skill['path']})")
        for note in result["notes"]:
            print(f"WARNING: {note}")
        print("Run: memoryhub wiki-doctor")
    return 1 if result["notes"] else 0


def cmd_wiki_doctor(args: argparse.Namespace) -> int:
    from .wiki_setup import doctor

    result = doctor(Path(args.target_home), mcp_entry=args.mcp_entry, api_url=args.api_url)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{'OK' if result['mcp']['ok'] else 'FAILED'} LLM Wiki MCP/API")
        print(f"Version: {result['mcp']['version'] or 'unknown'}")
        print(f"Projects: {len(result['mcp']['projects'])}")
        for agent, check in result["skills"].items():
            print(f"{'OK' if check['matches'] else 'FAILED'} {agent} second-brain skill")
        for agent, check in result["registrations"].items():
            print(f"{'OK' if check['ok'] else 'FAILED'} {agent} MCP registration")
        if result["mcp"]["error"]:
            print(f"ERROR: {result['mcp']['error']}", file=sys.stderr)
    return 0 if result["ok"] else 1


def cmd_brain_register(args: argparse.Namespace) -> int:
    from .brain_sync import install_cron, register_brain

    result = register_brain(
        args.project_id,
        Path(args.repo),
        args.branch,
        api_url=args.api_url,
        config_path=Path(args.config).expanduser() if args.config else None,
        large_merge_threshold=args.large_merge_threshold,
        refresh_command=args.refresh_command or [],
        binary=args.binary,
        install_hook=not args.skip_hook,
    )
    if args.install_cron:
        binary = args.binary or shutil.which("memoryhub")
        if not binary:
            raise ValueError("memoryhub executable not found for cron")
        result["cron_changed"] = install_cron(
            binary,
            args.cron_interval,
            config_path=Path(args.config).expanduser() if args.config else None,
            api_url=args.api_url,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"Registered {result['project_id']} -> "
            f"{result['canonical_branch']}@{result['canonical_commit'][:12]}"
        )
        print(f"Hook: {result['hook'] or 'skipped'}")
    return 0


def cmd_brain_sync(args: argparse.Namespace) -> int:
    from .brain_sync import sync_all, sync_brain

    kwargs = {
        "config_path": Path(args.config).expanduser() if args.config else None,
        "state_path": Path(args.state).expanduser() if args.state else None,
        "api_url": args.api_url,
        "wait_seconds": args.wait_seconds,
        "force": args.force,
    }
    results = sync_all(**kwargs) if args.all else [sync_brain(args.project_id, **kwargs)]
    passed = all(item.get("status") == "fresh" for item in results)
    if not args.quiet:
        print(json.dumps({"ok": passed, "brains": results}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


def cmd_brain_doctor(args: argparse.Namespace) -> int:
    from .brain_sync import doctor_brains

    result = doctor_brains(
        project_id=args.project_id,
        config_path=Path(args.config).expanduser() if args.config else None,
        state_path=Path(args.state).expanduser() if args.state else None,
        api_url=args.api_url,
        deep=args.deep,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result["brains"]:
            detail = item.get("canonical_commit", "")[:12]
            print(f"{'OK' if item['passed'] else 'FAILED'} {item['project_id']}: {item['status']} {detail}")
    return 0 if result["ok"] else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="memoryhub",
        description="Local-only operational memory shared by coding agents.",
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--db", help="Override the local SQLite path")
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize the local database")
    init.set_defaults(func=cmd_init)

    hook = commands.add_parser("hook", help="Capture an agent lifecycle event from stdin")
    hook.add_argument(
        "--event",
        required=True,
        choices=[
            "session-start", "user-prompt", "tool", "stop",
            "pre-compact", "post-compact",
        ],
    )
    hook.add_argument("--actor", required=True)
    hook.set_defaults(func=cmd_hook)

    checkpoint = commands.add_parser("checkpoint", help="Write a structured task handoff")
    checkpoint.add_argument("--actor", required=True)
    checkpoint.add_argument("--cwd")
    checkpoint.add_argument("--task-id")
    checkpoint.add_argument("--session-id")
    checkpoint.add_argument("--title")
    checkpoint.add_argument("--objective")
    checkpoint.add_argument(
        "--status", choices=["in_progress", "blocked", "done", "archived"]
    )
    checkpoint.add_argument("--summary")
    checkpoint.add_argument("--next-action")
    checkpoint.add_argument("--decision", action="append", default=[])
    checkpoint.add_argument("--blocker", action="append", default=[])
    checkpoint.add_argument("--file", action="append", default=[])
    checkpoint.add_argument("--validation", action="append", default=[])
    checkpoint.set_defaults(func=cmd_checkpoint)

    context = commands.add_parser("context", help="Render the active task context")
    context.add_argument("--cwd")
    context.add_argument("--task-id")
    context.set_defaults(func=cmd_context)

    tasks = commands.add_parser("tasks", help="List remembered tasks")
    tasks.add_argument("--cwd")
    tasks.add_argument("--all", action="store_true")
    tasks.add_argument("--limit", type=int, default=20)
    tasks.add_argument("--json", action="store_true")
    tasks.set_defaults(func=cmd_tasks)

    activity = commands.add_parser(
        "activity", help="Show recent Codex/Claude sessions and their last known task state"
    )
    activity.add_argument("--cwd", help="Restrict the dashboard to one workspace")
    activity.add_argument("--limit", type=int, default=20)
    activity.add_argument("--stale-after", default="2h")
    activity.add_argument("--width", type=int, default=72)
    activity.add_argument("--json", action="store_true")
    activity.set_defaults(func=cmd_activity)

    timeline = commands.add_parser(
        "timeline", help="Show a readable chronological history of agent events"
    )
    timeline.add_argument("--cwd", help="Restrict the timeline to one workspace")
    timeline.add_argument("--agent")
    timeline.add_argument("--task-id")
    timeline_filter = timeline.add_mutually_exclusive_group()
    timeline_filter.add_argument("--today", action="store_true")
    timeline_filter.add_argument("--since", help="Duration like 24h/10d or an ISO timestamp")
    timeline.add_argument("--limit", type=int, default=50)
    timeline.add_argument("--width", type=int, default=96)
    timeline.add_argument("--json", action="store_true")
    timeline.set_defaults(func=cmd_timeline)

    cleanup = commands.add_parser(
        "cleanup", help="Report stale sessions/tasks without deleting anything"
    )
    cleanup.add_argument("--dry-run", action="store_true", default=True)
    cleanup.add_argument("--cwd", help="Restrict the report to one workspace")
    cleanup.add_argument("--stale", default="10d")
    cleanup.add_argument("--limit", type=int, default=50)
    cleanup.add_argument("--width", type=int, default=96)
    cleanup.add_argument("--json", action="store_true")
    cleanup.set_defaults(func=cmd_cleanup)

    resume = commands.add_parser("resume", help="Resume a task and print its context")
    resume.add_argument("task_id")
    resume.add_argument("--actor", default="human")
    resume.set_defaults(func=cmd_resume)

    history = commands.add_parser("history", help="Inspect the redacted local event journal")
    history.add_argument("--task-id")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(func=cmd_history)

    doctor = commands.add_parser("doctor", help="Validate database, privacy and integrations")
    doctor.add_argument("--target-home")
    doctor.set_defaults(func=cmd_doctor)

    compaction_doctor = commands.add_parser(
        "compaction-doctor",
        help="Verify durable pre/post compaction snapshots",
    )
    compaction_doctor.add_argument("--cwd")
    compaction_doctor.add_argument("--all", action="store_true")
    compaction_doctor.add_argument("--json", action="store_true")
    compaction_doctor.set_defaults(func=cmd_compaction_doctor)

    delegate = commands.add_parser(
        "delegate-claude",
        help="Run one bounded sequential Claude coding worker with hard cleanup",
    )
    delegate.add_argument("--objective", required=True)
    delegate.add_argument("--cwd")
    delegate.add_argument("--task-id")
    delegate.add_argument("--constraint", action="append", default=[])
    delegate.add_argument("--validation", action="append", default=[])
    delegate.add_argument("--allowed-path", action="append", default=[])
    delegate.add_argument("--timeout", type=int, default=900)
    delegate.add_argument("--kill-grace", type=float, default=3.0)
    delegate.add_argument("--claude-binary", default="claude")
    delegate.add_argument("--model", default="opus")
    delegate.add_argument(
        "--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high"
    )
    delegate.add_argument(
        "--permission-mode",
        choices=["acceptEdits", "auto", "dontAsk", "manual", "plan"],
        default="acceptEdits",
    )
    delegate.add_argument("--max-budget-usd", type=float)
    delegate.add_argument("--dry-run", action="store_true")
    delegate.set_defaults(func=cmd_delegate_claude)

    autopilot = commands.add_parser(
        "autopilot", help="Run a recoverable multi-session Codex/Claude engineering goal"
    )
    autopilot_commands = autopilot.add_subparsers(dest="autopilot_command", required=True)

    autopilot_start = autopilot_commands.add_parser("start", help="Start one Autopilot goal")
    autopilot_start.add_argument("--objective", required=True)
    autopilot_start.add_argument("--cwd")
    autopilot_start.add_argument("--max-workers", type=int, choices=[1, 2], default=1)
    autopilot_start.add_argument("--max-attempts", type=int, choices=[1, 2, 3], default=2)
    autopilot_start.add_argument(
        "--lead-provider", choices=["auto", "codex", "claude"], default="auto"
    )
    autopilot_start.add_argument("--foreground", action="store_true")
    autopilot_start.set_defaults(func=cmd_autopilot_start)

    autopilot_run = autopilot_commands.add_parser(
        "run", help="Internal foreground runner used by detached jobs"
    )
    autopilot_run.add_argument("job_id")
    autopilot_run.add_argument("--cwd")
    autopilot_run.add_argument("--max-restarts", type=int, default=3)
    autopilot_run.set_defaults(func=cmd_autopilot_run)

    autopilot_status = autopilot_commands.add_parser("status", help="Show one job and its tasks")
    autopilot_status.add_argument("job_id", nargs="?")
    autopilot_status.add_argument("--cwd")
    autopilot_status.set_defaults(func=cmd_autopilot_status)

    autopilot_list = autopilot_commands.add_parser("list", help="List local Autopilot jobs")
    autopilot_list.add_argument("--cwd")
    autopilot_list.add_argument("--limit", type=int, default=20)
    autopilot_list.set_defaults(func=cmd_autopilot_list)

    autopilot_usage = autopilot_commands.add_parser(
        "usage", help="Refresh normalized Codex/Claude subscription usage"
    )
    autopilot_usage.add_argument(
        "--provider", choices=["all", "codex", "claude"], default="all"
    )
    autopilot_usage.add_argument("--timeout", type=int, default=12)
    autopilot_usage.add_argument("--codex-binary", default="codex")
    autopilot_usage.add_argument("--claude-binary", default="claude")
    autopilot_usage.set_defaults(func=cmd_autopilot_usage)

    autopilot_recover = autopilot_commands.add_parser(
        "recover", help="Restart a paused or crashed Autopilot job"
    )
    autopilot_recover.add_argument("job_id", nargs="?")
    autopilot_recover.add_argument("--cwd")
    autopilot_recover.set_defaults(func=cmd_autopilot_recover)

    autopilot_stop = autopilot_commands.add_parser("stop", help="Stop one Autopilot job")
    autopilot_stop.add_argument("job_id", nargs="?")
    autopilot_stop.add_argument("--cwd")
    autopilot_stop.set_defaults(func=cmd_autopilot_stop)

    mcp = commands.add_parser("mcp", help="Run the local MCP stdio server")
    mcp.set_defaults(func=cmd_mcp)

    wiki_setup = commands.add_parser(
        "wiki-setup", help="Install the shared second-brain skill and register LLM Wiki"
    )
    wiki_setup.add_argument("--target-home", default=str(Path.home()))
    wiki_setup.add_argument("--mcp-entry")
    wiki_setup.add_argument("--api-url", default="http://127.0.0.1:19828")
    wiki_setup.add_argument("--skip-agent-commands", action="store_true")
    wiki_setup.add_argument("--json", action="store_true")
    wiki_setup.set_defaults(func=cmd_wiki_setup)

    wiki_doctor = commands.add_parser(
        "wiki-doctor", help="Validate the local LLM Wiki API, MCP tools and agent skills"
    )
    wiki_doctor.add_argument("--target-home", default=str(Path.home()))
    wiki_doctor.add_argument("--mcp-entry")
    wiki_doctor.add_argument("--api-url", default="http://127.0.0.1:19828")
    wiki_doctor.add_argument("--json", action="store_true")
    wiki_doctor.set_defaults(func=cmd_wiki_doctor)

    brain_register = commands.add_parser(
        "brain-register", help="Register a canonical Git branch for automatic LLM Wiki sync"
    )
    brain_register.add_argument("project_id")
    brain_register.add_argument("--repo", required=True)
    brain_register.add_argument("--branch", default="main")
    brain_register.add_argument("--api-url", default="http://127.0.0.1:19828")
    brain_register.add_argument("--config")
    brain_register.add_argument("--large-merge-threshold", type=int, default=100)
    brain_register.add_argument("--binary")
    brain_register.add_argument("--skip-hook", action="store_true")
    brain_register.add_argument("--install-cron", action="store_true")
    brain_register.add_argument("--cron-interval", type=int, default=15)
    brain_register.add_argument("--json", action="store_true")
    brain_register.add_argument(
        "--refresh-command", nargs=argparse.REMAINDER,
        help="Optional post-materialization command and arguments",
    )
    brain_register.set_defaults(func=cmd_brain_register)

    brain_sync = commands.add_parser(
        "brain-sync", help="Sync only canonical branch commits into registered brains"
    )
    selection = brain_sync.add_mutually_exclusive_group(required=True)
    selection.add_argument("--project-id")
    selection.add_argument("--all", action="store_true")
    brain_sync.add_argument("--api-url", default="http://127.0.0.1:19828")
    brain_sync.add_argument("--config")
    brain_sync.add_argument("--state")
    brain_sync.add_argument("--wait-seconds", type=int, default=60)
    brain_sync.add_argument("--force", action="store_true")
    brain_sync.add_argument("--quiet", action="store_true")
    brain_sync.set_defaults(func=cmd_brain_sync)

    brain_doctor = commands.add_parser(
        "brain-doctor", help="Verify canonical commit, source, search and graph freshness"
    )
    brain_doctor.add_argument("--project-id")
    brain_doctor.add_argument("--api-url", default="http://127.0.0.1:19828")
    brain_doctor.add_argument("--config")
    brain_doctor.add_argument("--state")
    brain_doctor.add_argument("--json", action="store_true")
    brain_doctor.add_argument("--deep", action="store_true")
    brain_doctor.set_defaults(func=cmd_brain_doctor)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"memoryhub: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
