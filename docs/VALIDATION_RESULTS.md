# Validation results — 2026-07-13

## Verdict

**Memory Hub 0.5.0 passes the local, clean-install, operational dashboard and
Autopilot gates on the Automa Linux machine.** The automatic branch policy
remains canonical-only. The current headless shell could not complete the deep
deployed-brain freshness gate because the external LLM Wiki desktop API exits
during forced rescan; materialized source files still match canonical Git.

The 0.5.0 release adds `$autopilot` and the `memoryhub autopilot` lifecycle: a
durable local runner, goal/task contracts, subscription-aware Codex/Claude
routing, isolated worktrees, bounded retries, controlled two-worker
parallelism, deterministic validation and a fresh independent reviewer. A live
release gate completed with a real Claude planner/worker and a real Codex
reviewer. Claude created the scoped change but could not run Python because of
its headless approval policy; the runner independently ran 70 tests, preserved
the valid change, and completed the reviewed fast-forward without human input.

The 0.4.0 release added `memoryhub activity`, `memoryhub timeline` and
`memoryhub cleanup --dry-run` for daily operations. Live provider gates were
rerun successfully on 2026-07-12 after the installer/configuration cleanup.
Claude and Codex both recovered shared operational memory, survived
abrupt-source recovery scenarios, and retrieved fresh LLM Wiki graph/search
facts from the same local brain. A later headless rerun confirmed
`wiki-doctor` can pass when the LLM Wiki app is started under a display/Xvfb,
but the desktop API is not stable enough in this shell to complete
`brain-sync --all --force`.

## Current automated gates

| Check | Result | Evidence |
|---|---:|---|
| Full suite | PASS | 86/86 |
| Autopilot contract suite | PASS | 16/16: planning, routing, fallback, leases, orphan reaping, retry cap, scope and E2E |
| Real Autopilot goal | PASS | Claude plan+worker → runner recovery → 70/70 → fresh Codex review → fast-forward |
| False-positive validation gate | PASS | reviewer rejected a worker claim when the exact command exited 5 with zero tests |
| Provider sandbox recovery | PASS | Claude approval block and Codex bubblewrap block classified as infrastructure, never product blockers |
| Retry bound | PASS | configured two-attempt gate stops at exactly two attempts |
| Compaction continuity | PASS | deterministic pre/post pair verified; mutation detected |
| Compaction hook dedupe | PASS | `SessionStart` Memory Hub matcher is `startup|resume`; `PostCompact` owns reinjection |
| Agent-specific instructions | PASS | Codex keeps `$delegate-to-claude`; Claude does not receive Codex-only rules |
| Portable install tree | PASS | source, installed skill and app copy exclude `__pycache__` and `.pyc` artifacts |
| Three-command install | PASS | clone, enter repo, run installer+doctor in an isolated HOME |
| Activity dashboard | PASS | active, stale and ended sessions reported with task state |
| Timeline | PASS | events are chronological and filterable by agent/task/workspace |
| Cleanup dry-run | PASS | stale sessions/tasks and missing next actions reported without deletion |
| Delegation skill | PASS | official validator; portable/idempotent install |
| Fresh Codex skill discovery | PASS | selected the skill and wrapper once; dry-run, no raw Claude call |
| Adversarial cleanup | PASS | timeout and leaked-child groups killed; no live residual process |
| Real Claude delegation | PASS | Opus edited 1/1 allowed file; exact byte check; JSON schema valid |
| Installed live hooks | PASS | Codex and Claude both recovered 10/10 injected global-install facts |
| Bidirectional live handoff | PASS | Claude→Codex and Codex→Claude both scored 1.0 |
| Abrupt terminal live recovery | PASS | 10/10 across five Codex and five Claude receivers |
| Shared LLM Wiki live retrieval | PASS | Claude and Codex both returned graph/search values exactly |
| Brain agent live freshness | PASS | Claude and Codex both returned project, branch, commit and freshness token |
| Abuse/foolproof evaluation | PASS | 9/9; Phase 1 7/7, Phase 2.1 2/2 |
| Alternating chat storm | PASS | 500/500 events durable |
| Concurrent stress | PASS | 2,000/2,000 distinct writes, 48 workers |
| Stress secrecy/integrity | PASS | zero raw leaks; SQLite `ok`; DB `0600` |
| Latency | PASS | write p95 136.281 ms; context p95 11.624 ms |
| Compile check | PASS | `python3 -m compileall` |
| Deep deployed doctor | BLOCKED | LLM Wiki desktop API exits in headless shell during forced rescan |
| Deep materialization | PASS | zero missing, zero mismatched |

The abuse runner covers malformed and empty hook payloads, 100 duplicate
submissions, a terminal crash without finalization, a 200 KB secret-bearing
payload, the wrong customer workspace, invalid/incomplete handoffs, three
consecutive installations and 500 alternating Claude/Codex events.

The delegation tests additionally cover nonzero exit, malformed output,
out-of-scope edits, a second concurrent worker, dry-run behavior, a worker and
child that both ignore `SIGTERM`, and a nominally successful worker that leaks a
background child. The live authenticated run completed in an isolated test
repository with `cleanup.reaped=true`, `cleanup.group_alive=false`, exactly one
changed path and no scope violation. Codex then independently verified the
expected bytes and confirmed no `claude` or Claude-owned `node` process remained.
A separate fresh ephemeral Codex session discovered `$delegate-to-claude`, read
its contract and invoked the installed wrapper exactly once in `dry-run` mode;
it did not invoke the raw Claude CLI.

Phase 2.1 tests additionally cover a 150-file feature refactor, a large
canonical merge, incremental deletion, tamper detection/repair, sensitive and
binary files, refresh failure, eight concurrent sync requests, malformed
configuration, idempotent hooks and rejection of a remote LLM Wiki URL.

## Deployed canonical brains

Four real Automa brains are registered: two use `main`, two use `master`, and
3,401 eligible files were compared during the latest deep materialization
check. Two repositories were deliberately left on non-canonical working
branches during the audit; their materialized sources still matched canonical
Git rather than the checkout. In the current headless shell, the LLM Wiki
desktop API starts and passes `wiki-doctor`, then exits during
`memoryhub brain-sync --all --force`, so file/search/graph freshness evidence is
currently blocked by the external app runtime rather than by Memory Hub
materialization. Deployment-specific IDs, paths and commit hashes remain only in
the ignored local evaluation reports and are not part of the portable repo.

Automation installed on the machine:

- one idempotent post-commit hook per registered repository;
- one 15-minute `memoryhub brain-sync --all` cron fallback;
- LLM Wiki is expected to expose one listener on `127.0.0.1:19828` while the
  desktop app is running.

## Real-agent evidence

Completed live gates:

- globally installed hooks: Codex 10/10 and Claude 10/10;
- bidirectional operational handoff: Claude→Codex 10/10 and Codex→Claude 10/10;
- abrupt terminal recovery: 10/10 across five Codex and five Claude receivers;
- shared LLM Wiki retrieval: Claude 6/6 and Codex 6/6;
- Phase 2.1 exact canonical state: Claude 4/4 and Codex 4/4 for project, branch,
  40-character commit and freshness token.

The ignored local `evals/latest-*.json` reports contain the current successful
live runs. They remain intentionally untracked because they include
deployment-specific project IDs, paths, commit hashes and model output.

## Security review

- operational memory opens no network listener;
- LLM Wiki accepts loopback URLs only;
- no shell-string execution or `shell=True` exists in the adapter;
- refresh commands are explicit argument arrays;
- secret scan found only intentional redaction canaries in tests;
- source materialization rejects sensitive paths/content, binary data, invalid
  UTF-8 and files over 2 MB;
- no CRITICAL or HIGH issue remains open.

Autopilot additionally executes validation commands as argument arrays without
a shell, accepts only repository-oriented test/build commands, rejects
`python -c`, publish/deploy/push commands, checks changed paths again after
validation, and never treats a worker self-report as final evidence.

## Remaining scope limits

- macOS and Windows clean-machine certification has not been run;
- LLM Wiki freshness gates require the desktop API to stay running; this
  headless shell could not keep the Tauri app alive during forced rescan;
- remote multi-machine memory is intentionally not implemented;
- Phase 3 intelligent semantic memory remains a future product decision.
- Claude headless workers on this machine cannot approve some Bash commands;
  Autopilot recovers scoped changes by running its own deterministic gate, then
  still requires the independent reviewer.
