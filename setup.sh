#!/bin/bash
# Bring up a Cargo host from a bare Ubuntu machine.
#
# Pilot's installer brings its own MariaDB, Redis and nginx, so nothing is expected to be
# on the machine beforehand. Central registers this host afterwards and pushes its Cargo
# Settings; nothing here needs to know about Central.
set -euo pipefail

BENCH="${BENCH:-cargo}"
SITE="${SITE:-cargo.localhost}"
BRANCH="${BRANCH:-develop}"
REPO="${REPO:-https://github.com/frappe/cargo}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [ -z "$ADMIN_PASSWORD" ]; then
	echo "Set ADMIN_PASSWORD before running." >&2
	exit 1
fi

# Installs Python, Node, MariaDB, Redis and nginx, then pilot itself.
curl -fsSL https://raw.githubusercontent.com/frappe/pilot/develop/install.sh | bash

pilot --yes new "$BENCH" --database mariadb --admin-password "$ADMIN_PASSWORD"
pilot --yes -b "$BENCH" new-site "$SITE" --admin-password "$ADMIN_PASSWORD"
pilot --yes -b "$BENCH" get-app "$REPO" "$BRANCH" --install-dependencies
pilot --yes -b "$BENCH" install-app "$SITE" cargo

# Central needs to reach this site, and an operator needs to paste the pair into the
# Cargo Instance form.
pilot -b "$BENCH" --site "$SITE" execute cargo.api.central.issue_api_credentials
