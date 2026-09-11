#!/bin/bash
# Bring up a Cargo host from a bare Ubuntu machine.
#
# Pilot's installer brings its own MariaDB, Redis and nginx, so nothing is expected to be
# on the machine beforehand. Everything below the passwords is what the app is installed
# with: the provisioner knows all of it, and Cargo Settings has no other way to learn it.
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
# One per mandatory field of Cargo Settings, and nothing else: the install hook writes
# these straight onto it, so anything missing here fails the install.
CENTRAL_URL="${CENTRAL_URL:-}" # Central's URL for this host to call
ATLAS_URL="${ATLAS_URL:-}" # Atlas's URL for this host to call
CARGO_URL="${CARGO_URL:-}" # where this host answers
CENTRAL_WEBHOOK_SECRET="${CENTRAL_WEBHOOK_SECRET:-}" # signs the reports this host sends Central
REGION="${REGION:-}" # which region this Cargo provisions for
REGION_ID="${REGION_ID:-}" # that region's numeric id, as Atlas knows it
ATLAS_KEY="${ATLAS_KEY:-}"
ATLAS_SECRET="${ATLAS_SECRET:-}"
ATLAS_TENANT_ID="${ATLAS_TENANT_ID:-}" # the tenant every Atlas call of this host is scoped to
BENCH_USER="${BENCH_USER:-frappe}" # pilot refuses to run as root, so the bench gets its own user
BENCH_UID="${BENCH_UID:-1001}"
BENCH_GID="${BENCH_GID:-1001}"

if [ -z "$PILOT_ADMIN_PASSWORD" ] || [ -z "$SITE_PASSWORD" ]; then
	echo "Set PILOT_ADMIN_PASSWORD and SITE_PASSWORD before running." >&2
	exit 1
fi

ENROLMENT_VARS="CENTRAL_URL ATLAS_URL CARGO_URL CENTRAL_WEBHOOK_SECRET REGION REGION_ID \
	ATLAS_KEY ATLAS_SECRET ATLAS_TENANT_ID"

missing=""
for name in $ENROLMENT_VARS; do
	[ -n "${!name}" ] || missing="$missing $name"
done

if [ -n "$missing" ]; then
	echo "Set$missing before running." >&2
	echo "The webhook secret and the region's id come from this host's Cargo Instance in Central." >&2
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
# The install hook reads these, so they are quoted once and exported into that one command.
enrolment=""
for name in $ENROLMENT_VARS; do
	enrolment="$enrolment $name=$(printf '%q' "${!name}")"
done

# Installs Python, Node, MariaDB, Redis and nginx, then pilot itself. Pinned to a release
# rather than develop, so two hosts built a week apart get the same pilot.
#
# Twice, as the image build does: pilot refuses to install as root, so the first pass lays
# down the system stack and the second installs pilot itself for the bench user.
curl -fsSL "$INSTALLER" | bash
as_bench_user "curl -fsSL $q_installer | bash"

as_bench_user "pilot --yes new $q_bench --database mariadb --admin-password $q_admin_password"
as_bench_user "pilot --yes -b $q_bench new-site $q_site --admin-password $q_site_password"
as_bench_user "pilot --yes -b $q_bench get-app $q_repo $q_branch --install-dependencies"

# `su -` starts a login shell, so the enrolment variables are exported inside it rather
# than out here.
as_bench_user "$enrolment pilot --yes -b $q_bench install-app $q_site cargo"
