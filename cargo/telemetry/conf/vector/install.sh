#!/bin/bash
# Runs over SSH on a machine Cargo rented: a Garage node or the Datum host. Arguments come
# from the environment; CLUSTER is left out on a machine no cluster owns.
# Idempotent: run again with a new DATUM_TOKEN to rotate it.
set -euo pipefail

: "${VECTOR_VERSION:?VECTOR_VERSION is required}"
: "${DATUM_ENDPOINT:?DATUM_ENDPOINT is required}"
: "${DATUM_TOKEN:?DATUM_TOKEN is required}"
: "${REGION:?REGION is required}"
: "${ROLE:?ROLE is required}"
: "${MACHINE:?MACHINE is required}"
VECTOR_BINARY=/usr/local/bin/vector
CLUSTER_TAG=""
CLUSTER_ATTRIBUTE=""
if [ -n "${CLUSTER:-}" ]; then
	CLUSTER_TAG=".tags.cluster = \"$CLUSTER\""
	CLUSTER_ATTRIBUTE=".attributes.cluster = \"$CLUSTER\""
fi
# The log pipelines this role ships, filled in below. The Datum host ships none yet.
LOG_INPUTS=""

if ! "$VECTOR_BINARY" --version 2>/dev/null | grep -q "vector $VECTOR_VERSION "; then
	target="$(uname -m)-unknown-linux-musl"
	workdir="$(mktemp -d)"
	trap 'rm -rf "$workdir"' EXIT
	curl -fsSL "https://packages.timber.io/vector/$VECTOR_VERSION/vector-$VECTOR_VERSION-$target.tar.gz" \
		| tar -xz -C "$workdir"
	install -m 755 "$workdir/vector-$target/bin/vector" "$VECTOR_BINARY"
fi

install -d -m 755 /etc/vector
# Validation checks it exists; systemd moves it under /var/lib/private on first start.
install -d -m 755 /var/lib/vector

cat > /etc/vector/datum.env <<DATUM_ENV
DATUM_ENDPOINT=${DATUM_ENDPOINT%/}
DATUM_TOKEN=$DATUM_TOKEN
DATUM_ENV
chmod 600 /etc/vector/datum.env

cat > /etc/vector/vector.toml <<VECTOR_TOML
data_dir = "/var/lib/vector"

[sources.host]
type = "host_metrics"
collectors = ["cpu", "memory", "load", "network", "disk", "filesystem"]
scrape_interval_secs = 15
namespace = "host"
network.devices.excludes = ["lo", "veth*", "docker*"]
disk.devices.excludes = ["loop*", "ram*"]
filesystem.filesystems.excludes = ["tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup*", "fuse*"]

[transforms.labelled]
type = "remap"
inputs = ["host"]
source = '''
$CLUSTER_TAG
.tags.region = "$REGION"
.tags.role = "$ROLE"
.tags.machine = "$MACHINE"
'''

# Datum allows 12 writes a minute per token and route, shared by every node of the region.
[sinks.datum]
type = "prometheus_remote_write"
inputs = ["labelled"]
endpoint = "\${DATUM_ENDPOINT}/v1/ingest/remote"
auth.strategy = "bearer"
auth.token = "\${DATUM_TOKEN}"
batch.timeout_secs = 60
batch.max_events = 5000
healthcheck.enabled = false
buffer.when_full = "drop_newest"
VECTOR_TOML

# Garage logs to journald with its own priority and module, rather than coloured text.
# The gateway's request lines are kept as its access log. Per-block, per-item and
# per-worker chatter is not, nor Cargo's own once-a-minute health probes.
if [ "$ROLE" = "gateway" ] || [ "$ROLE" = "storage" ]; then
	LOG_INPUTS='"garage_lines"'
	cat >> /etc/vector/vector.toml <<GARAGE_LOGS

[sources.garage_journal]
type = "journald"
include_units = ["garage.service"]
since_now = true

[transforms.garage_lines]
type = "remap"
inputs = ["garage_journal"]
drop_on_abort = true
drop_on_error = true
source = '''
module = if .TARGET == "log" { .F_LOG_TARGET } else { .TARGET }
module = replace(string(module) ?? "garage", "::", ".")

priority = to_int(.PRIORITY) ?? 6
level = "debug"
if priority <= 3 {
	level = "error"
} else if priority == 4 {
	level = "warn"
} else if priority <= 6 {
	level = "info"
}

chatter = ["garage_block.resync", "garage_table.sync", "garage_util.background.worker"]
if level == "info" && includes(chatter, module) {
	abort
}

# On every line, not only request lines: until its unit is rewritten, a node's Garage
# still logs coloured text that carries no module.
message = strip_ansi_escape_codes(string(.message) ?? "")
if contains(message, "/v2/GetClusterHealth") || contains(message, "/v2/GetClusterStatus") {
	abort
}

. = {
	"ts": .timestamp,
	"product": "cargo",
	"service": "garage",
	"source": module,
	"level": level,
	"message": message,
}
'''
GARAGE_LOGS
fi

# Only failed requests: the rest are one line each, and the metrics already count them.
if [ "$ROLE" = "gateway" ]; then
	LOG_INPUTS="$LOG_INPUTS, \"nginx_access_lines\", \"nginx_error_lines\""
	cat >> /etc/vector/vector.toml <<NGINX_LOGS

[sources.nginx_access]
type = "file"
include = ["/var/log/nginx/access.log"]
read_from = "end"

[sources.nginx_error]
type = "file"
include = ["/var/log/nginx/error.log"]
read_from = "end"

[transforms.nginx_access_lines]
type = "remap"
inputs = ["nginx_access"]
drop_on_abort = true
drop_on_error = true
source = '''
request, error = parse_nginx_log(.message, "combined")
if error != null {
	abort
}

status = to_int(request.status)
if status < 499 {
	abort
}

level = "error"
if status == 499 {
	level = "warn"
}

. = {
	"ts": request.timestamp || .timestamp,
	"product": "cargo",
	"service": "nginx",
	"source": "nginx.access",
	"level": level,
	"message": .message,
}
'''

[transforms.nginx_error_lines]
type = "remap"
inputs = ["nginx_error"]
drop_on_error = true
source = '''
. = {
	"ts": .timestamp,
	"product": "cargo",
	"service": "nginx",
	"source": "nginx.error",
	"level": "error",
	"message": .message,
}
'''
NGINX_LOGS
fi

if [ -n "$LOG_INPUTS" ]; then
	cat >> /etc/vector/vector.toml <<DATUM_LOGS

[transforms.log_lines]
type = "remap"
inputs = [$LOG_INPUTS]
drop_on_error = true
source = '''
# Every source can carry a presigned URL, which works for whoever holds it until it expires.
message = replace(string(.message) ?? "", r'X-Amz-Signature=[^&\s"]+', "X-Amz-Signature=redacted")
.message = truncate(message, 8192)
.attributes = {
	"region": "$REGION",
	"role": "$ROLE",
	"machine": "$MACHINE",
}
$CLUSTER_ATTRIBUTE
'''

# The same request budget as metrics, on its own route: one write a minute from each node,
# and a refused write is dropped after two retries rather than queued behind new lines.
[sinks.datum_logs]
type = "http"
inputs = ["log_lines"]
uri = "\${DATUM_ENDPOINT}/v1/logs/ingest"
method = "post"
encoding.codec = "json"
auth.strategy = "bearer"
auth.token = "\${DATUM_TOKEN}"
batch.timeout_secs = 60
batch.max_events = 5000
request.rate_limit_num = 1
request.rate_limit_duration_secs = 60
request.retry_attempts = 2
healthcheck.enabled = false
buffer.max_events = 10000
buffer.when_full = "drop_newest"
DATUM_LOGS
fi
chmod 644 /etc/vector/vector.toml

# With the environment checks on, so the VRL programs compile here rather than in a
# restart loop. The sinks' health checks are off, so Datum is not called.
(set -a && . /etc/vector/datum.env && "$VECTOR_BINARY" validate /etc/vector/vector.toml)

cat > /etc/systemd/system/vector.service <<VECTOR_UNIT
[Unit]
Description=Machine metrics to Datum
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/vector/datum.env
ExecStart=$VECTOR_BINARY --config /etc/vector/vector.toml
Restart=on-failure
RestartSec=5
DynamicUser=true
# Reading the journal and nginx's logs.
SupplementaryGroups=systemd-journal adm
# A Datum that refuses every write must not fill syslog with Vector's complaints.
LogRateLimitIntervalSec=30s
LogRateLimitBurst=200
StateDirectory=vector
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
VECTOR_UNIT

systemctl daemon-reload
systemctl enable vector
systemctl restart vector

for _ in $(seq 1 15); do
	if systemctl is-active --quiet vector; then
		exit 0
	fi
	sleep 2
done

echo "vector did not come up within 30s" >&2
systemctl status vector --no-pager --lines 30 >&2
exit 1
