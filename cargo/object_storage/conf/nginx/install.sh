#!/bin/bash
# Runs over SSH on a cluster's gateway only. Puts nginx on port 80 in front of Garage, routing
# by subdomain: s3.<domain> to the S3 API, s3-admin.<domain> to the admin API.
set -euo pipefail

: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${S3_PORT:?S3_PORT is required}"
: "${ADMIN_PORT:?ADMIN_PORT is required}"
: "${TRUSTED_PROXIES:?TRUSTED_PROXIES is required}"
: "${CORS_ORIGIN:?CORS_ORIGIN is required}"
NGINX_SITE="${NGINX_SITE:-/etc/nginx/sites-available/garage}"

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

# The headers every upstream gets. Host is passed exactly as the client sent it: S3 signs it
# into every request, so a rewritten Host fails the signature.
proxy_set_header Host              \$http_host;
proxy_set_header X-Real-IP         \$remote_addr;
proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;
proxy_http_version 1.1;

# A name routed nowhere is dropped. Without this nginx hands it to the first server below,
# so any Host at all would reach Garage.
server {
	listen 80 default_server;
	listen [::]:80 default_server;
	server_name _;
	return 444;
}

server {
	listen 80;
	listen [::]:80;
	# Buckets are addressed by path only (s3-svc.<domain>/<bucket>), so this one name is all
	# there is: a bucket in the hostname lands on the catch-all and is dropped.
	server_name s3-svc.${WILDCARD_DOMAIN};

	# Objects go straight through: no size cap, and nothing held on this disk on the way.
	client_max_body_size    0;
	proxy_request_buffering off;
	proxy_buffering         off;

	# The Central dashboard uploads and downloads with presigned URLs, from its own origin.
	# Garage sets CORS only per bucket, with an owner key, so nginx answers for every bucket.
	# The signature still authorises each request; CORS only lets the browser send it.
	location / {
		if (\$request_method = OPTIONS) {
			add_header Access-Control-Allow-Origin  "${CORS_ORIGIN}" always;
			add_header Access-Control-Allow-Methods "GET, HEAD, PUT" always;
			add_header Access-Control-Allow-Headers "Content-Type" always;
			add_header Access-Control-Max-Age       3600 always;
			return 204;
		}

		proxy_hide_header Access-Control-Allow-Origin;
		add_header Access-Control-Allow-Origin   "${CORS_ORIGIN}" always;
		add_header Access-Control-Expose-Headers "ETag" always;
		proxy_pass http://127.0.0.1:${S3_PORT};
	}
}

server {
	listen 80;
	listen [::]:80;
	server_name s3-admin-svc.${WILDCARD_DOMAIN};

	location / {
		proxy_pass http://127.0.0.1:${ADMIN_PORT};
	}
}
NGINX_CONF

# Otherwise Debian's welcome page answers for any name this does not route.
rm -f /etc/nginx/sites-enabled/default
ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/garage

nginx -t
systemctl enable nginx
systemctl reload nginx 2> /dev/null || systemctl restart nginx
