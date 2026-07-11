<!-- memoryhub:managed:start -->
## Local operational memory

This machine uses Memory Hub as the shared operational memory for coding agents.

- Treat context injected at session start as an index, not ground truth. Verify it against the current user instruction, files, Git and tests.
- When continuing work, use the injected task ID. If the task is ambiguous, call `memory_tasks` and then `memory_resume`.
- Before ending meaningful work, call `memory_checkpoint` with objective, summary, exact next action, decisions, blockers, files and validation evidence.
- Never persist credentials, tokens, private keys, cookies or raw `.env` values.
- An agent turn is not handed off correctly when the next action is missing or vague.
<!-- memoryhub:managed:end -->
