# Validation results — 2026-07-12

## Verdict

**Phases 1, 2 and 2.1 plus compaction-aware continuity and the bounded
Codex-to-Claude worker pass all local, structural, live-agent and deployed-brain
gates on the Automa Linux machine.** The automatic policy is canonical-only and
the four registered brains are fresh.

The live provider gates were rerun successfully on 2026-07-12 after the 0.3.1
installer/configuration cleanup. Claude and Codex both recovered shared
operational memory, survived abrupt-source recovery scenarios, and retrieved
fresh LLM Wiki graph/search facts from the same local brain.

## Current automated gates

| Check | Result | Evidence |
|---|---:|---|
| Full suite | PASS | 67/67 with `ResourceWarning` treated as error |
| Compaction continuity | PASS | deterministic pre/post pair verified; mutation detected |
| Compaction hook dedupe | PASS | `SessionStart` Memory Hub matcher is `startup|resume`; `PostCompact` owns reinjection |
| Agent-specific instructions | PASS | Codex keeps `$delegate-to-claude`; Claude does not receive Codex-only rules |
| Portable install tree | PASS | source, installed skill and app copy exclude `__pycache__` and `.pyc` artifacts |
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
| Latency | PASS | write p95 112.667 ms; context p95 8.056 ms |
| Compile check | PASS | `python3 -m compileall` |
| Deep deployed doctor | PASS | 4/4 brains; 3,401 canonical files checked |
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

Four real Automa brains passed the deployed doctor: two use `main`, two use
`master`, and 3,401 eligible files were compared. Two repositories were
deliberately left on non-canonical working branches during the audit; their
materialized sources still matched canonical Git rather than the checkout.
Each freshness canary is visible through LLM Wiki file, search and graph APIs;
queues are empty. Deployment-specific IDs, paths and commit hashes remain only
in the ignored local evaluation reports and are not part of the portable repo.

Automation installed on the machine:

- one idempotent post-commit hook per registered repository;
- one 15-minute `memoryhub brain-sync --all` cron fallback;
- only one LLM Wiki listener on `127.0.0.1:19828`.

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

## Remaining scope limits

- macOS and Windows clean-machine certification has not been run;
- remote multi-machine memory is intentionally not implemented;
- Phase 3 intelligent semantic memory remains a future product decision.
