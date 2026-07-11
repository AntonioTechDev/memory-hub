from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .core import MemoryStore, redact


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
    payload = read_payload()
    context = store_from_args(args).capture_hook(args.event, args.actor, payload)
    if args.event == "session-start":
        print(context, end="")
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
        choices=["session-start", "user-prompt", "tool", "stop", "pre-compact"],
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
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"memoryhub: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
