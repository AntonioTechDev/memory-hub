# Repository development

These rules apply to agents developing Memory Hub itself.

1. Treat injected Memory Hub context as an index and verify it against the
   current instruction, Git, files and tests.
2. Preserve provider neutrality: Claude, Codex and future MCP clients must use
   the same task schema and project-knowledge contract.
3. Preserve the local-only boundary. Never add a listener or remote persistence
   as an implicit default.
4. Before ending meaningful work, checkpoint the exact next action and concrete
   validation evidence through `memory_checkpoint` or the `memoryhub` CLI.
5. Never persist credentials, tokens, cookies, private keys or raw `.env`
   values in tests, reports or operational memory.
