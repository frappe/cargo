---
name: auditing-cargo-telemetry
description: Use when reviewing Cargo, Garage, Datum, Atlas, or Central for telemetry outages, silent metric loss, service failure modes, bucket workflow failures, alerting gaps, recovery behavior, or microservice resilience.
---

# Auditing Cargo Telemetry

## Purpose

Explain failures so an operator can answer three questions immediately: **what breaks, how do we know, and what do we do next?** Never call a failure handled without code, configuration, test, or runbook evidence.

## Start Here

1. Read [references/cargo-architecture.md](references/cargo-architecture.md) for the starting system map. Verify it against the current repository before making claims.
2. Read [references/failure-mode-catalog.md](references/failure-mode-catalog.md) for any outage, workflow, resilience, or telemetry-loss review.
3. Use [references/review-output-template.md](references/review-output-template.md) for the response.

## Review Method

1. Name the user-visible capability being reviewed, such as bucket creation, S3 traffic, Garage metrics, or pilot telemetry ingestion.
2. Draw its path across Atlas, Cargo, Central, Garage, Proxy, Datum, and ClickHouse. Separate:
   - **Control plane:** provisioning, bucket and credential management.
   - **Data plane:** S3 traffic and telemetry ingestion/querying.
   - **Observability plane:** probes, scrapes, logs, webhooks, dashboards, and alerts.
3. At every network call, queue, scheduler, datastore, credential boundary, and state transition, ask:
   - What fails and what remains usable?
   - Is failure atomic, retryable, idempotent, and reconciled?
   - Can partial success or a lost response orphan state or lose a one-time secret?
   - Who detects it, from outside the failed component, and within how long?
   - Where does the signal go, who owns recovery, and how is recovery verified?
4. Test both explicit errors and absence of signal. “No metrics arrived” needs a freshness/dead-man check; an error log written by the failed service is not independent detection.
5. Test compound failures: Cargo plus Garage, Cargo plus Datum, scheduler plus telemetry, webhook delivery plus retry, and monitoring plus the component it monitors.

## Coverage Labels

- **Handled:** detection, notification, bounded recovery, and verification are evidenced.
- **Partial:** some protection exists, but detection, delivery, recovery, or verification is missing.
- **Gap:** no effective protection exists.
- **Unknown:** evidence is insufficient. Never silently treat Unknown as Handled.

## Output Rules

- Lead with direct answers in plain language.
- Keep the main matrix to six columns and one failure per row.
- Describe impact in user terms; avoid internal class and method names in the matrix.
- Put file paths, symbols, and technical proof in the evidence appendix.
- Sort rows by blast radius, then likelihood. Show critical gaps first.
- Distinguish current behavior from recommendations.
- Do not claim that telemetry, storage, or alerts survive merely because their process is separate; trace their actual dependencies.



## We want to make things visible not fix every outage.
For example in case garage node is down or unreachable while the data would be safe with the RF, however new bucket creation or new uploads might fail, we want to transparently let the user know about this, like how we have webhooks that talk to central about the current service health.

This visibility will come via our insight charts and insight based alerting.

We also need to assume some worst cases like what if cargo itself is down?

Some niche cases — 
#### Pilot Image
1. Are app images building as expected.
2. How many images built and if a app image is repeatedly failing with some error.

#### Garage
1. If cargo itself is down then live telemetry won't work which means the insight charts some of them might be rendered useless driving us in the wrong direction — advertising a problem with garage when the problem is actually with cargo.
2. We need to wire up storage disk node expansion in case of full disks automatically via insight alerts of 80% > disk usage.
3. If avg upload errors raise we need to know.
4. Operators need to exactly know if current read/writes are possible with the nodes we have or not, simplest yes/no answers about the service first then the details.

#### Telemetry
1. Is clickhouse receiving data?
2. Did a service stop sending data unxpectedly (machine not terminated but telemetry stopped.)
