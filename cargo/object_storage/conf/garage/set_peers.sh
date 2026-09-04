#!/bin/bash
# Records the peers a node should try on its next boot. Garage is already connected to them,
# so nothing is restarted here.
set -euo pipefail

: "${BOOTSTRAP_PEERS:?BOOTSTRAP_PEERS is required}"
GARAGE_CONFIG="${GARAGE_CONFIG:-/etc/garage.toml}"

awk -v list="$BOOTSTRAP_PEERS" '
	/^bootstrap_peers = \[/ {
		print
		count = split(list, peers, " ")
		for (index_ = 1; index_ <= count; index_++) printf "\t\"%s\",\n", peers[index_]
		inside = 1
		next
	}
	inside && /^\]/ { print; inside = 0; next }
	inside { next }
	{ print }
' "$GARAGE_CONFIG" > "$GARAGE_CONFIG.tmp"

mv "$GARAGE_CONFIG.tmp" "$GARAGE_CONFIG"
chmod 600 "$GARAGE_CONFIG"
