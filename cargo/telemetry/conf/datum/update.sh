#!/usr/bin/env bash
# Runs over SSH on a datum host install.sh set up. Arguments come from the environment.
#
# Moves the checkout to DATUM_COMMIT, or to the newest commit of DATUM_VERSION, then migrates
# ClickHouse and restarts datum. ClickHouse, the unit and /etc/datum.env are left as they are.
set -euo pipefail

: "${DATUM_REPOSITORY:?DATUM_REPOSITORY is required}"
: "${DATUM_VERSION:?DATUM_VERSION is required}"
: "${DATUM_USER_PASSWORD:?set it to the password for the datum user}"
: "${INSIGHTS_USER_PASSWORD:?set it to the password for the insights user}"
: "${DEFAULT_USER_PASSWORD:?set it to the password for the ClickHouse default user}"

SERVICE_USER="${SERVICE_USER:-frappe}"
DATUM_DIR="${DATUM_DIR:-/home/$SERVICE_USER/datum}"
DATUM_HOST="${DATUM_HOST:-127.0.0.1}"
DATUM_PORT="${DATUM_PORT:-8000}"
UV="${UV:-/usr/local/bin/uv}"
TARGET="${DATUM_COMMIT:-$DATUM_VERSION}"

if [ ! -d "$DATUM_DIR/.git" ] || [ ! -f /etc/datum.env ]; then
	echo "datum is not installed in $DATUM_DIR; set the host up first" >&2
	exit 1
fi

as_user() {
	su - "$SERVICE_USER" -c "$1"
}

wait_for() {
	local seconds="$1" what="$2"
	shift 2

	local waited=0
	while [ "$waited" -lt "$seconds" ]; do
		if "$@" > /dev/null 2>&1; then
			return 0
		fi
		sleep 2
		waited=$((waited + 2))
	done

	echo "$what did not come up within ${seconds}s" >&2
	return 1
}

# Quoted for the login shell `as_user` hands them to, as install.sh does.
DIR=$(printf '%q' "$DATUM_DIR")
REF=$(printf '%q' "$TARGET")
REPOSITORY=$(printf '%q' "$DATUM_REPOSITORY")

before=$(as_user "git -C $DIR rev-parse --short HEAD")
as_user "git -C $DIR remote set-url origin $REPOSITORY"
as_user "git -C $DIR fetch --quiet --tags --prune origin"

if as_user "git -C $DIR rev-parse --verify --quiet refs/remotes/origin/$REF" > /dev/null; then
	as_user "git -C $DIR checkout --quiet --force -B $REF origin/$REF"
	as_user "git -C $DIR reset --quiet --hard origin/$REF"
elif as_user "git -C $DIR rev-parse --verify --quiet $REF^{commit}" > /dev/null; then
	as_user "git -C $DIR checkout --quiet --force --detach $REF"
else
	echo "no branch, tag or commit named '$TARGET' in $DATUM_REPOSITORY" >&2
	exit 1
fi
# No skip on the same commit: a run that stopped before the restart leaves the old process
# answering /health, so the checkout says nothing about what is running. This reloads it.
echo "datum moves from $before to $(as_user "git -C $DIR rev-parse --short HEAD")"

as_user "$UV sync --group api --directory $DIR"

# Every migration is IF NOT EXISTS, so this applies only what the new commit adds.
as_user "set -a; . /etc/datum.env; set +a; $UV run --directory $DIR datum-migrate \
	--insights-user-password $(printf '%q' "$INSIGHTS_USER_PASSWORD") \
	--default-user-password $(printf '%q' "$DEFAULT_USER_PASSWORD")"

systemctl restart datum

if ! wait_for 60 "datum" curl -fs -o /dev/null "http://$DATUM_HOST:$DATUM_PORT/health"; then
	systemctl status datum --no-pager --lines 30 >&2
	exit 1
fi

echo "datum is answering on $DATUM_HOST:$DATUM_PORT"
