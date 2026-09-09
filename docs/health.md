# Cluster health

Every minute, Cargo asks each live cluster's gateway two questions and writes down what it
finds. That is the whole feature. It never talks to datum, so it keeps working when datum is
down — which is the point of it.

Code lives in `cargo/object_storage/health/live.py`.

## What it asks

Two calls to the gateway's admin API, `GetClusterHealth` and `GetClusterStatus`. Both go to
the gateway only, which is the one node Cargo can reach. Nothing is installed on any node.

The read happens once per run and every check reads the same copy, so two checks can never
disagree about what the cluster looked like.

## What it checks

| Check | Verdict |
|---|---|
| Gateway did not answer | **Critical** |
| Some partitions cannot be written (`partitionsQuorum < partitions`) | **Critical** |
| Some partitions are missing a copy (`partitionsAllOk < partitions`) | **Degraded** |
| A node has been down longer than `node_offline_seconds` | **Degraded** |
| A volume is below `disk_critical_percent` free | **Critical** |
| A volume is below `disk_degraded_percent` free | **Degraded** |

The cluster's verdict is the worst of these. Nothing wrong means **Healthy**.

If the gateway does not answer, that is the *only* finding. Cargo does not guess at nodes or
disks it could not see.

## What "cannot be written" means

Garage chops the keyspace into 256 partitions, and each one lives on `replication_factor`
nodes. That is fixed — a bigger cluster spreads the same 256 around, it does not get more.

A partition needs enough of its copies up to accept a write. Lose that, and every object in
that partition fails to write, every time, for as long as it lasts. Other objects are fine.
It is not a slowdown and a retry does not help, which is why it is Critical.

`partitionsAllOk` is the softer one: every copy present. At `replication_factor` 2 the two
numbers are always identical, because "enough copies" and "every copy" are both 2. The
Degraded row above only becomes reachable at 3.

Be aware of what this means at 2: losing one node takes roughly two thirds of your
partitions offline for writes, not one.

## When a cluster is not judged

- **Still being built** — Unknown. It has not promised anything yet.
- **Build failed** — Critical, without calling the gateway.

"Live" means the cluster served at least once (`activated_on` is set).

## Where the verdict goes

Onto the cluster itself, as **Health** and **Health Reason**:

```
OSC-0001-storage-0015 has 8% free on its data volume
```

Findings name the machine, never the Garage node id, because the machine is the thing you
can look up. Cargo reads the name off the node's tag, which setup wrote there.

The verdict is written the moment it changes — an unreachable gateway is Critical on that
same tick, with no grace period. Central is **not** told, and the cluster's **Status** does
not change: Status is the build lifecycle, Health is what the cluster is doing now.

## The local log

Each run also appends one line to `logs/cluster_health.json.log`:

```json
{"timestamp": "...", "cluster": "OSC-0001", "severity": "Degraded",
 "reason": "...", "error": "", "health": {...}, "nodes": [...]}
```

`health` and `nodes` are Garage's raw replies, kept as-is.

This exists for one reason: when datum was unreachable, this is the only record of what
happened. Nothing reads it. There is no viewer, no index, and it is not meant to be queried
— open it with `tail` or `jq` when you are working out what broke.

Kept for `history_hours`, trimmed by an hourly job. Writing appends and never rewrites; the
hourly job is what deletes. If the scheduler stops, the log grows and nothing else breaks.

## Settings

**Object Storage Health Settings**, one Single for the whole region.

| Field | Default | What it does |
|---|---|---|
| `disk_degraded_percent` | 20 | Free space below this is Degraded |
| `disk_critical_percent` | 10 | Free space below this is Critical |
| `node_offline_seconds` | 120 | Shorter than this is a blip, not an outage |
| `admin_timeout_seconds` | 5 | Gateway call timeout. Under a minute, so a hung gateway is not still waiting on the next run |
| `history_hours` | 6 | How long the log keeps a reading |

Percentages must be 1–100, and critical must be lower than degraded, or nothing would ever
be Degraded. The durations must be at least 1: zero would time every call out instantly,
call every blink an outage, or throw each reading away as it was written.

## Schedule

| Job | When |
|---|---|
| `refresh_health` | every minute |
| `prune_history` | hourly |

Both in `cargo/hooks.py`. A minute is what sets how fast you hear about a problem.
