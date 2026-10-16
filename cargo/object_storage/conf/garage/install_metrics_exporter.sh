#!/bin/bash
# Runs over SSH on one of a cluster's machines. Arguments come from the environment.
# Idempotent: run again with a new DATUM_TOKEN to rotate it.
set -euo pipefail

: "${VECTOR_VERSION:?VECTOR_VERSION is required}"
: "${DATUM_ENDPOINT:?DATUM_ENDPOINT is required}"
: "${DATUM_TOKEN:?DATUM_TOKEN is required}"
: "${CLUSTER:?CLUSTER is required}"
: "${REGION:?REGION is required}"
: "${ROLE:?ROLE is required}"
: "${MACHINE:?MACHINE is required}"
VECTOR_BINARY=/usr/local/bin/vector

if ! "$VECTOR_BINARY" --version 2>/dev/null | grep -q "vector $VECTOR_VERSION "; then
	target="$(uname -m)-unknown-linux-musl"
	workdir="$(mktemp -d)"
	trap 'rm -rf "$workdir"' EXIT
	curl -fsSL "https://packages.timber.io/vector/$VECTOR_VERSION/vector-$VECTOR_VERSION-$target.tar.gz" \
		| tar -xz -C "$workdir"
	install -m 755 "$workdir/vector-$target/bin/vector" "$VECTOR_BINARY"
fi

install -d -m 755 /etc/vector

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
.tags.cluster = "$CLUSTER"
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
chmod 644 /etc/vector/vector.toml

(set -a && . /etc/vector/datum.env && "$VECTOR_BINARY" validate --no-environment /etc/vector/vector.toml)

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
