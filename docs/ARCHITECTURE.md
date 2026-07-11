# Architecture — phases 1, 2 and 2.1

## Local boundary

Each workstation or customer installation is independent:

```text
Claude Code ----\
                 +--> lifecycle hooks + MCP stdio --> private SQLite
Codex ----------/

Claude Code ----\
                 +--> shared second-brain skill --> LLM Wiki MCP --> 127.0.0.1:19828
Codex ----------/                                      |
                                                       +--> local project graphs

canonical Git ref --> post-commit/cron --> brain sync --> LLM Wiki source + graph refresh
```

There is no central Automa memory service. An Automa VPS contains only Automa's
own brain; another person's machine contains only that person's local memory.
The database and API are not exposed online.

## Components

| Component | Responsibility |
|---|---|
| `memoryhub.core` | SQLite schema, global local-user task registry, workspace identity, redaction |
| `memoryhub.cli` | hooks, task operations, doctors and brain commands |
| `memoryhub.mcp_server` | provider-neutral task tools over process stdin/stdout |
| `memoryhub.install` | idempotent application and client configuration install |
| `memoryhub.wiki_setup` | shared skill and existing LLM Wiki MCP registration |
| `memoryhub.brain_sync` | canonical Git materialization, locking, rescan and freshness proof |

SQLite uses WAL, a ten-second busy timeout and `synchronous=NORMAL`. Event IDs
are idempotent, and bounded redacted events survive a missing final hook.

## Phase 1: operational state

The SQLite database is global for one local OS user. Workspaces are logical
scopes, not separate databases. A Git remote gives a stable workspace identity;
otherwise the normalized path is used. An explicit task ID cannot mutate a task
belonging to a different workspace.

Agents progressively record prompt, tool, stop and pre-compact events. A
structured checkpoint contains objective, status, summary, decisions, blockers,
files, validation evidence and one concrete next action. Recent events are a
fallback if a terminal dies before checkpointing.

## Phase 2: project knowledge

Claude and Codex receive an identical read-only `second-brain` skill and an MCP
registration pointing to the existing local LLM Wiki. Queries start with an
explicit `prj-*` scope; an area graph is used only for genuinely transversal
questions. Operational SQLite and project graphs remain separate sources.

## Phase 2.1: canonical-only freshness

Registration maps one LLM Wiki project to one repository and canonical branch.
Sync resolves `refs/heads/<branch>` (falling back to the local remote-tracking
ref), reads committed blobs directly with Git and never depends on the current
checkout. No fetch or working-tree mutation occurs.

```text
feature commit ------------------------------> ignored
canonical commit <100 changed files --------> incremental materialization
canonical commit >=100 / initial / repair ---> full reconciliation
                                                   |
                                                   +--> local LLM Wiki rescan
                                                   +--> exact commit canary
                                                   +--> file + search + graph proof
```

Every project has a cross-process lock. State changes to `fresh` only after an
optional refresh command succeeds and all three freshness probes agree. A deep
doctor independently compares every eligible Git blob with its raw and wiki
materialization. A failed refresh remains failed/pending and cannot claim a
fresh commit.

## Trust order

```text
current user instruction
  > files / Git / tests / runtime evidence
  > recent structured checkpoint
  > recent redacted event fallback
```

Memory provides context; it never authorizes an external action.

## Security model

- Operational MCP is `stdio`; it opens no listener.
- LLM Wiki targets are restricted to explicit loopback HTTP ports.
- Database, brain configuration and state are private to the local user.
- Known secrets are redacted before SQLite persistence.
- Brain sync rejects sensitive paths, detected credentials, binary/invalid UTF-8
  data and files larger than 2 MB.
- Git and refresh commands are argument arrays; no shell-string execution is
  used by the adapter.
- The sync process never fetches, checks out, commits, merges or pushes.
