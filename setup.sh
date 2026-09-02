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
CENTRAL_BOOTSTRAPPING_TOKEN="${CENTRAL_BOOTSTRAPPING_TOKEN:-}"  # Central's token for this host
CENTRAL_URL="${CENTRAL_URL:-}" # Central's URL for this host to call back to
ATLAS_URL="${ATLAS_URL:-}" # Atlas's URL for this host to call back to
REGION="${REGION:-}" # which region this Cargo provisions for

if [ -z "$PILOT_ADMIN_PASSWORD" ] || [ -z "$SITE_PASSWORD" ]; then
	echo "Set PILOT_ADMIN_PASSWORD and SITE_PASSWORD before running." >&2
	exit 1
fi

if [ -z "$CENTRAL_BOOTSTRAPPING_TOKEN" ] || [ -z "$CENTRAL_URL" ] || [ -z "$ATLAS_URL" ] || [ -z "$REGION" ]; then
	echo "Set CENTRAL_BOOTSTRAPPING_TOKEN, CENTRAL_URL, ATLAS_URL and REGION before running." >&2
	echo "The token comes from this host's Cargo Instance in Central." >&2
	exit 1
fi

# Installs Python, Node, MariaDB, Redis and nginx, then pilot itself. Pinned to a release
# rather than develop, so two hosts built a week apart get the same pilot.
curl -fsSL "https://raw.githubusercontent.com/frappe/pilot/${PILOT_VERSION}/install.sh" | bash

pilot --yes new "$BENCH" --database mariadb --admin-password "$PILOT_ADMIN_PASSWORD"
pilot --yes -b "$BENCH" new-site "$SITE" --admin-password "$SITE_PASSWORD"
pilot --yes -b "$BENCH" get-app "$REPO" "$BRANCH" --install-dependencies
# The install hook reads these and enrols the host with Central.
export CENTRAL_BOOTSTRAPPING_TOKEN CENTRAL_URL ATLAS_URL REGION
pilot --yes -b "$BENCH" install-app "$SITE" cargo
