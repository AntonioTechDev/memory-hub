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

## Commands

```bash
PYTHONWARNINGS='error::ResourceWarning' python3 -m unittest discover -s tests -v
python3 scripts/run_foolproof_eval.py --events 500
python3 scripts/run_local_stress.py --events 2000 --workers 48
python3 -m compileall -q memoryhub scripts tests
memoryhub brain-doctor --deep
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
