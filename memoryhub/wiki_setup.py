from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_API_URL = "http://127.0.0.1:19828"
SKILL_RELATIVE = Path("assets") / "second-brain" / "SKILL.md"
REQUIRED_TOOLS = {"llm_wiki_status", "llm_wiki_projects", "llm_wiki_search", "llm_wiki_graph"}


def canonical_skill() -> Path:
    return Path(__file__).resolve().parent / SKILL_RELATIVE


def validate_local_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("LLM Wiki API must use local HTTP on localhost/127.0.0.1/::1")
    if not parsed.port:
        raise ValueError("LLM Wiki API URL must include a port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("LLM Wiki API URL must contain only local host and port")
    return value.rstrip("/")


def discover_mcp_entry(explicit: str | None = None, home: Path | None = None) -> Path:
    override = explicit or os.environ.get("LLM_WIKI_MCP_ENTRY")
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise ValueError(f"LLM Wiki MCP entry not found: {candidate}")
        return candidate
    base = (home or Path.home()).expanduser()
    candidates = (
        base / "workspace" / "llm_wiki" / "mcp-server" / "dist" / "src" / "index.js",
        base / "workspace" / "llm-wiki" / "mcp-server" / "dist" / "src" / "index.js",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(
        "LLM Wiki MCP entry was not found; pass --mcp-entry or set LLM_WIKI_MCP_ENTRY"
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_once(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.memoryhub-backup-{stamp}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.memoryhub-backup-{stamp}-{counter}")
        counter += 1
    shutil.copy2(path, target)
    return target


def install_skill(target_home: Path) -> dict[str, Any]:
    source = canonical_skill()
    if not source.is_file():
        raise ValueError(f"Bundled second-brain skill missing: {source}")
    result: dict[str, Any] = {}
    for agent, directory in (
        ("codex", target_home / ".codex" / "skills" / "second-brain"),
        ("claude", target_home / ".claude" / "skills" / "second-brain"),
    ):
        target = directory / "SKILL.md"
        changed = not target.exists() or file_hash(target) != file_hash(source)
        backup_path = _backup_once(target) if changed and target.exists() else None
        if changed:
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        result[agent] = {
            "path": str(target),
            "changed": changed,
            "backup": str(backup_path) if backup_path else None,
            "sha256": file_hash(target),
        }
    return result


def _run(command: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return 124, f"command timed out: {command[0]}"
    detail = (completed.stderr or completed.stdout).strip()
    return completed.returncode, detail


def register_agents(entry: Path, api_url: str, target_home: Path) -> list[str]:
    notes: list[str] = []
    env = {**os.environ, "HOME": str(target_home), "CODEX_HOME": str(target_home / ".codex")}
    if shutil.which("codex"):
        _run(["codex", "mcp", "remove", "llm-wiki"], env)
        code, detail = _run(
            [
                "codex", "mcp", "add", "llm-wiki", "--env",
                f"LLM_WIKI_API_URL={api_url}", "--", "node", str(entry),
            ],
            env,
        )
        if code:
            notes.append(f"Codex registration failed: {detail}")
    else:
        notes.append("Codex not found; LLM Wiki registration skipped")

    if shutil.which("claude"):
        _run(["claude", "mcp", "remove", "--scope", "user", "llm-wiki"], env)
        code, detail = _run(
            [
                "claude", "mcp", "add", "--scope", "user", "llm-wiki",
                "-e", f"LLM_WIKI_API_URL={api_url}", "--", "node", str(entry),
            ],
            env,
        )
        if code:
            notes.append(f"Claude registration failed: {detail}")
    else:
        notes.append("Claude Code not found; LLM Wiki registration skipped")
    return notes


def setup(
    target_home: Path,
    *,
    mcp_entry: str | None = None,
    api_url: str = DEFAULT_API_URL,
    configure_agents: bool = True,
) -> dict[str, Any]:
    target_home = target_home.expanduser().resolve()
    api_url = validate_local_url(api_url)
    entry = discover_mcp_entry(mcp_entry, target_home)
    skills = install_skill(target_home)
    notes = register_agents(entry, api_url, target_home) if configure_agents else []
    return {
        "api_url": api_url,
        "mcp_entry": str(entry),
        "skills": skills,
        "notes": notes,
    }


@dataclass
class ProbeResult:
    ok: bool
    version: str | None
    tools: list[str]
    projects: list[str]
    error: str | None = None


def probe_mcp(entry: Path, api_url: str, timeout: int = 10) -> ProbeResult:
    messages = [
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "memoryhub-doctor", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "llm_wiki_status", "arguments": {}},
        },
    ]
    try:
        completed = subprocess.run(
            ["node", str(entry)],
            env={**os.environ, "LLM_WIKI_API_URL": api_url},
            input="\n".join(json.dumps(item) for item in messages) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode:
            return ProbeResult(False, None, [], [], (completed.stderr or completed.stdout).strip())
        replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in replies if "id" in item}
        tool_names = sorted(
            item["name"] for item in by_id[2]["result"]["tools"] if isinstance(item, dict)
        )
        status_text = by_id[3]["result"]["content"][0]["text"]
        status = json.loads(status_text)
        projects = sorted(str(item["id"]) for item in status.get("projects", []))
        missing = sorted(REQUIRED_TOOLS - set(tool_names))
        if missing:
            return ProbeResult(False, status.get("version"), tool_names, projects, f"missing tools: {', '.join(missing)}")
        if not status.get("ok"):
            return ProbeResult(False, status.get("version"), tool_names, projects, "LLM Wiki API is not healthy")
        return ProbeResult(True, status.get("version"), tool_names, projects)
    except (OSError, KeyError, IndexError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        return ProbeResult(False, None, [], [], str(error))


def doctor(target_home: Path, *, mcp_entry: str | None = None, api_url: str = DEFAULT_API_URL) -> dict[str, Any]:
    target_home = target_home.expanduser().resolve()
    api_url = validate_local_url(api_url)
    entry = discover_mcp_entry(mcp_entry, target_home)
    expected_hash = file_hash(canonical_skill())
    skill_checks: dict[str, Any] = {}
    for agent, path in (
        ("codex", target_home / ".codex" / "skills" / "second-brain" / "SKILL.md"),
        ("claude", target_home / ".claude" / "skills" / "second-brain" / "SKILL.md"),
    ):
        actual_hash = file_hash(path) if path.is_file() else None
        skill_checks[agent] = {
            "path": str(path), "exists": path.is_file(), "matches": actual_hash == expected_hash,
        }
    probe = probe_mcp(entry, api_url)
    env = {**os.environ, "HOME": str(target_home), "CODEX_HOME": str(target_home / ".codex")}
    registrations: dict[str, Any] = {}
    for agent, command in (
        ("codex", ["codex", "mcp", "get", "llm-wiki"]),
        ("claude", ["claude", "mcp", "get", "llm-wiki"]),
    ):
        if not shutil.which(command[0]):
            registrations[agent] = {"ok": False, "error": f"{command[0]} not found"}
            continue
        code, detail = _run(command, env)
        registrations[agent] = {
            "ok": code == 0 and str(entry) in detail,
            "error": None if code == 0 else detail,
        }
    return {
        "ok": (
            probe.ok
            and all(item["matches"] for item in skill_checks.values())
            and all(item["ok"] for item in registrations.values())
        ),
        "api_url": api_url,
        "mcp_entry": str(entry),
        "skills": skill_checks,
        "registrations": registrations,
        "mcp": {
            "ok": probe.ok, "version": probe.version, "tools": probe.tools,
            "projects": probe.projects, "error": probe.error,
        },
    }
