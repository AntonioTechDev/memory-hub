# Installation

## Requirements

- Linux with Python 3.11 or newer;
- Claude Code and/or Codex installed locally;
- Git and cron for automatic Phase 2.1 reconciliation;
- for Phase 2/2.1, an existing LLM Wiki build whose API listens only on
  `127.0.0.1` or `localhost`.

## Install operational continuity

```bash
git clone <repository-url> memory-hub
cd memory-hub
./install.sh
memoryhub doctor
```

The idempotent installer copies the dependency-free application to
`~/.local/share/memoryhub/app`, creates `~/.local/bin/memoryhub`, initializes a
private SQLite database and safely merges:

- `~/.codex/hooks.json`;
- `~/.claude/settings.json`;
- `~/.codex/AGENTS.md`;
- `~/.claude/CLAUDE.md`.

It also registers the same MCP `stdio` server in both clients. Changed existing
files are backed up before the managed block is replaced. Restart both agents
after the first installation.

## Connect the existing LLM Wiki

```bash
memoryhub wiki-setup
memoryhub wiki-doctor
```

The common MCP entry is auto-detected at
`~/workspace/llm_wiki/mcp-server/dist/src/index.js`. For another layout:

```bash
memoryhub wiki-setup \
  --mcp-entry /absolute/path/to/mcp-server/dist/src/index.js
```

The command installs the same `second-brain` skill for Claude and Codex and
registers the same MCP process and `http://127.0.0.1:19828` API. Only loopback
HTTP URLs with an explicit port are accepted.

## Keep a project brain fresh (Phase 2.1)

An LLM Wiki project must already exist. Register the Git repository and the one
branch considered authoritative:

```bash
memoryhub brain-register prj-example \
  --repo /absolute/path/to/repository \
  --branch main \
  --install-cron
memoryhub brain-sync --project-id prj-example
memoryhub brain-doctor --project-id prj-example --deep
```

Use `--branch master` for repositories whose canonical branch is `master`.
Registration writes private configuration to
`~/.config/memoryhub/brains.json`, installs an idempotent `post-commit` hook and,
when requested, one cron fallback. It never fetches, checks out, merges or
modifies Git.

Policy:

- a commit on a feature branch is ignored;
- a small canonical change is indexed incrementally;
- an initial sync, forced repair or change set of at least 100 files performs a
  full reconciliation;
- binary, oversized, credential-bearing and sensitive-path files are excluded;
- freshness is accepted only when the exact canonical commit canary is visible
  through LLM Wiki file, search and graph APIs.

Useful operations:

```bash
memoryhub brain-sync --all
memoryhub brain-sync --project-id prj-example --force
memoryhub brain-doctor
memoryhub brain-doctor --deep
```

`--deep` compares every eligible canonical Git blob with its materialized raw
source and wiki page. It is slower and intended for release/audit checks.

## Local data

```text
~/.local/share/memoryhub/
  memory.db
  brain-sync-state.json
  brain-sync-hook.log
  locks/
  app/

~/.config/memoryhub/
  brains.json
```

The data directory is `0700`; SQLite and configuration/state files are `0600`.
Set `MEMORYHUB_HOME` to move operational memory to another private local disk.
This does not enable remote access.

## Clean-room installation test

```bash
./install.sh --target-home /tmp/memoryhub-test --skip-agent-commands
/tmp/memoryhub-test/.local/bin/memoryhub doctor \
  --target-home /tmp/memoryhub-test
```

## Troubleshooting

```bash
memoryhub doctor
memoryhub wiki-doctor
memoryhub brain-doctor --deep
codex mcp list
claude mcp list
crontab -l
```

If a client does not see new hooks or MCP configuration, restart it. Do not
expose SQLite or the LLM Wiki API, and do not weaken a sandbox globally. Review
and allow loopback access only for the local LLM Wiki MCP when required.
