#!/bin/bash
# Runs over SSH on a service's public machine. Installs Caddy and writes the Caddyfile Cargo
# rendered for it. Caddy obtains and renews the certificates for every site in it by itself.
set -euo pipefail

: "${CADDYFILE:?CADDYFILE is required}"
CADDYFILE_PATH="${CADDYFILE_PATH:-/etc/caddy/Caddyfile}"

if ! command -v caddy > /dev/null 2>&1; then
	apt-get update -y
	DEBIAN_FRONTEND=noninteractive apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -y
	DEBIAN_FRONTEND=noninteractive apt-get install -y caddy
fi

printf '%s\n' "$CADDYFILE" > "$CADDYFILE_PATH"

caddy validate --adapter caddyfile --config "$CADDYFILE_PATH"
systemctl enable caddy
systemctl reload caddy 2> /dev/null || systemctl restart caddy
