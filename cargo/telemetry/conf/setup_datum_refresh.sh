#!/bin/bash
# Runs over SSH before the Datum agent install: a daily timer trades this node's machine token, from its
# Atlas metadata, for its own Datum token from Central. Idempotent; runs one refresh now.
# Cargo names the endpoint: Central only learns it once the region's Datum host is Active.
set -euo pipefail

: "${CENTRAL_URL:?CENTRAL_URL is required}"
: "${DATUM_ENDPOINT:?DATUM_ENDPOINT is required}"

cat > /usr/local/bin/datum-refresh <<'DATUM_REFRESH'
#!/bin/bash
set -euo pipefail

METADATA=http://169.254.169.254/latest
ENV_FILE=/etc/vector/datum.env

install -d -m 755 /etc/vector
[ -f "$ENV_FILE" ] || install -m 600 /dev/null "$ENV_FILE"

session=$(curl -fsS --max-time 5 -X PUT -H "X-metadata-token-ttl-seconds: 300" "$METADATA/api/token")
# Atlas may accept a metadata change before Metal applies it, so a fresh token can lag a little.
machine_token=""
for _ in $(seq 1 30); do
	if machine_token=$(curl -fsS --max-time 5 -H "X-metadata-token: $session" \
		"$METADATA/meta-data/attributes/datum_telemetry_token"); then
		break
	fi
	sleep 2
done
: "${machine_token:?no datum_telemetry_token in this machine metadata}"

response=$(curl -fsS --max-time 30 -H "Accept: application/json" \
	-H "X-Cargo-Machine-Token: $machine_token" \
	"$CENTRAL_URL/api/method/central.api.cargo.datum_token")

token=$(python3 -c 'import json, sys; print(json.load(sys.stdin)["message"]["token"])' <<<"$response")

next=$(mktemp "$ENV_FILE.XXXXXX")
{
	echo "DATUM_ENDPOINT=${DATUM_ENDPOINT%/}"
	echo "DATUM_TOKEN=$token"
	grep -v -e '^DATUM_ENDPOINT=' -e '^DATUM_TOKEN=' "$ENV_FILE" || true
} > "$next"

if cmp -s "$next" "$ENV_FILE"; then
	rm -f "$next"
	exit 0
fi

mv "$next" "$ENV_FILE"
# Before the Datum agent is installed there is no Vector to restart yet.
if systemctl is-active --quiet vector; then
	systemctl restart vector
fi
DATUM_REFRESH
chmod 755 /usr/local/bin/datum-refresh

cat > /etc/systemd/system/datum-refresh.service <<DATUM_SERVICE
[Unit]
Description=Fetch this node's Datum token from Central
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=CENTRAL_URL=${CENTRAL_URL%/}
Environment=DATUM_ENDPOINT=${DATUM_ENDPOINT%/}
ExecStart=/usr/local/bin/datum-refresh
DATUM_SERVICE

cat > /etc/systemd/system/datum-refresh.timer <<'DATUM_TIMER'
[Unit]
Description=Fetch this node's Datum token from Central every day

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
DATUM_TIMER

systemctl daemon-reload
systemctl enable --now datum-refresh.timer
systemctl start datum-refresh.service
