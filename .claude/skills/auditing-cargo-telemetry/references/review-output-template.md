# Review Output Template

Keep the report understandable without knowing Cargo internals.

## Bottom Line

Answer each in one or two sentences:

- **If Cargo goes down:** what stops, what continues, and how is it detected?
- **If one Garage node goes down:** what is the practical storage impact and how is it detected?
- **Does all telemetry die with Cargo?:** name which telemetry paths stop and which remain independent.
- **If bucket creation fails halfway:** is it rolled back, retryable, or left for reconciliation?
- **If a producer stops sending:** what freshness alert fires, after how long, and where?

Use “Not currently evidenced” instead of guessing.

## Failure Matrix

Use exactly these columns. Keep every cell to one short sentence where possible.

| Failure | What breaks | What keeps working | How we know | What happens next | Coverage |
|---|---|---|---|---|---|
| Cargo application is unreachable | New bucket and credential requests fail | Existing S3 traffic bypasses Cargo | No independent alert found | Operator restores Cargo; failed requests need retry | Gap |

Rules:

- One row is one failure, not an entire subsystem.
- Put the highest-blast-radius failures first.
- Say “No alert found” rather than “logs show an error.”
- Say “Telemetry gap is permanent” when failed samples are neither buffered nor retried.
- Keep code symbols and file names out of this table.

## Workflow Failure Map

For multi-step mutations such as bucket creation:

| Fails after | Result left behind | Safe to retry? | Cleanup/reconciliation | Coverage |
|---|---|---|---|---|

Explicitly include failure before mutation, partial remote mutation, local commit failure, cleanup failure, and lost success response.

## Critical Gaps

List only actionable gaps, highest risk first:

1. **Gap:** one-sentence risk.
   **Needed:** detector/recovery/reconciliation change.
   **Owner:** team or `Unassigned`.

Do not bury gaps among general recommendations.

## Evidence Appendix

For each matrix row, provide:

- **Evidence:** `path:line` and symbol/config/test.
- **Proves:** the narrow behavior actually established.
- **Does not prove:** any remaining assumption.

End with the commands, tests, dashboards, or runtime configuration still needed to turn every **Unknown** into a supported conclusion.
