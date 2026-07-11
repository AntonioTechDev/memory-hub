---
name: delegate-to-claude
description: Safely delegate one bounded coding implementation from Codex to Claude in the same working tree. Use when Codex assigns Claude a concrete code task and must enforce sequential execution, structured results, a hard timeout, process-group cleanup, shared Memory Hub continuity, scope checks, and independent review. Never invoke the raw Claude CLI for this workflow.
---

# Delegate to Claude

Use the bundled launcher for every Codex-to-Claude coding handoff. It runs one foreground Claude worker, records the result in Memory Hub, and terminates Claude plus its child processes on timeout or interruption.

## Required workflow

1. Finish Codex's current edit and inspect `git status --short`. Do not edit concurrently with Claude.
2. Reduce the work to one objective. Specify constraints, allowed paths, and commands that prove completion.
3. Invoke `scripts/delegate.py`; never call `claude` directly and never add `&`, `nohup`, `tmux`, `--bg`, or `--worktree`.
4. Read the JSON result and branch on `status`. Do not automatically retry a failure or timeout.
5. Review every changed path and the actual diff. Independently rerun the relevant tests before accepting the work.
6. Continue the parent task from the Memory Hub checkpoint written by the adapter.

## Invocation

Resolve the launcher relative to this skill directory, then run:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/delegate-to-claude/scripts/delegate.py" \
  --objective "Implement the bounded change" \
  --constraint "Preserve public APIs" \
  --allowed-path "src" \
  --allowed-path "tests" \
  --validation "python3 -m unittest discover -s tests -v" \
  --timeout 900
```

Prefer the default `opus` model and `high` effort for implementation work. Increase effort only when the task itself requires it; do not remove the hard timeout.

## Result gate

- `success`: review `changed_paths`, confirm `scope_violations` is empty, inspect the diff, and rerun validation.
- `timeout`: require `cleanup.reaped=true` and `cleanup.group_alive=false`; inspect the log and stop. Never retry automatically.
- `interrupted`, `failed`, `cleanup-failed`, `launch-error`, or `invalid-output`: inspect the report and fix the cause before any manual retry.
- `scope-violation`: preserve evidence, do not accept the result, and correct the out-of-contract changes deliberately.
- `blocked`: inspect Claude's blockers and choose the next action as Codex.

Read [references/contract.md](references/contract.md) when changing defaults, exit handling, or the delegation protocol.

## Safety invariants

- One Claude worker per workspace; the adapter fails fast if another is active.
- Same working tree is allowed only because execution is strictly sequential.
- Claude may edit files but may not commit, push, switch branches, reset, stash, or spawn background agents.
- Memory and logs are local, private, bounded, and redacted before persistence.
- A successful Claude response is evidence, not acceptance; Codex remains the reviewer and final owner.
