#!/usr/bin/env bash
# Runs over SSH on the datum host. Arguments come from the environment.
#
# The whole stack lives on this one machine: ClickHouse, the checkout and the API. Nothing
# it serves is reachable from outside -- ClickHouse and datum both bind to loopback.
set -euo pipefail

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

DATUM_DIR="${DATUM_DIR:-/opt/datum}"
DATUM_HOST="${DATUM_HOST:-127.0.0.1}"
DATUM_PORT="${DATUM_PORT:-8000}"
DATUM_WORKERS="${DATUM_WORKERS:-2}"
CLICKHOUSE_PORT="${DATUM_CLICKHOUSE_PORT:-8123}"
UV="${UV:-/usr/local/bin/uv}"

CLICKHOUSE_KEYRING=/usr/share/keyrings/clickhouse-keyring.gpg
CLICKHOUSE_ACCESS=/etc/clickhouse-server/users.d/datum.xml

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg apt-transport-https

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
cat > "$CLICKHOUSE_ACCESS" <<CLICKHOUSE_USERS
<clickhouse>
    <users>
        <default>
            <access_management>1</access_management>
            <password_sha256_hex>$(printf '%s' "$DATUM_DEFAULT_PASSWORD" | sha256sum | cut -d' ' -f1)</password_sha256_hex>
        </default>
    </users>
</clickhouse>
CLICKHOUSE_USERS
chmod 600 "$CLICKHOUSE_ACCESS"

systemctl enable --quiet clickhouse-server
systemctl restart clickhouse-server

for _ in $(seq 1 30); do
	if clickhouse-client --password "$DATUM_DEFAULT_PASSWORD" --query "SELECT 1" > /dev/null 2>&1; then
		break
	fi
	sleep 2
done
clickhouse-client --password "$DATUM_DEFAULT_PASSWORD" --query "SELECT 1" > /dev/null

# --- datum -------------------------------------------------------------------------------
if ! "$UV" --version > /dev/null 2>&1; then
	curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

# Re-run safe: the machine may already hold a checkout from an earlier attempt.
if [ ! -d "$DATUM_DIR/.git" ]; then
	git clone --quiet "$DATUM_REPOSITORY" "$DATUM_DIR"
fi

cd "$DATUM_DIR"
git remote set-url origin "$DATUM_REPOSITORY"
git fetch --quiet --tags --prune origin

# `version` is whatever the operator wrote: a branch, a tag or a commit. A branch has to
# follow the remote, so it is reset to origin's; anything else is checked out detached.
if git rev-parse --verify --quiet "refs/remotes/origin/$DATUM_VERSION" > /dev/null; then
	git checkout --quiet -B "$DATUM_VERSION" "origin/$DATUM_VERSION"
	git reset --quiet --hard "origin/$DATUM_VERSION"
elif git rev-parse --verify --quiet "$DATUM_VERSION^{commit}" > /dev/null; then
	git checkout --quiet --detach "$DATUM_VERSION"
else
	echo "no branch, tag or commit named '$DATUM_VERSION' in $DATUM_REPOSITORY" >&2
	exit 1
fi
echo "datum is at $(git rev-parse --short HEAD)"

"$UV" sync --group api

if [ -n "${DATUM_JWT_PUBLIC_KEY:-}" ]; then
	install -d -m 700 "$(dirname "$DATUM_JWT_PUBLIC_KEY_FILE")"
	install -m 600 /dev/null "$DATUM_JWT_PUBLIC_KEY_FILE"
	printf '%s\n' "$DATUM_JWT_PUBLIC_KEY" > "$DATUM_JWT_PUBLIC_KEY_FILE"
fi

# The unit reads its secrets from here rather than carrying them in its own text, where
# they would be world-readable through systemctl show.
install -m 600 /dev/null /etc/datum.env
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
"$UV" run datum-migrate \
	--insights-user-password "$DATUM_INSIGHTS_PASSWORD" \
	--default-user-password "$DATUM_DEFAULT_PASSWORD"

cat > /etc/systemd/system/datum.service <<DATUM_UNIT
[Unit]
Description=Datum Telemetry API
After=network-online.target clickhouse-server.service
Wants=network-online.target

[Service]
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

for _ in $(seq 1 30); do
	if curl -fsS -o /dev/null "http://$DATUM_HOST:$DATUM_PORT/health"; then
		exit 0
	fi
	sleep 2
done

echo "datum did not come up within 60s" >&2
systemctl status datum --no-pager --lines 30 >&2
exit 1
