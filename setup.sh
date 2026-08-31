#!/bin/bash
# Bring up a Cargo host from a bare Ubuntu machine.
#
# Pilot's installer brings its own MariaDB, Redis and nginx, so nothing is expected to be
# on the machine beforehand. Central registers this host afterwards and pushes its Cargo
# Settings; nothing here needs to know about Central.
set -euo pipefail

PILOT_VERSION="${PILOT_VERSION:-v0.0.29-pre-alpha}"
BENCH="${BENCH:-cargo}"
SITE="${SITE:-cargo.localhost}"
BRANCH="${BRANCH:-develop}"
REPO="${REPO:-https://github.com/frappe/cargo}"
# Two different passwords. MariaDB's root password is not one of them: pilot generates
# that itself when it initialises the bench.
PILOT_ADMIN_PASSWORD="${PILOT_ADMIN_PASSWORD:-}"   # pilot's own admin panel
SITE_PASSWORD="${SITE_PASSWORD:-}"     # the site's Frappe Administrator

if [ -z "$PILOT_ADMIN_PASSWORD" ] || [ -z "$SITE_PASSWORD" ]; then
	echo "Set PILOT_ADMIN_PASSWORD and SITE_PASSWORD before running." >&2
	exit 1
fi

# Installs Python, Node, MariaDB, Redis and nginx, then pilot itself. Pinned to a release
# rather than develop, so two hosts built a week apart get the same pilot.
curl -fsSL "https://raw.githubusercontent.com/frappe/pilot/${PILOT_VERSION}/install.sh" | bash

pilot --yes new "$BENCH" --database mariadb --admin-password "$PILOT_ADMIN_PASSWORD"
pilot --yes -b "$BENCH" new-site "$SITE" --admin-password "$SITE_PASSWORD"
pilot --yes -b "$BENCH" get-app "$REPO" "$BRANCH" --install-dependencies
pilot --yes -b "$BENCH" install-app "$SITE" cargo

# Central needs to reach this site, and an operator needs to paste the pair into the
# Cargo Instance form.
pilot -b "$BENCH" --site "$SITE" execute cargo.api.central.issue_api_credentials
