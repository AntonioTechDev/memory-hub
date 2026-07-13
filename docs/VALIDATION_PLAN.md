# Validation plan — phases 1, 2 and 2.1

## Phase 1 gates

| ID | Gate | Pass condition |
|---|---|---|
| P1 | Private install | SQLite `0600`, parent `0700`, no listener |
| P2 | Claude → Codex | exact objective/state/next action/evidence recovered |
| P3 | Codex → Claude | same exact recovery in reverse |
| P4 | Crash recovery | recent work survives without Stop/checkpoint |
| P5 | Cross-chat continuity | fresh session resolves the active task |
| P6 | Workspace isolation | no automatic context from another workspace |
| P7 | Global registry | explicit task listing spans local workspaces |
| P8 | Secret safety | zero raw credential canaries in SQLite |
| P9 | Concurrency | no loss/duplication; integrity `ok` |
| P10 | Idempotency | repeated events/install do not duplicate |
| P11 | Portability | Git remote identity survives a path move |
| P12 | Handoff validity | explicit status requires a concrete next action |
| P13 | Compaction snapshot | task state is hashed durably before compaction |
| P14 | Post-compact recovery | same state is verified and reinjected afterward |
| P15 | Compaction honesty | mutation between hooks is reported as failure |
| P16 | Activity dashboard | active, stale and ended sessions are reported with task state |
| P17 | Timeline | events are chronological and filterable by agent/task/workspace |
| P18 | Cleanup dry-run | stale sessions/tasks are reported without deletion |

## Codex-to-Claude delegation gates

| ID | Gate | Pass condition |
|---|---|---|
| D1 | Skill contract | official skill validator passes; installer is idempotent |
| D2 | Real worker | authenticated Claude edits only the allowed file and returns schema-valid JSON |
| D3 | Sequential lock | a second worker in one workspace fails immediately |
| D4 | Hard timeout | a `SIGTERM`-ignoring worker is killed and reaped |
| D5 | Descendant cleanup | a successful parent cannot leak a background child |
| D6 | Malformed/failure | invalid JSON and nonzero exits fail closed |
| D7 | Scope enforcement | out-of-contract paths cannot produce success |
| D8 | Continuity | every outcome creates a redacted Memory Hub checkpoint |
| D9 | Final ownership | Codex independently reviews diff and reruns validation |

## Phase 2 gates

| ID | Gate | Pass condition |
|---|---|---|
| W1 | Shared skill | Claude/Codex skill hashes match |
| W2 | Shared MCP | both clients target the same local entry/API |
| W3 | MCP contract | status, project, search and graph tools healthy |
| W4 | Scope isolation | explicit project first; no implicit customer merge |
| W5 | Real retrieval | both providers return the same hidden oracle values |
| W6 | Fail-open | Phase 1 works while LLM Wiki is unavailable |
| W7 | Local-only | non-loopback API URLs are rejected |
| W8 | Idempotency | repeat setup leaves one skill/config entry |

## Phase 2.1 gates

| ID | Gate | Pass condition |
|---|---|---|
| B1 | Feature isolation | hundreds of feature-branch files change nothing |
| B2 | Canonical merge | large merge triggers full reconciliation |
| B3 | Incremental update | small add/change/delete is exact and prunes removed files |
| B4 | Freshness proof | exact commit token appears in file, search and graph |
| B5 | Tamper recovery | deep doctor detects corruption; force sync repairs it |
| B6 | Secret/binary safety | excluded content never materializes |
| B7 | Failure honesty | failed refresh never records `fresh` |
| B8 | Concurrency | simultaneous syncs serialize without corruption |
| B9 | Automation | hook/cron installation is idempotent |
| B10 | Real deployment | every registered project matches its canonical Git ref |
| B11 | Provider alignment | real Claude and Codex return the same branch/commit/token |

## Autopilot gates

| ID | Gate | Pass condition |
|---|---|---|
| A1 | Proportional plan | `xs` remains one fast task; task caps reject over-engineering |
| A2 | Contract integrity | goal, criteria, validation and DAG are schema-valid and acyclic |
| A3 | Usage routing | real Codex/Claude usage is normalized; limited provider is not selected |
| A4 | Provider fallback | Codex rate limit hands a fresh attempt to Claude without lost state |
| A5 | Parallel safety | two disjoint tasks use isolated worktrees; overlapping scope serializes |
| A6 | Lease recovery | duplicate claims fail; dead runner tasks return to ready exactly once |
| A7 | Worker proof | self-report alone cannot pass path scope or deterministic validation |
| A8 | Integration | validated commits cherry-pick sequentially and conflicts fail closed |
| A9 | Final gate | executable validation plus fresh read-only review are both required |
| A10 | Source safety | fast-forward occurs only while original HEAD and clean state are unchanged |
| A11 | Process cleanup | timeout/crash leaves no worker process, lease or task worktree |
| A12 | Portable skill | Codex/Claude skills match, validate and install idempotently |

## Commands

```bash
PYTHONWARNINGS='error::ResourceWarning' python3 -m unittest discover -s tests -v
python3 scripts/run_foolproof_eval.py --events 500
python3 scripts/run_local_stress.py --events 2000 --workers 48
python3 -m compileall -q memoryhub scripts tests
python3 -m unittest tests.test_autopilot -v
memoryhub autopilot usage
memoryhub brain-doctor --deep
memoryhub compaction-doctor
memoryhub activity
memoryhub timeline --today
memoryhub cleanup --dry-run --stale 10d
```

Live gates, when both subscription CLIs have quota:

```bash
python3 scripts/run_real_agent_eval.py --live
python3 scripts/run_crash_eval.py --live --count 10
python3 scripts/run_installed_smoke.py --live
python3 scripts/run_llm_wiki_eval.py --live --oracle /path/to/local-oracle.json
python3 scripts/run_brain_agent_eval.py --live --project-id prj-example
```

## Release decision

The release is technically ready for the validated Linux installation when all
local/structural gates pass, deep doctor is green on every registered project
and both providers have passed the live alignment gates at least once on the
release code. A live re-run blocked solely by an external subscription quota is
reported as **blocked**, never converted to PASS or product failure.

Claims for macOS, Windows or remote multi-machine synchronization require their
own future gates; they are outside this release.
