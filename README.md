# Memory Hub

Memory Hub is a local, provider-neutral continuity layer for coding agents.
Claude Code, Codex and future MCP-compatible clients share the same operational
task state, while an existing LLM Wiki remains the source of project knowledge.

The current release (`0.2.1`) provides three deliberately separate layers:

- **Phase 1 — operational continuity:** prompts, recent events and structured
  handoffs are stored in one private SQLite database for the local user;
- **Phase 2 — shared project knowledge:** Claude and Codex use the same
  `second-brain` skill and the same local LLM Wiki MCP;
- **Phase 2.1 — canonical freshness:** registered project brains automatically
  follow committed `main`/`master` content, never the currently checked-out
  feature branch.

There is no Automa cloud database, remote memory account, telemetry or public
listener. Every customer or workstation owns its own local database and LLM
Wiki installation.

## Quick start

```bash
git clone <repository-url> memory-hub
cd memory-hub && ./install.sh
memoryhub wiki-setup && memoryhub wiki-doctor
```

Register each existing LLM Wiki project once:

```bash
memoryhub brain-register prj-example \
  --repo /absolute/path/to/repository \
  --branch main \
  --install-cron
memoryhub brain-sync --project-id prj-example
memoryhub brain-doctor --project-id prj-example --deep
```

`brain-register` installs an idempotent `post-commit` hook. `--install-cron`
adds a 15-minute reconciliation fallback. Both read the canonical Git ref
directly, so a large refactor on a feature branch does not pollute the brain.
Once that ref is merged into `main`/`master`, the next sync performs a complete
reconciliation when the configured threshold is reached.

## Daily use

Hooks create or continue the active task automatically. Agents checkpoint
objective, decisions, blockers, validation evidence and one exact next action
through the `memory_checkpoint` MCP tool. Equivalent CLI commands are:

```bash
memoryhub tasks --all
memoryhub context
memoryhub checkpoint \
  --actor codex \
  --status in_progress \
  --summary "Service installed; smoke test pending" \
  --next-action "Run make smoke-test"
memoryhub resume task_0123456789abcdef
```

Stored memory is an index, not ground truth. Current user instructions, files,
Git and tests always take precedence.

## Validate

```bash
PYTHONWARNINGS='error::ResourceWarning' python3 -m unittest discover -s tests -v
python3 scripts/run_foolproof_eval.py --events 500
python3 scripts/run_local_stress.py --events 2000 --workers 48
memoryhub brain-doctor --deep
```

Optional live-provider gates consume Claude/Codex subscription usage:

```bash
python3 scripts/run_real_agent_eval.py --live
python3 scripts/run_crash_eval.py --live --count 10
python3 scripts/run_installed_smoke.py --live
cp evals/llm-wiki-oracle.example.json /tmp/my-llm-wiki-oracle.json
# Edit project/query fields for this installation, then:
python3 scripts/run_llm_wiki_eval.py --live --oracle /tmp/my-llm-wiki-oracle.json
python3 scripts/run_brain_agent_eval.py --live --project-id prj-example
```

See [architecture](docs/ARCHITECTURE.md),
[installation](docs/INSTALLATION.md),
[validation plan](docs/VALIDATION_PLAN.md), and
[latest validation results](docs/VALIDATION_RESULTS.md).

## Scope

Phase 3 semantic or long-term personal memory is intentionally deferred. The
first decision after observing real usage is whether operational continuity plus
fresh project graphs already solves the practical problem.

## License

MIT. See [LICENSE](LICENSE).
