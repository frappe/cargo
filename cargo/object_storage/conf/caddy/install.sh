#!/bin/bash
# Runs over SSH on a cluster's proxy node. Caddy serves s3-svc and s3-admin-svc over public
# TLS, obtaining and renewing certificates itself, and forwards to Garage on the gateway.
set -euo pipefail

: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${S3_UPSTREAM:?S3_UPSTREAM is required}"
: "${ADMIN_UPSTREAM:?ADMIN_UPSTREAM is required}"
: "${CORS_ORIGIN:?CORS_ORIGIN is required}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

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

# Caddy passes Host through unchanged, which S3 needs: it signs Host into every request.
# Bodies stream both ways with no size cap.
cat > "$CADDYFILE" <<CADDY_CONF
# Buckets are addressed by path only, so a bucket in the hostname matches no site here.
s3-svc.${WILDCARD_DOMAIN} {
	# Garage sets CORS only per bucket, with an owner key, so Caddy answers for every bucket.
	# The signature still authorises each request; CORS only lets the browser send it.
	@preflight method OPTIONS
	handle @preflight {
		header Access-Control-Allow-Origin "${CORS_ORIGIN}"
		header Access-Control-Allow-Methods "GET, HEAD, PUT"
		header Access-Control-Allow-Headers "Content-Type"
		header Access-Control-Max-Age 3600
		respond 204
	}

	handle {
		reverse_proxy ${S3_UPSTREAM} {
			flush_interval -1
			header_down Access-Control-Allow-Origin "${CORS_ORIGIN}"
			header_down Access-Control-Expose-Headers "ETag"
		}
	}
}

s3-admin-svc.${WILDCARD_DOMAIN} {
	reverse_proxy ${ADMIN_UPSTREAM}
}
CADDY_CONF

caddy validate --adapter caddyfile --config "$CADDYFILE"
systemctl enable caddy
systemctl reload caddy 2> /dev/null || systemctl restart caddy
