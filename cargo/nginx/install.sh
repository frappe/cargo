#!/bin/bash
# Runs over SSH on a service's public machine. Installs nginx in place of Caddy and starts it.
set -euo pipefail

systemctl disable --now caddy 2> /dev/null || true

apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y nginx
rm -f /etc/nginx/sites-enabled/default

systemctl enable --now nginx
