# Watcher

Status: planned. Cargo does not implement this yet. The layouts in [Layouts](#layouts) need engineer approval before implementation.

## Purpose

Watcher is a Cargo module in each region. It finds abuse that starts from, or targets, VMs in the region, and reports each finding to Central.

- A scheduler job runs every minute.
- Watcher reads the per-VM abuse metrics in Datum, the logs in the log bucket, and external feeds.
- Watcher compares them with its thresholds and its feeds.
- After every run, Watcher sends a signed report to Central. The report holds the findings and the time up to which Watcher checked each input.
- Watcher knows no teams, trials, profiles, cases or policy. Central decides what to do with each finding. See [Central findings from Watcher](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#findings-from-watcher).
- Watcher never calls Atlas.

```text
Metal ──metrics──▶ Datum ─────────────┐
Metal, HTTP proxy ──logs──▶ Log store (Garage bucket) ──▶ Watcher (Cargo, per region) ◀── Blocklists, shared abuse databases
                                                              │ findings (signed report)
Central ◀─────────────────────────────────────────────────────┘
Central ──commands──▶ Atlas ──▶ Metal
```

Other documents:

- Log bucket, keys and the per-VM table: [abuse logs and metrics](abuse-logs.md).
- Counters, port classes and log formats: [Atlas counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters), [Atlas port classes](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#port-classes), [Atlas flow log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#flow-log), [Atlas SNI log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#sni-log) and [Atlas NAT port ranges](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#nat-port-ranges).
- Profiles, exceptions, signals, cases and the deploy order: [Central abuse spec](https://github.com/frappe/central/blob/develop/spec/ABUSE.md).

## Inputs

| Input | Read with | Use |
|---|---|---|
| Per-VM `vm_abuse_*` table in Datum | The read-only ClickHouse user `watcher`. See [per-VM metrics](abuse-logs.md#per-vm-metrics). | Volume rules, and the dedicated public addresses of each VM (`vm_abuse_public_address`) for address checks |
| Flow log, SNI log, `nat-ranges` log and proxy access log in the log bucket | The read key. See [keys](abuse-logs.md#keys) and [object layout](abuse-logs.md#object-layout). | Feed matches from the flow log and the SNI log, hostname checks from the proxy access log, the shared host addresses for address checks and attribution from the `nat-ranges` log |
| Blocklists and shared abuse databases | Each [Abuse Feed](#feeds) | Feed matches |

Watcher reads only metrics and log rows. It never reads the content of VM traffic.

Both credentials stay in Cargo. Watcher reads the `watcher` password from the Datum Server and the read key from the log bucket record. No call to Central carries them.

### Metrics

Metal exports per-VM counters to Datum at the metald telemetry interval, 30 s by default. Each series has the label `virtual_machine`, the Atlas VM ID, for example `vm-0a1b2c3d4e`. Datum keeps `vm_abuse_*` series for 30 days. See [Atlas counters](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#counters).

Watcher uses these series:

| Series | Use |
|---|---|
| `vm_abuse_transmit_packets_total`, `vm_abuse_transmit_bytes_total` | Outbound floods and volume. Labels `scope` (public or mesh) and `protocol`. |
| `vm_abuse_receive_packets_total`, `vm_abuse_receive_bytes_total` | Answer ratios and inbound floods. Same labels. |
| `vm_abuse_tcp_initiated_total`, `vm_abuse_tcp_answered_total` | SYN floods and scan answer ratios |
| `vm_abuse_udp_first_packets_total` | UDP to new destinations |
| `vm_abuse_destinations` | Gauge: distinct public destinations in the window. Labels `port_class` (a class, or `all`) and `window` (`60s` or `10m`). |
| `vm_abuse_ports_per_destination_max` | Gauge: the highest number of distinct ports to one public address in the last 60 s |
| `vm_abuse_flows_per_destination_max` | Gauge: the highest number of new flows to one public address in the last 10 min. Label `port_class`. |
| `vm_abuse_unanswered_pairs` | Gauge: unanswered address and port pairs in the last 1 h |
| `vm_abuse_smtp_blocked_total` | Port 25 attempts that `smtp_outbound` blocked. Metal blocks port 25 only for a VM whose network has `is_port_25_blocked`. |
| `vm_abuse_stratum_flows_total` | New flows with a stratum match in the first payload bytes |
| `vm_abuse_amplification_bytes_total` | Public UDP bytes of the `amplification` class. Labels `direction` and `port`. `transmit` counts bytes from an amplification source port. `receive` counts bytes to an amplification destination port. |
| `vm_abuse_public_address` | Gauge, value 1: one public IPv4 or IPv6 address that the VM has for itself. Label `address`. Watcher uses it for [address checks](#feeds). The shared host address is not in this series. It is in the `nat-ranges` log. |

Watcher computes rates from counter differences. A counter that goes down is a metald restart. Watcher treats it as a gap, not as a rate.

Port classes (`auth`, `smtp`, `submission`, `amplification`, `mining`) are a fixed list in metald. Watcher reads them only as the `port_class` label. See [Atlas port classes](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#port-classes).

### Stale inputs

- If the Datum query fails, or its newest sample is older than 5 min, Watcher reports no finding from Datum in that run and writes an Error Log.
- If the log bucket does not answer, Watcher reports no finding from logs in that run and writes an Error Log. It reads the missed objects on a later run.
- If a feed fetch fails, Watcher keeps the last good entries. See [Feeds](#feeds).
- If an input fails, Watcher does not move `checked_until` for that input. Central sees the lag. See [Findings](#findings).

## Finding codes

A finding code uses the GuardDuty style `<threat>:<resource>/<name>`. A finding never names a reporter. Its `evidence` holds counts, ports, ratios and destination prefixes. See [Findings](#findings).

| Finding code | Match | Source |
|---|---|---|
| `DoS:VM/OutboundFlood` | Public pps or bps above the threshold | Datum |
| `DoS:VM/SynFlood` | Initiated SYN to few destinations, few answered | Datum |
| `Recon:VM/Portscan` | Fan-out with a low answer ratio | Datum |
| `UnauthorizedAccess:VM/BruteForce` | Auth ports to many addresses, or many connections to one | Datum |
| `Smtp:VM/BlockedPort25` | Fewer than 500 blocked port 25 attempts, to fewer than 50 destinations | Datum |
| `Spam:VM/Smtp` | Many port 25 attempts, a submission spray, or a public address on a DNSBL or in SNDS | Datum, feed |
| `Reflector:VM/<protocol>` | Amplification ratio, or a Shadowserver report | Datum, feed |
| `CryptoCurrency:VM/PoolConnection` | Pool address from the feed in the flow log, or a stratum match | Flow log, Datum |
| `CryptoCurrency:VM/PoolName` | Pool name in the SNI log | SNI log |
| `Backdoor:VM/C2Activity` | Flows to Spamhaus DROP or abuse.ch Feodo addresses | Flow log |
| `Proxy:VM/Tor` | Connections to Tor relays from the public consensus | Flow log |
| `Phishing:Site/Reported` | A hostname from the proxy access log on Safe Browsing, OpenPhish or URLhaus | Proxy access log, feed |
| `Phishing:Bucket/Reported` | A public bucket hostname on a phishing feed | Bucket, feed |
| `DoS:VM/InboundFlood` | Received pps or bps on a VM above the threshold | Datum |
| `Integrity:Log/Missing` | A log object is missing, changed or deleted. Severity High, no VM. See [Log integrity](#log-integrity). | Log bucket |

Central keeps the codes that need team data or a report, for example `Phishing:Site/LookalikeDomain` and `Content:Site/Illegal`. Watcher does not produce them.

## Threshold rules

1. Every rule has a volume floor and a shape condition: answer ratio, packet size, fan-out or protocol.
2. Rules count public traffic only. Mesh traffic never counts.
3. Limits are absolute. They do not change with the VM size.
4. A rule needs a series in [Metrics](#metrics). A rule whose series Metal does not export stays Off.

## Windows

Every window is 1 min or longer.

| Window | Use |
|---|---|
| 1 min | Volumetric flood, horizontal and vertical scan, reflector |
| 2 min | UDP, small-packet, ICMP, and SYN floods, inbound flood |
| 10 min | Brute force, SMTP, mining stratum |
| 1 h | Slow scan, C2 |

Reaction time is the window, plus up to 30 s until the next sample, plus up to 1 min until the next scheduler run. From Phase 3, when the thresholds are Active, for the flood and scan rules, with 1 min and 2 min windows, this is about 3 min (2.5 min to 3.5 min) from the start of abuse to a restriction. No automatic limit exists before that. A VM can flood or scan for these minutes. This is an accepted trade-off: Atlas keeps no detection state, and Watcher finds all abuse.

## Thresholds

An [Abuse Threshold](#abuse-threshold) holds the trigger values of one finding code in the region.

- `mode` is Off, Shadow or Active. A finding from a Shadow threshold has `is_shadow`. Central stores it and takes no action. A finding from an Active threshold goes to the Central policy engine.
- An operator changes a value or a mode in Desk, with no deploy.
- These are starting values. They ship as fixtures, not as JSON defaults, so a region can use other values. No value is Active until [calibration](#calibration) confirms it.
- The "Reads" column names the [series](#metrics) or the source of each rule. Rates and ratios use the `scope="public"` series. A 1 min count of destinations reads `window="60s"`. A 10 min count reads `window="10m"`.

The values below are the standard level.

Report level:

- Watcher reports a finding when the measured value reaches 0.5 times the standard threshold. This is the strict level.
- The level applies to volume triggers: rates, counts of addresses and counts of attempts. These are the triggers with `is_volume`. They scale with the profile.
- Watcher checks ratio and shape conditions (answer ratio, packet size, bytes out against bytes in) at the value in the table.
- `measured` and `threshold` in the finding hold only the volume triggers. Watcher puts the measured ratios in `evidence`. See [Findings](#findings).
- A feed match or a stratum match has empty `measured` and `threshold`.
- Central applies the profile and any exception. It acts only when every value in `measured` reaches the matching `threshold` value times the profile multiplier. It acts on a feed match or a stratum match for every profile. See [Central profiles](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#profiles) and [Central exceptions](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#exceptions).

Outbound to public destinations:

| Rule | Finding code | Trigger | Reads |
|---|---|---|---|
| UDP flood | `DoS:VM/OutboundFlood` | UDP above 20k pps for 2 min, and received below 10% of sent or fewer than 8 destinations | `vm_abuse_transmit_packets_total` and `vm_abuse_receive_packets_total` with `protocol="udp"`, `vm_abuse_destinations{port_class="all",window="60s"}` |
| Small-packet flood | `DoS:VM/OutboundFlood` | Above 20k pps for 2 min, average packet below 200 bytes | `vm_abuse_transmit_packets_total`, `vm_abuse_transmit_bytes_total` |
| Volumetric | `DoS:VM/OutboundFlood` | Above 1 Gbit/s for 1 min, received below 5% of sent | `vm_abuse_transmit_bytes_total`, `vm_abuse_receive_bytes_total` |
| ICMP flood | `DoS:VM/OutboundFlood` | Above 1k pps for 2 min | `vm_abuse_transmit_packets_total{protocol="icmp"}` |
| SYN flood | `DoS:VM/SynFlood` | Initiated SYN above 2k/s for 2 min, answered below 10% | `vm_abuse_tcp_initiated_total`, `vm_abuse_tcp_answered_total` |
| Horizontal scan | `Recon:VM/Portscan` | 256 or more addresses in one port class in 1 min, below 25% answered | `vm_abuse_destinations{window="60s"}` for each port class, `vm_abuse_tcp_initiated_total`, `vm_abuse_tcp_answered_total` |
| Vertical scan | `Recon:VM/Portscan` | 512 or more ports on one address in 1 min, below 25% answered | `vm_abuse_ports_per_destination_max`, `vm_abuse_tcp_initiated_total`, `vm_abuse_tcp_answered_total` |
| Slow scan | `Recon:VM/Portscan` | 2,000 or more unanswered address and port pairs in 1 h. Low severity. | `vm_abuse_unanswered_pairs` |
| Brute force | `UnauthorizedAccess:VM/BruteForce` | The `auth` class to 64 or more addresses in 10 min, or 300 or more new flows to one address in 10 min | `vm_abuse_destinations{port_class="auth",window="10m"}`, `vm_abuse_flows_per_destination_max{port_class="auth"}` |
| Reflector | `Reflector:VM/<protocol>` | Amplification source port, bytes out above 10 times bytes in, above 10 Mbit/s for 1 min | `vm_abuse_amplification_bytes_total`, `transmit` against `receive` for each `port`. The `port` gives the protocol, for example 53 is DNS. |

SMTP:

| Rule | Trigger | Result | Reads |
|---|---|---|---|
| Port 25 misconfiguration | 1 to 499 blocked attempts in 10 min, to fewer than 50 destinations | `Smtp:VM/BlockedPort25`, Low | `vm_abuse_smtp_blocked_total`, `vm_abuse_destinations{port_class="smtp",window="10m"}` |
| Port 25 spam | 500 or more blocked attempts in 10 min, or 50 or more destinations | `Spam:VM/Smtp`, Medium | `vm_abuse_smtp_blocked_total`, `vm_abuse_destinations{port_class="smtp",window="10m"}` |
| Submission spray | The `submission` class to 20 or more addresses, or 300 or more new flows to one address, in 10 min | `Spam:VM/Smtp`, Medium | `vm_abuse_destinations{port_class="submission",window="10m"}`, `vm_abuse_flows_per_destination_max{port_class="submission"}` |
| DNSBL listing | A listing of a shared host address from the `nat-ranges` log, or of a dedicated public address of a VM | `Spam:VM/Smtp` from the feed. See [Feeds](#feeds). | DNSBL feeds, `nat-ranges` log, `vm_abuse_public_address` |

Other rules:

| Rule | Trigger | Reads |
|---|---|---|
| Mining pool, feed | 1 match of a pool address in the flow log, or of a pool name in the SNI log | Flow log, SNI log |
| Mining pool, stratum | 1 or more stratum flows in 10 min | `vm_abuse_stratum_flows_total` |
| C2 | 1 flow to abuse.ch Feodo, or 3 flows in 1 h to Spamhaus DROP | Flow log |
| Inbound flood | Above 20k pps or 1 Gbit/s received for 2 min. `DoS:VM/InboundFlood`. Central decides the protection. | `vm_abuse_receive_packets_total`, `vm_abuse_receive_bytes_total` |

The HTTP proxy has static limits for each client address and site: `limit_req` 100 requests a second with a burst of 500, and `limit_conn` 200. Watcher does not change them.

## Feeds

An [Abuse Feed](#abuse-feed) holds one external list. Watcher fetches each enabled feed on its own schedule. A feed has no default URL. A feed match is a finding. A feed is not an abuse report, so nobody sends a reply.

| Feed | Use |
|---|---|
| Mining pools | Pool addresses and names |
| Spamhaus DROP, abuse.ch Feodo | C2 addresses |
| Tor consensus | Tor relay addresses |
| Safe Browsing, OpenPhish, URLhaus | Phishing and malware hostnames |
| DNSBLs: Spamhaus ZEN, Barracuda, and others | Listings of our public addresses |
| Microsoft SNDS | Spam trap hits and complaints for our address ranges |
| Shadowserver | Reports for the region's prefixes, through its API |

Watcher matches feeds in these ways:

- Logs. Each Metal host uploads one `flow` object and one `sni` object every 5 min. Watcher reads each new flow and SNI object once. It matches the remote addresses of outbound flow rows and the server names of SNI rows against the feeds. Inbound flow rows have no remote address. Objects are visible only after their upload, so the newest 5 min are not visible yet. See [object layout](abuse-logs.md#object-layout).
- Hostname checks. Watcher checks the distinct hostnames in the proxy access log (sites and custom domains) against the phishing feeds, when a hostname first shows in the log and every day after. A match is `Phishing:Site/Reported`.
- Public buckets. Watcher checks the hostname of each public bucket against the phishing feeds, when the bucket becomes public and every day after. A match is `Phishing:Bucket/Reported`. This check applies only if Cargo offers public buckets. See [public buckets](abuse-logs.md#public-buckets).
- Address checks. Every hour, Watcher checks two sets of addresses against the DNSBLs, Microsoft SNDS and Shadowserver: the dedicated public addresses of each VM in `vm_abuse_public_address`, and the shared host addresses in the `nat-ranges` log.

The flow log covers every VM, with shared NAT or a dedicated public address, because Metal records it at the VM tap. It holds public traffic only. Each row names the VM. See [Atlas flow log](https://github.com/frappe/atlas/blob/develop/docs/abuse/metrics.md#flow-log).

Limit: Metal creates at most 10,000 flow entries for each VM, so it writes at most 10,000 flow rows for each VM in each minute. At the cap, Metal creates no entry for a new flow of that VM. A feed match can miss such a flow. The cap of one VM does not push out the entries of other VMs.

For each VM at the cap in a minute, Metal writes one summary row. The summary row has direction `overflow` and no addresses or ports, so Watcher does not match it against the feeds. Its `Dropped` field holds the count of new flows with no entry in the minute. `vm_abuse_flow_rows_dropped_total` also counts them.

Watcher sends a match to Central as a finding. It does not suspend a site, a bucket or a VM. Central owns the case and the decision.

A finding from a feed with `is_trusted` has `is_trusted_source`. See [Abuse Feed](#abuse-feed).

A feed match from the flow log has `virtual_machine`. A dedicated address belongs to one VM. A finding for a dedicated address has `address` and `virtual_machine`. A shared host address is shared by the VMs on the host. A finding for a shared host address has `address` only. Central decides what to do with each finding.

## Log integrity

A Garage write key can replace or delete any object in the log bucket. See [keys](abuse-logs.md#keys). Watcher checks that the log objects stay as the writers uploaded them.

- A Metal host writes `flow`, `sni` and `nat-ranges`. A proxy node writes `proxy-access`. See [object layout](abuse-logs.md#object-layout).
- Expected cadence: one object every 5 min for `flow`, `sni` and `proxy-access`. Metal writes `nat-ranges` when a range changes, and the full table once a day, so the expected cadence of `nat-ranges` is daily.
- Watcher records the ETag of each log object when it reads it. Watcher keeps this record in the cache for 7 days.
- A writer is expected from its first object until it has had no object for 24 h.
- A `flow`, `sni` or `proxy-access` object is missing when its window ended more than 15 min ago and the bucket has no object for it.
- The `nat-ranges` log of a Metal host is missing when the host has no `nat-ranges` object in the last 24 h and 15 min.
- Watcher does not report missing objects while the log bucket does not answer.
- Every hour, Watcher lists the objects of the last 7 days again. An object with a different ETag is changed. An object that Watcher recorded and the list does not show is deleted.
- If the cache is empty, Watcher records the ETags again and reports no change for those objects.

Each missing, changed or deleted object gives a finding:

| Field | Value |
|---|---|
| `finding_code` | `Integrity:Log/Missing` |
| `severity` | High |
| `virtual_machine` | Empty. The finding has no VM. |
| `evidence` | `writer`, `objects` and `change` |
| `measured`, `threshold` | Empty |

- This finding has no Abuse Threshold, so it is never Shadow.
- Central takes no customer action on it. Central posts it to `#abuse`.
- Missing objects of one writer that follow each other update the same `finding_id`, as for other findings.
- Watcher checks only the last 7 days. It does not find a change to an older object.

### Mining

The AUP forbids mining on every server. CPU use alone is never evidence. Watcher finds mining from:

- `vm_abuse_stratum_flows_total`. Metal reads only the first payload bytes of new flows to non-standard ports for this match.
- Matches of pool addresses in the flow log, and of pool names in the SNI log, against the mining pool feed.

Central decides the outcome for each team.

## Findings

A finding is one match of one finding code on one subject in one window. The subject is a VM, a bucket, a hostname, an address or a log writer.

Watcher sends findings in a report with `service` = `abuse`. Central owns the inbound shape. See [Central reports from Cargo](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#reports-from-cargo).

Delivery uses the Cargo Settings fields that Central sets with `configure` ([Central enrolling Cargo](central-contract.md#central-enrolling-cargo)):

- Watcher posts the report to `central_webhook_url`. This is `central.api.state_delivery.receive`.
- Watcher signs the report with `central_webhook_secret`.
- While `central_webhook_enabled` is off, Watcher sends nothing. It keeps its open findings and sends them in the first report after `central_webhook_enabled` is on again.

The report has these fields in addition to `region`, `region_id` and `service`:

| Field | Value |
|---|---|
| `checked_until` | An object with one timestamp for each input: `metrics`, `logs` and `feeds`. `metrics` and `logs` are the end of the last window that Watcher checked from that input. `feeds` is the end of the last hourly address check. |
| `findings` | The new and changed findings of the run. Empty when no finding is new or changed. |

Each finding has these fields:

| Field | Value |
|---|---|
| `finding_id` | The Watcher ID of the finding. Central matches repeats on it. |
| `finding_code` | A code from [Finding codes](#finding-codes) |
| `severity` | Low, Medium, High or Critical, from the trigger row that matched |
| `virtual_machine` | Optional. The Atlas VM ID, for example `vm-0a1b2c3d4e`, when known. |
| `bucket` | Optional. The bucket name, when the subject is a bucket. |
| `hostname` | Optional. The hostname, when the subject is a hostname. |
| `address` | Optional. The public address, when the subject is an address. |
| `window_start` | The start of the window |
| `window_end` | The end of the window |
| `measured` | The measured value of each volume trigger. Empty for a feed match or a stratum match. |
| `threshold` | The standard threshold of each volume trigger. Empty for a feed match or a stratum match. |
| `evidence` | An object. See the table below. No payloads. |
| `is_shadow` | Set when the Abuse Threshold `mode` is Shadow |
| `is_trusted_source` | Set when the finding comes from a feed with `is_trusted` |

Each field of `evidence` is optional:

| Field | Value |
|---|---|
| `port_class` | The port class of the traffic, for example `auth`. Central needs `port_class` or `ports` to block a port. |
| `ports` | A list of destination port numbers |
| `protocol` | The protocol, for example `udp` |
| `counts` | An object of counts, for example the number of destinations |
| `ratios` | An object of measured ratios, for example `answered_percent` |
| `prefixes` | Destination prefixes, truncated to /24 for IPv4 and /48 for IPv6 |
| `feed` | The name of the Abuse Feed that matched |
| `hostname_match` | The feed entry that matched a hostname |
| `writer` | The Metal host name or the proxy node name, for `Integrity:Log/Missing` |
| `objects` | The object keys, for `Integrity:Log/Missing` |
| `change` | `missing`, `changed` or `deleted`, for `Integrity:Log/Missing` |

```json
{
	"region": "bom",
	"region_id": 3,
	"service": "abuse",
	"checked_until": {
		"metrics": "2026-11-02T10:03:00Z",
		"logs": "2026-11-02T10:00:00Z",
		"feeds": "2026-11-02T10:00:00Z"
	},
	"findings": [
		{
			"finding_id": "fnd-7f3a",
			"finding_code": "Recon:VM/Portscan",
			"severity": "Medium",
			"virtual_machine": "vm-0a1b2c3d4e",
			"window_start": "2026-11-02T10:02:00Z",
			"window_end": "2026-11-02T10:03:00Z",
			"measured": {"destinations": 4312},
			"threshold": {"destinations": 256},
			"evidence": {"port_class": "auth", "ports": [22], "ratios": {"answered_percent": 3}, "prefixes": ["203.0.113.0/24"]},
			"is_shadow": false,
			"is_trusted_source": false
		}
	]
}
```

- A match that continues an open finding of the same subject and finding code updates the same `finding_id` with a later `window_end`. A gap of more than one window starts a new finding.
- Watcher sends the report after every run, every minute, also when no finding is new or changed. Central alerts when a region sends no `abuse` report for 10 min, when `checked_until.metrics` or `checked_until.logs` is more than 15 min old, or when `checked_until.feeds` is more than 2 h old. See [Central reports from Cargo](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#reports-from-cargo).
- The report is signed in the same way as the other Cargo reports: an HMAC-SHA256 of the body with `central_webhook_secret`, in `X-Frappe-Webhook-Signature`, with the headers `X-FC-Source: cargo` and `X-FC-Region`. See [Central Cargo reports](https://github.com/frappe/central/blob/develop/spec/WEBHOOKS.md#cargo-reports).
- If the delivery fails, Watcher sends the findings again on the next run. A repeat is safe, because Central matches it by `finding_id`.
- Watcher sends findings for every VM, also for tenant 0 VMs. Central skips VMs that it does not know.

## Attribution

Central attributes a shared host address in an abuse report with a Cargo call:

```
POST {cargo_url}/api/method/cargo.watcher.api.attribute_address
X-Cargo-Access-Token: <a Central token for atlas-cargo:<region-id> with scope abuse:attribute>

{"address": "198.51.100.7", "port": 40312, "time": "2026-11-02T10:05:31Z"}
```

| Field | Value |
|---|---|
| `address` | The public source address in the report |
| `port` | The public source port in the report |
| `time` | The time in the report, in UTC |

Cargo reads the `nat-ranges` log of the Metal host that has `address`: the last full table before `time`, and the change rows from that table up to `time`. It finds the range on `address` that holds `port` at `time`, and returns the VM of that range. The flow entry cap does not affect attribution.

To find the Metal host that has `address`, Cargo reads the latest full table of every Metal host in the region. A region has few hosts. Cargo can keep the address-to-host result in the cache for 1 h.

Metal writes `nat-ranges` rows as follows:

- `assigned` when Metal sets up the VM network on the host: VM create, VM start and migration in.
- `freed` when Metal removes the VM network from the host: VM delete, VM stop and migration out.
- A migrated VM gets a new range on its new host.

The flow log holds the guest address and the guest port, before NAT. The public port of a report is only in the `nat-ranges` log. Cargo cannot join flow rows to a report by port. Thus attribution uses the `nat-ranges` log only.

```json
{"virtual_machine": "vm-0a1b2c3d4e"}
```

- `virtual_machine` is the Atlas VM ID, or `null` when no range matches.
- Cargo refuses a `time` newer than the last uploaded window of that host, because the log does not hold it yet. Central can ask again later for a new time.
- Cargo refuses a `time` older than 179 days. The log keeps 180 days, and the full table before `time` can be up to 1 day older than `time`.
- The call uses the same token checks as the bucket calls. See [Central calling Cargo](central-contract.md#central-calling-cargo).
- Then Cargo accepts the call only when Central signed the token (`iss` is `central`) and its `scope` is `abuse:attribute`. A token from Atlas, or a token with another scope, gets `403`.
- Central mints the token in the same way as its `atlas-cargo:<region-id>` token for bucket calls, with the scope `abuse:attribute`.
- The answer holds no other log row.

A dedicated address belongs to one VM and is not in the `nat-ranges` log. A VM with a dedicated public IPv4 address gets no NAT slot and no `nat-ranges` rows. Central attributes a dedicated address from its own Public IP history.

IPv6 does not use the shared IPv4 address or a range. Central attributes an IPv6 address from its Public IP history. In the flow log, the `Local address` of an IPv6 row is the address of the VM.

`attribute_address` ships in Phase 2, before Watcher detection. See [deploy order](abuse-logs.md#deploy-order).

## Calibration

1. Baseline. Phase 1 collects the per-VM metrics and the flow log before Watcher ships. See [deploy order](abuse-logs.md#deploy-order). Until per-VM metrics are in Datum, an operator runs the Atlas script [atlas-abuse-baseline.py](https://github.com/frappe/atlas/blob/develop/scripts/atlas-abuse-baseline.py) on every Metal host from 2 to 7 November 2026, so that the samples include the October month end. Agents do not run it. Its data includes mesh traffic and counts a SYN-ACK as a SYN, so it gives upper bounds only.
2. Shadow. Every Abuse Threshold `mode` is Shadow for at least 4 weeks, with a month end. Findings have `is_shadow`. Central takes no action on them.
3. Red team in staging, from a test tenant. Each row must give a finding in time:

   | Tool | Rule | Finding within |
   |---|---|---|
   | masscan at 100, 1k, and 10k pps | Horizontal scan | 3 min |
   | nmap `-T3` and `-T4`, top 1000 ports | Vertical scan | 3 min |
   | hping3 `--flood`, SYN and UDP | SYN flood, UDP flood | 4 min |
   | hydra against 100 SSH hosts in a test range | Brute force | 12 min |
   | iperf3 UDP at 2 Gbit/s to a silent sink | Volumetric | 3 min |
   | xmrig to a test stratum server | Mining, stratum | 12 min |
   | swaks loop to 100 MX hosts on port 25 | Port 25 spam | 12 min |

   These must stay silent: a bench build (yarn, pip, git), `apt upgrade`, a 50 GB backup to S3, a month-end run of 10k emails, a 500 Mbit/s WireGuard VPN, and a crawler on 1k sites.
4. Active. Switch one finding code at a time, when false positives are below 1 for each 1,000 VM-days and every red-team row gives a finding.

After that, responders in Central mark disputed signals as false positives. See [Central signals](https://github.com/frappe/central/blob/develop/spec/ABUSE.md#signals). Operators tune each region in its Abuse Thresholds.

## Layouts

Status: awaiting engineer approval. Do not write DocType JSON before an engineer approves these layouts.

Notation:

- `[Section]` is a section.
- Each field shows the label, the fieldname and the type.
- RO means read only.

### Abuse Threshold

Not team-scoped. Only `System Manager` reads and writes it. One record for each finding code in the region. Naming: by `finding_code`. `track_changes` on.

```text
┌ Abuse Threshold Recon:VM/Portscan ────────────────────────────────────────────────────────┐
│ [Threshold]                                                                               │
│ Finding Code      finding_code      Data             │ Mode          mode       Select    │
│ Window (Minutes)  window_minutes    Int (1 or more)  │   Off / Shadow / Active            │
│ [Triggers]                                                                                │
│ Triggers          triggers          Table  Abuse Threshold Trigger                        │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

| Child | Fields |
|---|---|
| Abuse Threshold Trigger | `rule` Data (for example `udp_flood`), `severity` Select (Low, Medium, High, Critical), `parameter` Data (for example `transmit_packets_per_second`), `comparison` Select (Above, Below), `value` Float, `unit` Data, `is_volume` Check, `window_minutes` Int (empty uses the parent window) |

- All triggers of one rule must match together. Rules of one finding code match separately. See [Thresholds](#thresholds).
- A finding takes its `severity` from the trigger row that matched. All rows of one rule have the same `severity`. One finding code can have rules of different severity. For example, the slow scan rows of `Recon:VM/Portscan` are Low, and its other rows are Medium.
- `value` is the standard level. Watcher reports at 0.5 times `value` for a trigger with `is_volume` set. See [Thresholds](#thresholds).
- Only triggers with `is_volume` go into `measured` and `threshold` of a finding. Watcher puts the measured ratios of the other triggers in `evidence`. See [Findings](#findings).
- `mode` is the calibration switch. Shadow sends findings with `is_shadow`. See [Calibration](#calibration).
- A window is at least 1 min. The controller refuses a shorter one.
- The starting values ship as fixtures, not as JSON defaults, so a region can use other values.

### Abuse Feed

Not team-scoped. Only `System Manager` reads and writes it. Naming: by `feed_name`.

```text
┌ Abuse Feed OpenPhish ─────────────────────────────────────────────────────────────────────┐
│ [Feed]                                                                                    │
│ Feed Name         feed_name         Data             │ Enabled       is_enabled  Check    │
│ Kind              kind              Select           │ Trusted       is_trusted  Check    │
│   Mining Pools / Spamhaus DROP / abuse.ch Feodo /    │ Refresh (Minutes)  refresh_minutes │
│   Tor / Safe Browsing / OpenPhish / URLhaus /        │                    Int             │
│   DNSBL / SNDS / Shadowserver                        │                                    │
│ URL               url               Data             │ Credential    credential Password  │
│ [Status]                                                                                  │
│ Last Fetched At   last_fetched_at   Datetime RO      │ Entries       entry_count  Int RO  │
│ Last Error        last_error        Small Text RO                                         │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

- A feed applies to this region only. Each region's Cargo has its own feeds.
- `url` has no default.
- `is_trusted` marks a feed that Central can act on with no operator check. Each finding from the feed has `is_trusted_source`. See [Findings](#findings).
- Watcher keeps the fetched entries in the cache, not in a DocType.
- A fetch failure sets `last_error` and keeps the last good entries. See [Feeds](#feeds).
