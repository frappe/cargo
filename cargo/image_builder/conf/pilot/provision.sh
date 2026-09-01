#!/bin/bash
# Runs over SSH on a bare Atlas VM. Its exit status is the build's, so no status file.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${VERSION:?VERSION is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
FRAPPE_VERSION="${FRAPPE_VERSION:-}"
SITE="${SITE:-}"
BENCH="${BENCH:-pilot}"

curl -fsSL "https://raw.githubusercontent.com/frappe/pilot/${VERSION}/install.sh" | SUDO_PASS="" bash

if [ -n "$FRAPPE_VERSION" ]; then
	pilot --yes new "$BENCH" --database mariadb --admin-password "$ADMIN_PASSWORD"
	pilot --yes -b "$BENCH" get-app https://github.com/frappe/frappe "$FRAPPE_VERSION" --install-dependencies
fi

if [ -n "$SITE" ]; then
	pilot --yes -b "$BENCH" new-site "$SITE" --admin-password "$ADMIN_PASSWORD"
fi

# The snapshot should not carry build litter or this machine's identity.
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
truncate -s 0 /etc/machine-id
rm -f /root/.ssh/authorized_keys /etc/ssh/ssh_host_*
sync
