# Autopilot contract

Autopilot compiles the goal into a goal contract, task DAG and bounded task
contracts. Profiles are `fast`, `builder`, `senior` and `lead`; provider
adapters map them to native model and effort settings. The minimum sufficient
profile is selected first and escalated only after failed evidence.

The runner owns lifecycle and Git integration. It permits at most two workers,
uses isolated worktrees, validates allowed paths, executes allow-listed test
commands without a shell, commits validated task changes and cherry-picks them
sequentially into an integration branch.

Provider states are `available`, `constrained`, `rate_limited`, `unavailable`
and `unknown`. A rate limit opens a circuit breaker and routes a fresh attempt
to the alternative provider when possible. Paid API usage is never enabled
automatically.

Worker attempts end as success, blocked, failure, timeout, rate limit, invalid
output, scope violation or validation failure. A lease prevents duplicate work.
After the retry gate, the task becomes blocked instead of looping forever.

Completion requires executable validation evidence plus a schema-valid,
read-only review from a fresh lead session. If the source branch remains clean
and unmoved, the validated integration branch is fast-forwarded into it;
otherwise the branch is preserved for deliberate integration.
