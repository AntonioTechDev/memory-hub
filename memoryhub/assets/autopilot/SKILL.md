---
name: autopilot
description: Start and supervise one long-running, recoverable local engineering goal through Memory Hub. Use only when the user explicitly invokes $autopilot or asks to activate Autopilot for autonomous multi-session work, provider fallback, bounded Codex/Claude workers, controlled parallelism and independent validation. Do not use for ordinary interactive edits unless the user explicitly requests Autopilot.
---

# Memory Hub Autopilot

Use Memory Hub as the control plane. Do not imitate the loop inside the current
chat and do not invoke raw Codex or Claude worker CLIs.

## Start

1. Reduce the user's message after `$autopilot` to the exact goal. Preserve its
   constraints and completion conditions; do not expand scope.
2. Verify that the current Git worktree is clean. If dirty, report the blocker
   instead of stashing, resetting or discarding changes.
3. Start exactly one job:

```bash
memoryhub autopilot start --objective "<goal>"
```

4. Return the job ID, initial status and the one status command. Do not keep a
   foreground worker running in this chat.

Autopilot defaults to one worker and chooses a second only when the validated
plan proves independent paths. Do not add flags unless the user explicitly
requests a provider or concurrency limit.

## Observe and control

```bash
memoryhub autopilot status <job-id>
memoryhub autopilot recover <job-id>
memoryhub autopilot stop <job-id>
```

- Use `status` when the user asks for progress from any later Codex or Claude
  chat.
- Use `recover` only when status shows a dead/paused runner; it is idempotent
  with respect to committed task state.
- Use `stop` only on explicit user request.

## Acceptance

Treat `completed` as credible only when the report contains deterministic
validation evidence and a passing independent review. `blocked`, `failed` and
`paused` are honest terminal/intermediate results, not reasons to bypass the
gate or launch raw provider commands.

Read [references/contract.md](references/contract.md) when diagnosing routing,
fallback, retries, usage or validation behavior.

## Safety

- Never enable paid API fallback, deploy, push or perform irreversible remote
  actions unless the goal explicitly authorizes them.
- Never edit an Autopilot worktree manually from the controller chat.
- Never run two workers in the same worktree.
- Never mark a goal complete from a worker self-report alone.
