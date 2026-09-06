#!/bin/bash
# Runs over SSH on one of a cluster's machines. Arguments come from the environment.
set -euo pipefail

: "${GARAGE_BINARY:?GARAGE_BINARY is required}"
: "${GARAGE_VERSION:?GARAGE_VERSION is required}"
: "${BINARY_URL:?BINARY_URL is required}"
: "${METADATA_DIR:?METADATA_DIR is required}"
: "${DATA_DIR:?DATA_DIR is required}"
: "${RPC_PUBLIC_ADDR:?RPC_PUBLIC_ADDR is required}"
: "${RPC_SECRET:?RPC_SECRET is required}"
: "${ADMIN_TOKEN:?ADMIN_TOKEN is required}"
: "${METRICS_TOKEN:?METRICS_TOKEN is required}"
: "${REGION:?REGION is required}"
: "${BASE_DOMAIN:?BASE_DOMAIN is required}"
: "${REPLICATION_FACTOR:?REPLICATION_FACTOR is required}"
: "${RPC_PORT:?RPC_PORT is required}"
: "${S3_PORT:?S3_PORT is required}"
: "${WEB_PORT:?WEB_PORT is required}"
: "${K2V_PORT:?K2V_PORT is required}"
: "${ADMIN_PORT:?ADMIN_PORT is required}"
GARAGE_CONFIG="${GARAGE_CONFIG:-/etc/garage.toml}"

install -d -m 700 "$METADATA_DIR" "$DATA_DIR"

if ! "$GARAGE_BINARY" --version 2>/dev/null | grep -q "$GARAGE_VERSION"; then
	curl -fsSL -o /tmp/garage "$BINARY_URL"
	install -m 755 /tmp/garage "$GARAGE_BINARY"
	rm -f /tmp/garage
fi

# A binary built for another architecture installs fine, then fails to exec.
if ! installed="$("$GARAGE_BINARY" --version 2>&1)"; then
	echo "$GARAGE_BINARY does not run on $(uname -m): $installed" >&2
	echo "it came from $BINARY_URL -- check the cluster's Garage Arch" >&2
	exit 1
fi

cat > "$GARAGE_CONFIG" <<GARAGE_TOML
metadata_dir = "$METADATA_DIR"
data_dir     = "$DATA_DIR"
db_engine    = "lmdb"

replication_factor = $REPLICATION_FACTOR
consistency_mode   = "consistent"

rpc_bind_addr   = "[::]:$RPC_PORT"
rpc_public_addr = "$RPC_PUBLIC_ADDR:$RPC_PORT"
rpc_secret      = "$RPC_SECRET"

# Left empty: nodes are peered over the admin API, and set_peers.sh fills this in for the
# next boot. The brackets are the anchor it looks for.
bootstrap_peers = [
]

[s3_api]
s3_region     = "$REGION"
api_bind_addr = "[::]:$S3_PORT"
root_domain   = ".s3.$REGION.$BASE_DOMAIN"

[s3_web]
bind_addr   = "[::]:$WEB_PORT"
root_domain = ".web.$REGION.$BASE_DOMAIN"
index       = "index.html"

[k2v_api]
api_bind_addr = "[::]:$K2V_PORT"

[admin]
api_bind_addr = "[::]:$ADMIN_PORT"
admin_token   = "$ADMIN_TOKEN"
metrics_token = "$METRICS_TOKEN"
GARAGE_TOML
chmod 600 "$GARAGE_CONFIG"

cat > /etc/systemd/system/garage.service <<GARAGE_UNIT
[Unit]
Description=Object Storage Garage Service
After=network-online.target
Wants=network-online.target

[Service]
Environment="RUST_LOG=garage=info" "RUST_BACKTRACE=1"
ExecStart=$GARAGE_BINARY server
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
ProtectHome=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
GARAGE_UNIT

systemctl daemon-reload
systemctl enable garage
systemctl restart garage

for _ in $(seq 1 30); do
	if "$GARAGE_BINARY" status > /dev/null 2>&1; then
		exit 0
	fi
	sleep 2
done

echo "garage did not come up within 60s" >&2
systemctl status garage --no-pager --lines 30 >&2
exit 1
