from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .core import memory_home, redact_text
from .wiki_setup import DEFAULT_API_URL, validate_local_url

CONFIG_VERSION = 1
STATE_VERSION = 1
HOOK_START = "# >>> memoryhub brain-sync >>>"
HOOK_END = "# <<< memoryhub brain-sync <<<"
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_SOURCE_BYTES = 2_000_000

TEXT_EXTENSIONS = {
    ".bash", ".c", ".cc", ".cfg", ".cjs", ".conf", ".cpp", ".css",
    ".csv", ".dart", ".dockerfile", ".env-example", ".erl", ".ex", ".exs",
    ".fish", ".go", ".gql", ".graphql", ".h", ".hcl", ".hpp", ".htm",
    ".html", ".ini", ".java", ".jl", ".js", ".json", ".jsonc", ".jsx",
    ".kt", ".less", ".lua", ".md", ".mdx", ".mjs", ".mk", ".php",
    ".pl", ".prisma", ".proto", ".ps1", ".py", ".rb", ".rs", ".rst",
    ".sass", ".scala", ".scss", ".sh", ".sol", ".sql", ".svelte", ".swift",
    ".tf", ".thrift", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".vue",
    ".xml", ".yaml", ".yml", ".zsh",
}
ALLOWED_NAMES = {"dockerfile", "makefile", "license", "readme", "agents.md", "claude.md"}
SENSITIVE_NAMES = {
    ".env", ".npmrc", ".pypirc", "auth.json", "credentials.json", "id_rsa",
    "id_ed25519", "secrets.json", "service-account.json",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".pem", ".sqlite", ".sqlite3"}
EXCLUDED_PARTS = {
    ".git", ".idea", ".next", ".nuxt", ".output", ".turbo", ".venv",
    ".vscode", "__pycache__", "build", "coverage", "dist", "node_modules",
    "target", "vendor",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return (base / "memoryhub" / "brains.json").resolve()


def default_state_path() -> Path:
    return memory_home() / "brain-sync-state.json"


@contextmanager
def project_lock(project_id: str, timeout: float = 30.0):
    directory = memory_home() / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"brain-{validate_project_id(project_id)}.lock"
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, f"{os.getpid()}\n".encode())
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > 3600
            except OSError:
                stale = False
            if stale:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ValueError(f"brain sync is already running: {project_id}")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.close(descriptor)
        finally:
            try:
                path.unlink()
            except OSError:
                pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, path)
    path.chmod(0o600)


def _load_json(path: Path, *, empty: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(empty)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON: {path}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = (path or default_config_path()).expanduser().resolve()
    value = _load_json(config_path, empty={"version": CONFIG_VERSION, "brains": {}})
    if value.get("version") != CONFIG_VERSION or not isinstance(value.get("brains"), dict):
        raise ValueError(f"unsupported brain config: {config_path}")
    return value


def load_state(path: Path | None = None) -> dict[str, Any]:
    state_path = (path or default_state_path()).expanduser().resolve()
    value = _load_json(state_path, empty={"version": STATE_VERSION, "brains": {}})
    if value.get("version") != STATE_VERSION or not isinstance(value.get("brains"), dict):
        raise ValueError(f"unsupported brain state: {state_path}")
    return value


def validate_project_id(value: str) -> str:
    if not PROJECT_ID_RE.fullmatch(value):
        raise ValueError("project id must contain only letters, numbers, dot, underscore or dash")
    return value


def run_git(repo: Path, *args: str, timeout: int = 30, binary: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True,
        text=not binary, timeout=timeout,
    )
    if completed.returncode:
        detail = completed.stderr if isinstance(completed.stderr, str) else completed.stderr.decode(errors="replace")
        raise ValueError(f"git {' '.join(args)} failed: {detail.strip()}")
    return completed.stdout


def repo_root(path: Path) -> Path:
    root = str(run_git(path.expanduser().resolve(), "rev-parse", "--show-toplevel")).strip()
    return Path(root).resolve()


def canonical_commit(repo: Path, branch: str) -> str:
    # Local refs are intentional: automatic indexing must never fetch or mutate Git.
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        try:
            value = str(run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")).strip()
        except ValueError:
            continue
        if re.fullmatch(r"[0-9a-f]{40,64}", value):
            return value
    raise ValueError(f"canonical branch not found locally: {branch}")


def current_branch(repo: Path) -> str:
    return str(run_git(repo, "branch", "--show-current")).strip() or "DETACHED"


def tracked_paths(repo: Path, commit: str) -> list[str]:
    raw = run_git(repo, "ls-tree", "-r", "--name-only", "-z", commit, binary=True)
    assert isinstance(raw, bytes)
    return sorted(item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item)


def changed_paths(repo: Path, previous: str, current: str) -> list[str]:
    raw = run_git(repo, "diff", "--name-only", "-z", previous, current, binary=True)
    assert isinstance(raw, bytes)
    return sorted(set(item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item))


def git_file(repo: Path, commit: str, relative: str) -> bytes:
    value = run_git(repo, "show", f"{commit}:{relative}", timeout=60, binary=True)
    assert isinstance(value, bytes)
    return value


def allowed_path(relative: str) -> bool:
    if any(ord(character) < 32 for character in relative):
        return False
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if name in SENSITIVE_NAMES or suffix in SENSITIVE_SUFFIXES:
        return False
    if name.startswith(".env") and name not in {".env.example", ".env.sample"}:
        return False
    return suffix in TEXT_EXTENSIONS or name in ALLOWED_NAMES


def safe_source(data: bytes) -> tuple[str, str] | None:
    if len(data) > MAX_SOURCE_BYTES or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if redact_text(text, limit=max(len(text), 1)) != text:
        return None
    return text, hashlib.sha256(data).hexdigest()


def _language(relative: str) -> str:
    suffix = PurePosixPath(relative).suffix.lstrip(".").casefold()
    return {"py": "python", "js": "javascript", "ts": "typescript", "sh": "bash", "md": "markdown"}.get(suffix, suffix or "text")


def _digest(text: str, relative: str) -> dict[str, Any]:
    symbols: list[str] = []
    imports: list[str] = []
    seen_symbols: set[str] = set()
    seen_imports: set[str] = set()
    symbol_patterns = (
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait)\s+([A-Za-z_]\w*)"),
        re.compile(r"^\s*#{1,3}\s+(.+)$"),
    )
    import_patterns = (
        re.compile(r"^\s*import\s.*?from\s+[\"']([^\"']+)[\"']"),
        re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import"),
        re.compile(r"^\s*use\s+([A-Za-z0-9_:]+)"),
    )
    lines = text.splitlines()
    for line in lines:
        for pattern in symbol_patterns:
            match = pattern.match(line)
            if match and match.group(1) not in seen_symbols and len(symbols) < 40:
                seen_symbols.add(match.group(1)); symbols.append(match.group(1))
        for pattern in import_patterns:
            match = pattern.match(line)
            if match and match.group(1) not in seen_imports and len(imports) < 30:
                seen_imports.add(match.group(1)); imports.append(match.group(1))
    return {"lines": len(lines), "symbols": symbols, "imports": imports, "language": _language(relative)}


def source_page(relative: str, alias: str, text: str, sha256: str, commit: str) -> str:
    digest = _digest(text, relative)
    title = relative.replace('"', "'")
    raw_path = f"raw/sources/{alias}/{relative}"
    lines = [
        "---", "type: source", f'title: "{title}"', f'sources: ["{raw_path}"]',
        f"lang: {digest['language']}", f"lines: {digest['lines']}",
        f"src_sha: {sha256}", f"source_commit: {commit}", "---", "",
        f"# {relative}", "", f"`{raw_path}` · `{commit[:12]}`", "",
    ]
    if digest["symbols"]:
        lines.extend(["## Symbols", *[f"- `{item}`" for item in digest["symbols"]], ""])
    if digest["imports"]:
        lines.extend(["## Imports", *[f"- `{item}`" for item in digest["imports"]], ""])
    lines.extend(["Project: [[overview|Overview]] · [[index|Index]]", ""])
    return "\n".join(lines)


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(data)
    os.replace(temp, path)
    return True


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


@dataclass
class MaterializeResult:
    tracked: int
    eligible: int
    written: int
    removed: int
    secret_skipped: int
    manifest_count: int


def materialize(entry: dict[str, Any], commit: str, mode: str, paths: list[str]) -> MaterializeResult:
    repo = Path(entry["repo_path"])
    project = Path(entry["project_path"])
    alias = str(entry["source_alias"])
    raw_root = project / "raw" / "sources" / alias
    wiki_root = project / "wiki" / "sources" / alias
    all_tracked = tracked_paths(repo, commit)
    eligible_paths = [item for item in all_tracked if allowed_path(item)]
    eligible_set = set(eligible_paths)
    selected = eligible_paths if mode == "reconcile" else sorted(set(paths) | {p for p in paths if p not in eligible_set})
    written = removed = secret_skipped = 0
    manifest_path = project / "raw" / ".sources-manifest"
    if mode == "reconcile" or not manifest_path.exists():
        safe_manifest_set: set[str] = set()
    else:
        safe_manifest_set = {
            line.strip() for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(f"{alias}/")
        }

    # Full reconcile scans every tracked file. Incremental sync reads only the
    # changed paths and updates the previous safe manifest.
    cache: dict[str, tuple[str, str]] = {}
    inspect_paths = eligible_paths if mode == "reconcile" else [item for item in selected if item in eligible_set]
    for relative in inspect_paths:
        safe = safe_source(git_file(repo, commit, relative))
        if safe is None:
            secret_skipped += 1
            safe_manifest_set.discard(f"{alias}/{relative}")
            continue
        cache[relative] = safe
        safe_manifest_set.add(f"{alias}/{relative}")
    if mode != "reconcile":
        for relative in selected:
            if relative not in eligible_set:
                safe_manifest_set.discard(f"{alias}/{relative}")

    for relative in selected:
        raw_target = raw_root / PurePosixPath(relative)
        wiki_target = wiki_root / f"{relative}.md"
        safe = cache.get(relative)
        if safe is None:
            for target, stop in ((raw_target, raw_root), (wiki_target, wiki_root)):
                if target.is_file():
                    target.unlink(); removed += 1; _remove_empty_parents(target, stop)
            continue
        text, digest = safe
        if _write_if_changed(raw_target, text.encode("utf-8")):
            written += 1
        current_head = wiki_target.read_text(encoding="utf-8", errors="ignore")[:1500] if wiki_target.is_file() else ""
        # Preserve LLM enrichment when the underlying source is unchanged. The
        # project-level freshness page carries the newest canonical commit.
        if f"src_sha: {digest}" not in current_head:
            if _write_if_changed(wiki_target, source_page(relative, alias, text, digest, commit).encode("utf-8")):
                written += 1

    if mode == "reconcile":
        expected_raw = {str((raw_root / PurePosixPath(item)).resolve()) for item in cache}
        expected_wiki = {str((wiki_root / f"{item}.md").resolve()) for item in cache}
        for root, expected in ((raw_root, expected_raw), (wiki_root, expected_wiki)):
            if root.exists():
                for file in root.rglob("*"):
                    if file.is_file() and str(file.resolve()) not in expected:
                        file.unlink(); removed += 1

    safe_manifest = sorted(safe_manifest_set)
    _write_if_changed(manifest_path, ("\n".join(safe_manifest) + "\n").encode("utf-8"))
    return MaterializeResult(
        tracked=len(all_tracked), eligible=len(safe_manifest), written=written, removed=removed,
        secret_skipped=secret_skipped, manifest_count=len(safe_manifest),
    )


class LlmWikiApi:
    def __init__(self, base_url: str = DEFAULT_API_URL, timeout: int = 30) -> None:
        self.base_url = validate_local_url(base_url)
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.base_url}/api/v1{path}", data=data, method=method,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {})},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ValueError(f"LLM Wiki API failed: {error}") from error
        if not isinstance(payload, dict) or payload.get("ok") is False:
            raise ValueError(f"LLM Wiki API returned an error for {path}")
        return payload

    def projects(self) -> list[dict[str, Any]]:
        value = self.request("GET", "/projects")
        return [item for item in value.get("projects", []) if isinstance(item, dict)]

    def rescan(self, project_id: str) -> dict[str, Any]:
        return self.request("POST", f"/projects/{quote(project_id, safe='')}/sources/rescan")

    def search(self, project_id: str, query: str) -> dict[str, Any]:
        return self.request(
            "POST", f"/projects/{quote(project_id, safe='')}/search",
            {"query": query, "topK": 10, "includeContent": True},
        )

    def graph(self, project_id: str, query: str = "", limit: int = 1000) -> dict[str, Any]:
        params = urlencode({"q": query, "limit": limit})
        return self.request("GET", f"/projects/{quote(project_id, safe='')}/graph?{params}")

    def file(self, project_id: str, path: str) -> str:
        params = urlencode({"path": path})
        value = self.request("GET", f"/projects/{quote(project_id, safe='')}/files/content?{params}")
        return str(value.get("content", ""))


def resolve_project(api: LlmWikiApi, project_id: str) -> dict[str, Any]:
    matches = [item for item in api.projects() if str(item.get("id")) == project_id]
    if len(matches) != 1:
        raise ValueError(f"LLM Wiki project not found or ambiguous: {project_id}")
    path = Path(str(matches[0].get("path", ""))).expanduser().resolve()
    if not path.is_dir() or not (path / ".llm-wiki" / "project.json").is_file():
        raise ValueError(f"invalid LLM Wiki project path: {path}")
    return {**matches[0], "path": str(path)}


def _hook_directory(repo: Path) -> Path:
    try:
        configured = str(run_git(repo, "config", "--get", "core.hooksPath")).strip()
    except ValueError:
        configured = ""
    if configured:
        path = Path(configured).expanduser()
        return (path if path.is_absolute() else repo / path).resolve()
    value = str(run_git(repo, "rev-parse", "--git-path", "hooks")).strip()
    path = Path(value)
    return (path if path.is_absolute() else repo / path).resolve()


def install_post_commit_hook(
    repo: Path,
    project_id: str,
    binary: str,
    *,
    config_path: Path,
    api_url: str,
) -> Path:
    hooks = _hook_directory(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "post-commit"
    current = path.read_text(encoding="utf-8") if path.exists() else "#!/bin/sh\n"
    block = (
        f"{HOOK_START}\n"
        f"( {shlex.quote(binary)} brain-sync --project-id {shlex.quote(project_id)} "
        f"--config {shlex.quote(str(config_path))} --api-url {shlex.quote(api_url)} --quiet "
        f">>\"$HOME/.local/share/memoryhub/brain-sync-hook.log\" 2>&1 & ) || true\n"
        f"{HOOK_END}\n"
    )
    if HOOK_START in current and HOOK_END in current:
        before, remainder = current.split(HOOK_START, 1)
        _, after = remainder.split(HOOK_END, 1)
        updated = before.rstrip() + "\n" + block + after.lstrip("\n")
    else:
        updated = current.rstrip() + "\n" + block
    if updated != current:
        path.write_text(updated, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def register_brain(
    project_id: str,
    repo: Path,
    canonical_branch_name: str,
    *,
    api_url: str = DEFAULT_API_URL,
    config_path: Path | None = None,
    large_merge_threshold: int = 100,
    refresh_command: list[str] | None = None,
    binary: str | None = None,
    install_hook: bool = True,
) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    if large_merge_threshold < 1:
        raise ValueError("large merge threshold must be positive")
    root = repo_root(repo)
    head = canonical_commit(root, canonical_branch_name)
    api = LlmWikiApi(api_url)
    project = resolve_project(api, project_id)
    alias = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip(".-") or "repository"
    entry = {
        "project_id": project_id,
        "repo_path": str(root),
        "project_path": project["path"],
        "source_alias": alias,
        "canonical_branch": canonical_branch_name,
        "large_merge_threshold": large_merge_threshold,
        "refresh_command": list(refresh_command or []),
        "registered_at": utc_now(),
    }
    path = (config_path or default_config_path()).expanduser().resolve()
    config = load_config(path)
    config["brains"][project_id] = entry
    _atomic_json(path, config)
    hook_path = None
    if install_hook:
        executable = binary or shutil.which("memoryhub")
        if not executable:
            raise ValueError("memoryhub executable not found; pass --binary or install first")
        hook_path = install_post_commit_hook(
            root, project_id, executable, config_path=path, api_url=api.base_url
        )
    return {**entry, "canonical_commit": head, "hook": str(hook_path) if hook_path else None}


def _run_refresh(command: list[str], entry: dict[str, Any], commit: str, mode: str) -> None:
    if not command:
        return
    env = {
        **os.environ,
        "MEMORYHUB_BRAIN_PROJECT": str(entry["project_id"]),
        "MEMORYHUB_CANONICAL_BRANCH": str(entry["canonical_branch"]),
        "MEMORYHUB_CANONICAL_COMMIT": commit,
        "MEMORYHUB_SYNC_MODE": mode,
    }
    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"refresh command timed out: {command[0]}") from error
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise ValueError(f"refresh command failed ({completed.returncode}): {detail}")


def freshness_token(project_id: str, commit: str) -> str:
    return f"MEMORYHUB_FRESH_{project_id.replace('-', '_').upper()}_{commit[:16].upper()}"


def write_freshness_page(entry: dict[str, Any], commit: str, mode: str, changed: int) -> Path:
    token = freshness_token(str(entry["project_id"]), commit)
    path = Path(entry["project_path"]) / "wiki" / "memoryhub-freshness.md"
    content = (
        "---\n"
        "type: memoryhub-freshness\n"
        f'title: "{token}"\n'
        f'canonical_branch: "{entry["canonical_branch"]}"\n'
        f'canonical_commit: "{commit}"\n'
        f'sync_mode: "{mode}"\n'
        f"changed_files: {changed}\n"
        "---\n\n"
        f"# {token}\n\n"
        f"Canonical commit: `{commit}`\n"
    )
    _write_if_changed(path, content.encode("utf-8"))
    return path


def verify_freshness(api: LlmWikiApi, entry: dict[str, Any], commit: str) -> dict[str, Any]:
    project_id = str(entry["project_id"])
    token = freshness_token(project_id, commit)
    try:
        content = api.file(project_id, "wiki/memoryhub-freshness.md")
        file_ok = token in content and commit in content
    except ValueError:
        file_ok = False
    try:
        search = api.search(project_id, token)
        search_ok = any(
            token.casefold() in json.dumps(item, ensure_ascii=False).casefold()
            for item in search.get("results", []) if isinstance(item, dict)
        )
    except ValueError:
        search_ok = False
    try:
        graph = api.graph(project_id, token, 100)
        nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
        graph_ok = any(token.casefold() in json.dumps(item, ensure_ascii=False).casefold() for item in nodes)
        graph_nodes = len(nodes)
    except ValueError:
        graph_ok = False; graph_nodes = 0
    return {
        "token": token, "file": file_ok, "search": search_ok,
        "graph": graph_ok, "graph_nodes": graph_nodes,
        "passed": file_ok and search_ok and graph_ok,
    }


def verify_materialization(entry: dict[str, Any], commit: str) -> dict[str, Any]:
    repo = Path(entry["repo_path"])
    project = Path(entry["project_path"])
    alias = str(entry["source_alias"])
    raw_root = project / "raw" / "sources" / alias
    wiki_root = project / "wiki" / "sources" / alias
    checked = missing = mismatched = skipped = 0
    for relative in tracked_paths(repo, commit):
        if not allowed_path(relative):
            continue
        safe = safe_source(git_file(repo, commit, relative))
        if safe is None:
            skipped += 1
            continue
        text, digest = safe
        checked += 1
        raw = raw_root / PurePosixPath(relative)
        page = wiki_root / f"{relative}.md"
        if not raw.is_file() or not page.is_file():
            missing += 1
            continue
        if raw.read_text(encoding="utf-8", errors="replace") != text:
            mismatched += 1
            continue
        if f"src_sha: {digest}" not in page.read_text(encoding="utf-8", errors="ignore")[:1500]:
            mismatched += 1
    return {
        "checked": checked, "missing": missing, "mismatched": mismatched,
        "secret_or_binary_skipped": skipped, "passed": missing == 0 and mismatched == 0,
    }


def _sync_brain_unlocked(
    project_id: str,
    *,
    config_path: Path | None = None,
    state_path: Path | None = None,
    api_url: str = DEFAULT_API_URL,
    wait_seconds: int = 60,
    force: bool = False,
) -> dict[str, Any]:
    project_id = validate_project_id(project_id)
    config_file = (config_path or default_config_path()).expanduser().resolve()
    state_file = (state_path or default_state_path()).expanduser().resolve()
    config = load_config(config_file)
    entry = config["brains"].get(project_id)
    if not isinstance(entry, dict):
        raise ValueError(f"brain is not registered: {project_id}")
    repo = Path(str(entry["repo_path"]))
    commit = canonical_commit(repo, str(entry["canonical_branch"]))
    state = load_state(state_file)
    previous_record = state["brains"].get(project_id, {})
    previous = str(previous_record.get("commit", ""))
    if previous == commit and previous_record.get("status") == "fresh" and not force:
        live_evidence = verify_freshness(LlmWikiApi(api_url), entry, commit)
        if live_evidence["passed"]:
            return {
                "project_id": project_id, "status": "fresh", "noop": True,
                "commit": commit, "evidence": live_evidence,
            }
        force = True
    if previous and re.fullmatch(r"[0-9a-f]{40,64}", previous):
        try:
            paths = changed_paths(repo, previous, commit)
        except ValueError:
            paths = tracked_paths(repo, commit)
    else:
        paths = tracked_paths(repo, commit)
    threshold = int(entry.get("large_merge_threshold", 100))
    mode = "reconcile" if force or not previous or len(paths) >= threshold else "incremental"
    manifest_path = Path(entry["project_path"]) / "raw" / ".sources-manifest"
    if not manifest_path.is_file():
        mode = "reconcile"
    started = time.monotonic()
    materialized = materialize(entry, commit, mode, paths)
    write_freshness_page(entry, commit, mode, len(paths))
    _run_refresh(list(entry.get("refresh_command", [])), entry, commit, mode)
    api = LlmWikiApi(api_url)
    api.rescan(project_id)
    evidence = verify_freshness(api, entry, commit)
    deadline = time.monotonic() + max(0, wait_seconds)
    while not evidence["passed"] and time.monotonic() < deadline:
        time.sleep(min(2, max(0.1, deadline - time.monotonic())))
        evidence = verify_freshness(api, entry, commit)
    status = "fresh" if evidence["passed"] else "pending"
    record = {
        "project_id": project_id, "commit": commit, "branch": entry["canonical_branch"],
        "status": status, "mode": mode, "changed_files": len(paths),
        "synced_at": utc_now(), "duration_seconds": round(time.monotonic() - started, 3),
        "materialized": materialized.__dict__, "evidence": evidence,
    }
    state["brains"][project_id] = record
    _atomic_json(state_file, state)
    return record


def sync_brain(
    project_id: str,
    *,
    config_path: Path | None = None,
    state_path: Path | None = None,
    api_url: str = DEFAULT_API_URL,
    wait_seconds: int = 60,
    force: bool = False,
) -> dict[str, Any]:
    with project_lock(project_id):
        return _sync_brain_unlocked(
            project_id,
            config_path=config_path,
            state_path=state_path,
            api_url=api_url,
            wait_seconds=wait_seconds,
            force=force,
        )


def sync_all(**kwargs: Any) -> list[dict[str, Any]]:
    config_path = kwargs.get("config_path")
    config = load_config(config_path)
    results = []
    for project_id in sorted(config["brains"]):
        try:
            results.append(sync_brain(project_id, **kwargs))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results.append({"project_id": project_id, "status": "failed", "error": str(error)})
    return results


def doctor_brains(
    *,
    project_id: str | None = None,
    config_path: Path | None = None,
    state_path: Path | None = None,
    api_url: str = DEFAULT_API_URL,
    deep: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    state = load_state(state_path)
    ids = [validate_project_id(project_id)] if project_id else sorted(config["brains"])
    api = LlmWikiApi(api_url)
    results = []
    for item_id in ids:
        entry = config["brains"].get(item_id)
        if not isinstance(entry, dict):
            results.append({"project_id": item_id, "status": "unregistered", "passed": False})
            continue
        try:
            head = canonical_commit(Path(entry["repo_path"]), str(entry["canonical_branch"]))
            record = state["brains"].get(item_id, {})
            evidence = verify_freshness(api, entry, head)
            queue_path = Path(entry["project_path"]) / ".llm-wiki" / "file-change-queue.json"
            queue = _load_json(queue_path, empty={"tasks": []}) if queue_path.exists() else {"tasks": []}
            pending = len([task for task in queue.get("tasks", []) if isinstance(task, dict)])
            passed = record.get("commit") == head and record.get("status") == "fresh" and evidence["passed"] and pending == 0
            materialization = verify_materialization(entry, head) if deep else None
            if materialization is not None:
                passed = passed and materialization["passed"]
            results.append({
                "project_id": item_id, "passed": passed,
                "status": "fresh" if passed else "stale",
                "canonical_branch": entry["canonical_branch"], "canonical_commit": head,
                "current_branch": current_branch(Path(entry["repo_path"])),
                "recorded_commit": record.get("commit"), "pending_queue": pending,
                "evidence": evidence,
                **({"materialization": materialization} if materialization is not None else {}),
            })
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results.append({"project_id": item_id, "status": "failed", "passed": False, "error": str(error)})
    return {"ok": bool(results) and all(item["passed"] for item in results), "brains": results}


def install_cron(
    binary: str,
    interval_minutes: int = 15,
    *,
    config_path: Path | None = None,
    api_url: str = DEFAULT_API_URL,
) -> bool:
    if interval_minutes < 1 or 60 % interval_minutes:
        raise ValueError("cron interval must be a positive divisor of 60")
    marker = "# memoryhub brain-sync main-only"
    command_parts = [shlex.quote(binary), "brain-sync", "--all"]
    if config_path is not None:
        command_parts.extend(["--config", shlex.quote(str(config_path.expanduser().resolve()))])
    command_parts.extend(["--api-url", shlex.quote(validate_local_url(api_url)), "--quiet"])
    command = f"*/{interval_minutes} * * * * {' '.join(command_parts)}"
    completed = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    current = completed.stdout if completed.returncode == 0 else ""
    lines = [line for line in current.splitlines() if marker not in line and "memoryhub brain-sync --all" not in line]
    updated = "\n".join([*lines, marker, command]).strip() + "\n"
    if updated == current:
        return False
    result = subprocess.run(["crontab", "-"], input=updated, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"failed to install cron: {result.stderr.strip()}")
    return True
