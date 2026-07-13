from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

START_MARKER = "<!-- memoryhub:managed:start -->"
END_MARKER = "<!-- memoryhub:managed:end -->"

COMMON_INSTRUCTION_LINES = [
    "Treat context injected at session start as an index, not ground truth. Verify it against the current user instruction, files, Git and tests.",
    "When continuing work, use the injected task ID. If the task is ambiguous, call `memory_tasks` and then `memory_resume`.",
    "Before ending meaningful work, call the MCP tool `memory_checkpoint` with objective, summary, exact next action, decisions, blockers, files and validation evidence.",
    "Never persist credentials, tokens, private keys, cookies or raw `.env` values.",
    "An agent turn is not handed off correctly when the next action is missing or vague.",
]

CODEX_INSTRUCTION_LINES = [
    *COMMON_INSTRUCTION_LINES,
    "When Codex delegates coding work to Claude, it must use the installed `$delegate-to-claude` skill and its adapter; never invoke the raw `claude` CLI for that workflow.",
    "When the user explicitly invokes `$autopilot`, use the installed skill and Memory Hub runner; do not imitate the orchestration loop inside the current chat.",
]

CLAUDE_INSTRUCTION_LINES = [
    *COMMON_INSTRUCTION_LINES,
    "When the user explicitly invokes `/autopilot` or `$autopilot`, use the installed Memory Hub skill and runner; do not imitate the orchestration loop inside the current chat.",
]

IGNORED_TREE_NAMES = {"__pycache__"}
IGNORED_TREE_SUFFIXES = {".pyc", ".pyo"}


def instruction_block(lines: list[str]) -> str:
    bullets = "\n".join(f"- {line}" for line in lines)
    return f"""{START_MARKER}
## Local operational memory

This machine uses Memory Hub as the shared operational memory for coding agents.

{bullets}
{END_MARKER}
"""


CODEX_INSTRUCTIONS = instruction_block(CODEX_INSTRUCTION_LINES)
CLAUDE_INSTRUCTIONS = instruction_block(CLAUDE_INSTRUCTION_LINES)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.memoryhub-backup-{timestamp()}")
    shutil.copy2(path, target)
    return target


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def hook_command(binary: Path, event: str, actor: str) -> str:
    return f"{shlex.quote(str(binary))} hook --event {event} --actor {actor}"


def _is_memoryhub_handler(handler: dict[str, Any], event: str) -> bool:
    command = str(handler.get("command", ""))
    return "memoryhub" in command and f"--event {event}" in command


def _session_start_matcher() -> str:
    return "startup|resume"


def merge_hooks(path: Path, binary: Path, actor: str) -> bool:
    config = load_json(path)
    hooks = config.setdefault("hooks", {})
    event_names = {
        "SessionStart": "session-start",
        "UserPromptSubmit": "user-prompt",
        "PostToolUse": "tool",
        "Stop": "stop",
        "PreCompact": "pre-compact",
        "PostCompact": "post-compact",
    }
    changed = False
    for external_name, internal_name in event_names.items():
        entries = hooks.setdefault(external_name, [])
        command = hook_command(binary, internal_name, actor)
        already_present = False
        for group in entries:
            if not isinstance(group, dict):
                continue
            for handler in group.get("hooks", []):
                if isinstance(handler, dict) and _is_memoryhub_handler(handler, internal_name):
                    already_present = True
                    if external_name == "SessionStart":
                        expected = _session_start_matcher()
                        if group.get("matcher") != expected:
                            group["matcher"] = expected
                            changed = True
                        if handler.get("statusMessage") != "Loading local operational memory":
                            handler["statusMessage"] = "Loading local operational memory"
                            changed = True
        if already_present:
            continue
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": command, "timeout": 5}]
        }
        if external_name == "SessionStart":
            group["matcher"] = _session_start_matcher()
            group["hooks"][0]["statusMessage"] = "Loading local operational memory"
        entries.append(group)
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup(path)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def merge_instructions(path: Path, instructions: str) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if START_MARKER in current and END_MARKER in current:
        before, remainder = current.split(START_MARKER, 1)
        _, after = remainder.split(END_MARKER, 1)
        updated = before.rstrip() + "\n\n" + instructions + after.lstrip("\n")
    else:
        updated = current.rstrip() + ("\n\n" if current.strip() else "") + instructions
    if updated == current:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(updated, encoding="utf-8")
    return True


def copy_application(repo_root: Path, target_home: Path) -> tuple[Path, Path]:
    app_dir = target_home / ".local" / "share" / "memoryhub" / "app"
    binary = target_home / ".local" / "bin" / "memoryhub"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        repo_root / "memoryhub",
        app_dir / "memoryhub",
        ignore=_portable_copy_ignore,
    )
    binary.parent.mkdir(parents=True, exist_ok=True)
    installed_memory_home = target_home / ".local" / "share" / "memoryhub"
    launcher = f"""#!/usr/bin/env python3
import os
import sys
os.environ.setdefault("MEMORYHUB_HOME", {str(installed_memory_home)!r})
sys.path.insert(0, {str(app_dir)!r})
from memoryhub.cli import main
raise SystemExit(main())
"""
    binary.write_text(launcher, encoding="utf-8")
    binary.chmod(0o755)
    return app_dir, binary


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(_portable_files(path)):
        digest.update(str(item.relative_to(path)).encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.hexdigest()


def _is_ignored_tree_item(path: Path) -> bool:
    return path.name in IGNORED_TREE_NAMES or path.suffix in IGNORED_TREE_SUFFIXES


def _portable_files(path: Path) -> list[Path]:
    result: list[Path] = []
    for item in path.rglob("*"):
        if any(_is_ignored_tree_item(part) for part in item.relative_to(path).parents):
            continue
        if any(part.name in IGNORED_TREE_NAMES for part in item.relative_to(path).parents):
            continue
        if _is_ignored_tree_item(item):
            continue
        if item.is_file():
            result.append(item)
    return result


def _has_ignored_artifacts(path: Path) -> bool:
    return any(_is_ignored_tree_item(item) for item in path.rglob("*"))


def _portable_copy_ignore(_: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in IGNORED_TREE_NAMES or Path(name).suffix in IGNORED_TREE_SUFFIXES
    }


def install_codex_delegation_skill(target_home: Path) -> dict[str, Any]:
    source = Path(__file__).resolve().parent / "assets" / "delegate-to-claude"
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"bundled delegation skill is missing: {source}")
    target = target_home / ".codex" / "skills" / "delegate-to-claude"
    changed = (
        not target.is_dir()
        or _tree_hash(source) != _tree_hash(target)
        or _has_ignored_artifacts(target)
    )
    backup_path: Path | None = None
    if changed and target.exists():
        backup_path = target.with_name(
            f"{target.name}.memoryhub-backup-{timestamp()}"
        )
        counter = 1
        while backup_path.exists():
            backup_path = target.with_name(
                f"{target.name}.memoryhub-backup-{timestamp()}-{counter}"
            )
            counter += 1
        if target.is_dir():
            shutil.copytree(target, backup_path)
            shutil.rmtree(target)
        else:
            shutil.copy2(target, backup_path)
            target.unlink()
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=_portable_copy_ignore)
        (target / "scripts" / "delegate.py").chmod(0o755)
    return {
        "path": str(target),
        "changed": changed,
        "backup": str(backup_path) if backup_path else None,
        "sha256": _tree_hash(target),
    }


def install_bundled_skill(source_name: str, target: Path) -> dict[str, Any]:
    source = Path(__file__).resolve().parent / "assets" / source_name
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"bundled skill is missing: {source}")
    changed = (
        not target.is_dir()
        or _tree_hash(source) != _tree_hash(target)
        or _has_ignored_artifacts(target)
    )
    backup_path: Path | None = None
    if changed and target.exists():
        backup_path = target.with_name(f"{target.name}.memoryhub-backup-{timestamp()}")
        counter = 1
        while backup_path.exists():
            backup_path = target.with_name(
                f"{target.name}.memoryhub-backup-{timestamp()}-{counter}"
            )
            counter += 1
        if target.is_dir():
            shutil.copytree(target, backup_path)
            shutil.rmtree(target)
        else:
            shutil.copy2(target, backup_path)
            target.unlink()
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=_portable_copy_ignore)
    return {
        "path": str(target),
        "changed": changed,
        "backup": str(backup_path) if backup_path else None,
        "sha256": _tree_hash(target),
    }


def run_agent_commands(binary: Path, target_home: Path) -> list[str]:
    notes: list[str] = []
    env = {**os.environ, "HOME": str(target_home), "CODEX_HOME": str(target_home / ".codex")}
    if shutil.which("codex"):
        commands = [
            ["codex", "features", "enable", "hooks"],
            ["codex", "mcp", "remove", "memoryhub"],
            ["codex", "mcp", "add", "memoryhub", "--", str(binary), "mcp"],
        ]
        for index, command in enumerate(commands):
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            if result.returncode and index != 1:
                notes.append(f"Codex command failed: {' '.join(command)}: {result.stderr.strip()}")
    else:
        notes.append("Codex not found; MCP registration skipped")

    if shutil.which("claude"):
        subprocess.run(
            ["claude", "mcp", "remove", "--scope", "user", "memoryhub"],
            env=env,
            capture_output=True,
            text=True,
        )
        command = [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "memoryhub",
            "--",
            str(binary),
            "mcp",
        ]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        if result.returncode:
            notes.append(f"Claude command failed: {' '.join(command)}: {result.stderr.strip()}")
    else:
        notes.append("Claude Code not found; MCP registration skipped")
    return notes


def install(target_home: Path, *, configure_agents: bool = True) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    target_home = target_home.expanduser().resolve()
    app_dir, binary = copy_application(repo_root, target_home)
    env_before = os.environ.get("MEMORYHUB_HOME")
    os.environ["MEMORYHUB_HOME"] = str(target_home / ".local" / "share" / "memoryhub")
    try:
        from .core import MemoryStore

        MemoryStore().initialize()
    finally:
        if env_before is None:
            os.environ.pop("MEMORYHUB_HOME", None)
        else:
            os.environ["MEMORYHUB_HOME"] = env_before

    changed = {
        "codex_hooks": merge_hooks(target_home / ".codex" / "hooks.json", binary, "codex"),
        "claude_hooks": merge_hooks(
            target_home / ".claude" / "settings.json", binary, "claude-code"
        ),
        "codex_instructions": merge_instructions(
            target_home / ".codex" / "AGENTS.md", CODEX_INSTRUCTIONS
        ),
        "claude_instructions": merge_instructions(
            target_home / ".claude" / "CLAUDE.md", CLAUDE_INSTRUCTIONS
        ),
    }
    delegation_skill = install_codex_delegation_skill(target_home)
    autopilot_skills = {
        "codex": install_bundled_skill(
            "autopilot", target_home / ".codex" / "skills" / "autopilot"
        ),
        "claude": install_bundled_skill(
            "autopilot", target_home / ".claude" / "skills" / "autopilot"
        ),
    }
    notes = run_agent_commands(binary, target_home) if configure_agents else []
    return {
        "app_dir": str(app_dir),
        "binary": str(binary),
        "database": str(target_home / ".local" / "share" / "memoryhub" / "memory.db"),
        "changed": changed,
        "delegation_skill": delegation_skill,
        "autopilot_skills": autopilot_skills,
        "notes": notes,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Install Memory Hub locally for this user.")
    result.add_argument("--target-home", default=str(Path.home()))
    result.add_argument(
        "--skip-agent-commands",
        action="store_true",
        help="Do not invoke codex/claude MCP registration commands",
    )
    result.add_argument("--json", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    result = install(
        Path(args.target_home), configure_agents=not args.skip_agent_commands
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Memory Hub installed: {result['binary']}")
        print(f"Local database: {result['database']}")
        print("Network: disabled (SQLite + MCP stdio only)")
        for note in result["notes"]:
            print(f"WARNING: {note}")
        print(f"Run: {result['binary']} doctor --target-home {args.target_home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
