# Self-evaluation — release 0.5.0

Summary: overall score **4.6/5.0** across five quality axes.

| Axis | Score | Evidence and gap |
|---|---:|---|
| Accuracy | 5 | 86/86 tests, 9/9 abuse gates, 2,000/2,000 concurrent writes and a real completed Claude→runner→Codex Autopilot goal. A deliberately incorrect worker validation claim was rejected by the reviewer. |
| Completeness | 4 | Goal contracts, routing, usage, worktrees, bounded retries, crash recovery, runner-side validation, skills and installation are covered. Native macOS/Windows release certification is not yet available. |
| Clarity | 5 | Functional analysis, architecture, CLI help, skill contract and validation results describe separate responsibilities and terminal states. |
| Actionability | 5 | The same installer places the skill in Codex and Claude; one explicit `$autopilot`/`/autopilot` invocation starts a detached observable job. |
| Conciseness | 4 | The public surface is one skill and one nested CLI, but the implementation is split across three sizeable modules that could be reduced only after stable usage reveals genuine duplication. |

Critical issues: **none**.

Self-check: the user should agree because the score distinguishes the proven
Automa/Linux release from untested cross-platform portability and cites concrete
test and live-agent evidence rather than relying on implementation self-report.

Top improvements:

1. Run the clean-install and crash-recovery matrix on macOS and Windows before
   describing those platforms as certified.
2. Observe real long-running jobs before extracting or deleting orchestration
   abstractions; avoid speculative simplification before production evidence.

Verdict: **deliver as-is for the validated Linux release**.
