#!/bin/bash
# Runs over SSH on a service's public machine. Installs nginx, writes the config Cargo rendered
# for it, and takes ports 80 and 443 over from Caddy. The certificate files must already exist.
set -euo pipefail

: "${NGINX_CONFIG:?NGINX_CONFIG is required}"
: "${CONFIG_NAME:?CONFIG_NAME is required}"
CONFIG_PATH="/etc/nginx/conf.d/${CONFIG_NAME}.conf"

if ! command -v nginx > /dev/null 2>&1; then
	# Caddy may still hold ports 80 and 443, so nginx must not start before its config passes.
	printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
	chmod +x /usr/sbin/policy-rc.d
	trap 'rm -f /usr/sbin/policy-rc.d' EXIT
	apt-get update -y
	DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
	rm -f /usr/sbin/policy-rc.d
fi

rm -f /etc/nginx/sites-enabled/default
printf '%s\n' "$NGINX_CONFIG" > "$CONFIG_PATH"

# Fails on a missing certificate file before Caddy stops, so a gateway keeps serving.
nginx -t

# Caddy kept its certificates in /var/lib/caddy; stopping it frees the memory and the ports.
if systemctl list-unit-files caddy.service > /dev/null 2>&1; then
	systemctl disable --now caddy
fi

systemctl enable nginx
systemctl reload nginx 2> /dev/null || systemctl restart nginx
