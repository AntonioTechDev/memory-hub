# Delegation contract

The adapter invokes Claude once in foreground print mode with JSON-schema output and no session persistence. It uses `start_new_session=True`, so the worker and every descendant share a dedicated process group.

On hard timeout or parent interruption, the adapter sends `SIGTERM` to the process group, waits for the configured grace period, sends `SIGKILL` if needed, and reaps the direct child. It never retries automatically.

The default permission mode is `acceptEdits`. The prompt denies Git history/branch operations and background agents. `allowed_paths` is also checked against the working-tree state after Claude exits. Existing dirty paths are fingerprinted so further worker changes can be distinguished.

Statuses and exit codes:

- `success`: 0
- generic `failed`: 1
- `blocked`: 4
- `scope-violation`: 5
- `invalid-output`: 6
- `cleanup-failed`: 7
- `timeout`: 124
- `interrupted`: 130
- `launch-error`: 127

Each run writes a redacted mode-0600 report under `~/.local/share/memoryhub/delegations/runs/` and writes a Memory Hub checkpoint. A worker success always hands control back to Codex for diff review and independent validation.
