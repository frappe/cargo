#!/bin/bash
# Bring up a production Cargo host from a bare Ubuntu machine.
#
# Pilot's installer brings its own MariaDB, Redis and nginx, so nothing is expected to be
# on the machine beforehand. This host enrols itself with Central on install, using the
# bootstrapping token below: Central never calls back, so it needs Central's URL up front.
#
# The bench is deployed to production behind nginx on port 80, without TLS: HTTPS is
# terminated by the proxy in front of this host.
set -euo pipefail

PILOT_VERSION="${PILOT_VERSION:-v0.0.29-pre-alpha}"
BENCH="${BENCH:-cargo}"
SITE="${SITE:-cargo.localhost}"
ADMIN_DOMAIN="${ADMIN_DOMAIN:-}"
BRANCH="${BRANCH:-develop}"
REPO="${REPO:-https://github.com/frappe/cargo}"
PILOT_ADMIN_PASSWORD="${PILOT_ADMIN_PASSWORD:-}"
SITE_PASSWORD="${SITE_PASSWORD:-}"
CENTRAL_BOOTSTRAPPING_TOKEN="${CENTRAL_BOOTSTRAPPING_TOKEN:-}"
CENTRAL_URL="${CENTRAL_URL:-}"
ATLAS_URL="${ATLAS_URL:-}"
REGION="${REGION:-}"
# pilot refuses to run as root, so the bench gets its own user.
BENCH_USER="${BENCH_USER:-frappe}"
BENCH_UID="${BENCH_UID:-1001}"
BENCH_GID="${BENCH_GID:-1001}"

if [ -z "$PILOT_ADMIN_PASSWORD" ] || [ -z "$SITE_PASSWORD" ]; then
	echo "Set PILOT_ADMIN_PASSWORD and SITE_PASSWORD before running." >&2
	exit 1
fi

if [ -z "$ADMIN_DOMAIN" ]; then
	echo "Set ADMIN_DOMAIN before running: production needs a domain for pilot's admin panel." >&2
	exit 1
fi

if [ -z "$CENTRAL_BOOTSTRAPPING_TOKEN" ] || [ -z "$CENTRAL_URL" ] || [ -z "$ATLAS_URL" ] || [ -z "$REGION" ]; then
	echo "Set CENTRAL_BOOTSTRAPPING_TOKEN, CENTRAL_URL, ATLAS_URL and REGION before running." >&2
	echo "The token comes from this host's Cargo Instance in Central." >&2
	exit 1
fi

if ! id -u "$BENCH_USER" > /dev/null 2>&1; then
	groupadd -g "$BENCH_GID" "$BENCH_USER" 2> /dev/null || groupadd "$BENCH_USER"
	useradd -m -s /bin/bash -u "$BENCH_UID" -g "$BENCH_USER" "$BENCH_USER" 2> /dev/null ||
		useradd -m -s /bin/bash -g "$BENCH_USER" "$BENCH_USER"
fi

as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/${PILOT_VERSION}/install.sh"

q_installer=$(printf '%q' "$INSTALLER")
q_bench=$(printf '%q' "$BENCH")
q_site=$(printf '%q' "$SITE")
q_repo=$(printf '%q' "$REPO")
q_branch=$(printf '%q' "$BRANCH")
q_admin_password=$(printf '%q' "$PILOT_ADMIN_PASSWORD")
q_site_password=$(printf '%q' "$SITE_PASSWORD")
q_bootstrapping_token=$(printf '%q' "$CENTRAL_BOOTSTRAPPING_TOKEN")
q_central_url=$(printf '%q' "$CENTRAL_URL")
q_atlas_url=$(printf '%q' "$ATLAS_URL")
q_region=$(printf '%q' "$REGION")
q_admin_domain=$(printf '%q' "$ADMIN_DOMAIN")

# Twice: the first pass lays down the system stack as root, the second installs pilot for
# the bench user.
curl -fsSL "$INSTALLER" | bash
as_bench_user "curl -fsSL $q_installer | bash"

as_bench_user "pilot --yes new $q_bench --database mariadb --admin-password $q_admin_password"
as_bench_user "pilot --yes -b $q_bench new-site $q_site --admin-password $q_site_password"
as_bench_user "pilot --yes -b $q_bench get-app $q_repo $q_branch --install-dependencies"

# `su -` starts a login shell, so the install hook's variables are exported inside it.
as_bench_user "CENTRAL_BOOTSTRAPPING_TOKEN=$q_bootstrapping_token \
	CENTRAL_URL=$q_central_url ATLAS_URL=$q_atlas_url REGION=$q_region \
	pilot --yes -b $q_bench install-app $q_site cargo"

as_bench_user "pilot --yes -b $q_bench setup production --admin-domain $q_admin_domain"
