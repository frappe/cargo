#!/usr/bin/env bash
# Runs over SSH on the datum host. Arguments come from the environment.
#
# The whole stack lives on this one machine: ClickHouse, the checkout and the API. Nothing
# it serves is reachable from outside -- ClickHouse and datum both bind to loopback.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${DATUM_REPOSITORY:?DATUM_REPOSITORY is required}"
: "${DATUM_VERSION:?DATUM_VERSION is required}"
: "${DATUM_CLICKHOUSE_USER:?set it to the user datum-api connects as}"
: "${DATUM_CLICKHOUSE_PASSWORD:?set it to the password for the datum user}"
: "${DATUM_INSIGHTS_PASSWORD:?set it to the password for the insights user}"
: "${DATUM_DEFAULT_PASSWORD:?set it to the password for the ClickHouse default user}"

if [ -z "${DATUM_JWT_PUBLIC_KEY_FILE:-}" ] && [ -z "${DATUM_OIDC_ISSUER:-}" ]; then
	echo "set DATUM_JWT_PUBLIC_KEY_FILE or DATUM_OIDC_ISSUER, or every call is a 401" >&2
	exit 1
fi

SERVICE_USER="${SERVICE_USER:-frappe}"
SERVICE_UID="${SERVICE_UID:-1001}"
SERVICE_GID="${SERVICE_GID:-1001}"
DATUM_DIR="${DATUM_DIR:-/home/$SERVICE_USER/datum}"
DATUM_HOST="${DATUM_HOST:-127.0.0.1}"
DATUM_PORT="${DATUM_PORT:-8000}"
DATUM_WORKERS="${DATUM_WORKERS:-2}"
CLICKHOUSE_PORT="${DATUM_CLICKHOUSE_PORT:-8123}"
UV="${UV:-/usr/local/bin/uv}"

CLICKHOUSE_KEYRING=/usr/share/keyrings/clickhouse-keyring.gpg
CLICKHOUSE_ACCESS=/etc/clickhouse-server/users.d/datum.xml

apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg apt-transport-https

# Datum runs as its own user, never root. The uid is pinned so anything baked into an
# image is owned by the same account this creates.
if ! id -u "$SERVICE_USER" > /dev/null 2>&1; then
	groupadd -g "$SERVICE_GID" "$SERVICE_USER" 2> /dev/null || groupadd "$SERVICE_USER"
	useradd -m -s /bin/bash -u "$SERVICE_UID" -g "$SERVICE_USER" "$SERVICE_USER" 2> /dev/null ||
		useradd -m -s /bin/bash -g "$SERVICE_USER" "$SERVICE_USER"
fi

as_user() {
	su - "$SERVICE_USER" -c "$1"
}

# Poll until it answers: nothing here starts instantly.
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

# --- ClickHouse ------------------------------------------------------------------------
# apt-key is gone in 24.04, so the key is dearmoured and the repo line signed-by= it.
if ! command -v clickhouse-client > /dev/null; then
	curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key |
		gpg --dearmor --yes -o "$CLICKHOUSE_KEYRING"
	chmod 644 "$CLICKHOUSE_KEYRING"
	echo "deb [signed-by=$CLICKHOUSE_KEYRING] https://packages.clickhouse.com/deb stable main" \
		> /etc/apt/sources.list.d/clickhouse.list
	apt-get update -qq
	apt-get install -y -qq clickhouse-server clickhouse-client
fi

# Two things a stock install will not do: let `default` issue CREATE USER, which the ACL
# migration needs, and give it a password. Both are ours to set -- we installed it.
install -d -m 755 /etc/clickhouse-server/users.d
# `remove="remove"` drops the empty <password> the stock users.xml ships: ClickHouse
# refuses a user carrying two authentication methods.
cat > "$CLICKHOUSE_ACCESS" <<CLICKHOUSE_USERS
<clickhouse>
    <users>
        <default>
            <access_management>1</access_management>
            <password remove="remove"/>
            <password_sha256_hex>$(printf '%s' "$DATUM_DEFAULT_PASSWORD" | sha256sum | cut -d' ' -f1)</password_sha256_hex>
        </default>
    </users>
</clickhouse>
CLICKHOUSE_USERS
# The server drops to its own user before reading this, so root-only would lock it out.
chown clickhouse:clickhouse "$CLICKHOUSE_ACCESS"
chmod 600 "$CLICKHOUSE_ACCESS"

systemctl enable --quiet clickhouse-server
systemctl restart clickhouse-server

wait_for 60 "clickhouse" clickhouse-client --password "$DATUM_DEFAULT_PASSWORD" --query "SELECT 1"

# --- datum -------------------------------------------------------------------------------
if ! "$UV" --version > /dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

# Re-run safe: the machine may already hold a checkout from an earlier attempt.
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATUM_DIR"
if [ ! -d "$DATUM_DIR/.git" ]; then
	as_user "git clone --quiet $(printf '%q' "$DATUM_REPOSITORY") $(printf '%q' "$DATUM_DIR")"
fi

cd "$DATUM_DIR"

# `as_user` hands its argument to a login shell, which parses it again -- so everything
# that came from outside this script is quoted for that second pass. A version is whatever
# the operator typed; unquoted, one carrying `;` or `$(` would run as the service account.
DIR=$(printf '%q' "$DATUM_DIR")
VERSION=$(printf '%q' "$DATUM_VERSION")
REPOSITORY=$(printf '%q' "$DATUM_REPOSITORY")

as_user "git -C $DIR remote set-url origin $REPOSITORY"
as_user "git -C $DIR fetch --quiet --tags --prune origin"

# A branch has to follow the remote, so it is reset to origin's; a tag or commit is
# checked out detached.
if as_user "git -C $DIR rev-parse --verify --quiet refs/remotes/origin/$VERSION" > /dev/null; then
	as_user "git -C $DIR checkout --quiet -B $VERSION origin/$VERSION"
	as_user "git -C $DIR reset --quiet --hard origin/$VERSION"
elif as_user "git -C $DIR rev-parse --verify --quiet $VERSION^{commit}" > /dev/null; then
	as_user "git -C $DIR checkout --quiet --detach $VERSION"
else
	echo "no branch, tag or commit named '$DATUM_VERSION' in $DATUM_REPOSITORY" >&2
	exit 1
fi
echo "datum is at $(as_user "git -C $DIR rev-parse --short HEAD")"

as_user "$UV sync --group api --directory $DIR"

if [ -n "${DATUM_JWT_PUBLIC_KEY:-}" ]; then
	install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$(dirname "$DATUM_JWT_PUBLIC_KEY_FILE")"
	install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 /dev/null "$DATUM_JWT_PUBLIC_KEY_FILE"
	printf '%s\n' "$DATUM_JWT_PUBLIC_KEY" > "$DATUM_JWT_PUBLIC_KEY_FILE"
fi

# The unit reads its secrets from here rather than carrying them in its own text, where
# they would be world-readable through systemctl show.
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 /dev/null /etc/datum.env
{
	echo "DATUM_CLICKHOUSE_HOST=${DATUM_CLICKHOUSE_HOST:-127.0.0.1}"
	echo "DATUM_CLICKHOUSE_PORT=$CLICKHOUSE_PORT"
	echo "DATUM_CLICKHOUSE_USER=$DATUM_CLICKHOUSE_USER"
	echo "DATUM_CLICKHOUSE_PASSWORD=$DATUM_CLICKHOUSE_PASSWORD"
	echo "DATUM_TIMEOUT=${DATUM_TIMEOUT:-30}"
	[ -n "${DATUM_OIDC_ISSUER:-}" ] && echo "DATUM_OIDC_ISSUER=$DATUM_OIDC_ISSUER"
	[ -n "${DATUM_JWT_PUBLIC_KEY:-}" ] && echo "DATUM_JWT_PUBLIC_KEY_FILE=$DATUM_JWT_PUBLIC_KEY_FILE"
} >> /etc/datum.env

# Connects as `default`, because `datum` is what it is about to create.
as_user "set -a; . /etc/datum.env; set +a; $UV run --directory $(printf '%q' "$DATUM_DIR") datum-migrate \
	--insights-user-password $(printf '%q' "$DATUM_INSIGHTS_PASSWORD") \
	--default-user-password $(printf '%q' "$DATUM_DEFAULT_PASSWORD")"

cat > /etc/systemd/system/datum.service <<DATUM_UNIT
[Unit]
Description=Datum Telemetry API
After=network-online.target clickhouse-server.service
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DATUM_DIR
EnvironmentFile=/etc/datum.env
ExecStart=$DATUM_DIR/.venv/bin/uvicorn datum.api.app:create_app --factory --host $DATUM_HOST --port $DATUM_PORT --workers $DATUM_WORKERS
# ClickHouse may still be starting when this first runs; restarting is what waits it out.
Restart=always
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
DATUM_UNIT

systemctl daemon-reload
systemctl enable --quiet datum
systemctl restart datum

if ! wait_for 60 "datum" curl -fs -o /dev/null "http://$DATUM_HOST:$DATUM_PORT/health"; then
	systemctl status datum --no-pager --lines 30 >&2
	exit 1
fi

echo "datum is answering on $DATUM_HOST:$DATUM_PORT"
