#!/bin/bash
# Runs over SSH on the datum host. Puts nginx on port 80 in front of the stack, routing by
# subdomain: telemetry-svc.<domain> to datum, telemetry-read-svc.<domain> to ClickHouse.
set -euo pipefail

: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${DATUM_PORT:?DATUM_PORT is required}"
: "${CLICKHOUSE_PORT:?CLICKHOUSE_PORT is required}"
: "${TRUSTED_PROXIES:?TRUSTED_PROXIES is required}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/datum}"

if ! command -v nginx > /dev/null 2>&1; then
	apt-get update -y
	DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
fi

# Only the proxy in front may say who the client was. Anyone else's X-Forwarded-For is
# ignored, or a client could name any address it liked.
real_ip_from=""
for proxy in $TRUSTED_PROXIES; do
	real_ip_from="${real_ip_from}set_real_ip_from ${proxy};
"
done

cat > "$NGINX_SITE" <<NGINX_CONF
${real_ip_from}real_ip_header    X-Forwarded-For;
real_ip_recursive on;

proxy_set_header Host              \$http_host;
proxy_set_header X-Real-IP         \$remote_addr;
proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;
proxy_http_version 1.1;

# A name routed nowhere is dropped. Without this nginx hands it to the first server below,
# so any Host at all would reach datum.
server {
	listen 80 default_server;
	listen [::]:80 default_server;
	server_name _;
	return 444;
}

server {
	listen 80;
	listen [::]:80;
	server_name telemetry-svc.${WILDCARD_DOMAIN};

	# A pilot ships whatever it has buffered, so a batch is as big as its outage was long.
	client_max_body_size    0;
	proxy_request_buffering off;

	location / {
		proxy_pass http://127.0.0.1:${DATUM_PORT};
	}
}

server {
	listen 80;
	listen [::]:80;
	server_name telemetry-read-svc.${WILDCARD_DOMAIN};

	# Results stream back as they are found rather than being held here first.
	proxy_buffering off;
	proxy_read_timeout 300s;

	location / {
		proxy_pass http://127.0.0.1:${CLICKHOUSE_PORT};
	}
}
NGINX_CONF

# Otherwise Debian's welcome page answers for any name this does not route.
rm -f /etc/nginx/sites-enabled/default
ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/datum

nginx -t
systemctl enable nginx
systemctl reload nginx 2> /dev/null || systemctl restart nginx
