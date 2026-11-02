#!/bin/bash
# Runs over SSH after install.sh: a daily timer trades this node's machine token, from its
# Atlas metadata, for its own Datum token from Central. Idempotent; runs one refresh now.
set -euo pipefail

: "${CENTRAL_URL:?CENTRAL_URL is required}"

cat > /usr/local/bin/datum-refresh <<'DATUM_REFRESH'
#!/bin/bash
set -euo pipefail

METADATA=http://169.254.169.254/latest
ENV_FILE=/etc/vector/datum.env

session=$(curl -fsS --max-time 5 -X PUT -H "X-metadata-token-ttl-seconds: 300" "$METADATA/api/token")
machine_token=$(curl -fsS --max-time 5 -H "X-metadata-token: $session" \
	"$METADATA/meta-data/attributes/datum_telemetry_token")

response=$(curl -fsS --max-time 30 -H "Accept: application/json" \
	-H "X-Cargo-Machine-Token: $machine_token" \
	"$CENTRAL_URL/api/method/central.api.cargo.datum_token")

{
	read -r endpoint
	read -r token
} < <(python3 -c '
import json, sys
message = json.load(sys.stdin)["message"]
print(message.get("endpoint") or "")
print(message["token"])
' <<<"$response")

# Central names no endpoint while the region's Datum reports itself down; keep shipping where we were.
endpoint=${endpoint:-$(sed -n 's/^DATUM_ENDPOINT=//p' "$ENV_FILE")}
: "${endpoint:?Central named no Datum endpoint and none is configured}"

next=$(mktemp "$ENV_FILE.XXXXXX")
{
	echo "DATUM_ENDPOINT=${endpoint%/}"
	echo "DATUM_TOKEN=$token"
	grep -v -e '^DATUM_ENDPOINT=' -e '^DATUM_TOKEN=' "$ENV_FILE" || true
} > "$next"

if cmp -s "$next" "$ENV_FILE"; then
	rm -f "$next"
	exit 0
fi

mv "$next" "$ENV_FILE"
systemctl restart vector
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
