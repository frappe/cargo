#!/bin/bash
# Runs over SSH on a bare Atlas VM. Its exit status is the build's, so no status file.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${VERSION:?VERSION is required}"
: "${ADMIN_DOMAIN:?ADMIN_DOMAIN is required}"
: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${SITE:?SITE is required}"
: "${BENCH:?BENCH is required}"
: "${FRAPPE_VERSION:?FRAPPE_VERSION is required}"
BENCH_USER="${BENCH_USER:-frappe}"
BENCH_UID="${BENCH_UID:-1001}"
BENCH_GID="${BENCH_GID:-1001}"
SWAP_SIZE="${SWAP_SIZE:-1536M}"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-30}"
PROBE_DELAY="${PROBE_DELAY:-5}"

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/${VERSION}/install.sh"

# Pilot wants an upper, a lower, a digit and a symbol. Nothing reads this again: the host
# is Central managed from its first boot, and Cargo keeps no copy.
ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24)aA1#"

# The image boots with the memory it was baked on, which is not enough to build assets.
# The cleanup at the end takes the swap file off, so it is never part of the snapshot.
fallocate -l "$SWAP_SIZE" /swapfile
chmod 600 /swapfile
mkswap -q /swapfile
swapon /swapfile

# Cloud images ship their own first user (ubuntu at 1000). Clear every regular user out so
# the bench user is the only one, at ids the image can rely on: anything baked into this
# image is owned by 1001, whatever the base image happened to number its own user.
for existing in $(awk -F: -v me="$BENCH_USER" '$3 >= 1000 && $3 < 60000 && $1 != me {print $1}' /etc/passwd); do
	pkill -KILL -u "$existing" 2> /dev/null || true
	userdel -r "$existing" 2> /dev/null || userdel "$existing" || true
done

# Free the ids in case a group outlived its user.
stale_group="$(awk -F: -v gid="$BENCH_GID" -v me="$BENCH_USER" '$3 == gid && $1 != me {print $1}' /etc/group)"
[ -n "$stale_group" ] && groupdel "$stale_group" 2> /dev/null || true

if ! id -u "$BENCH_USER" > /dev/null 2>&1; then
	groupadd -g "$BENCH_GID" "$BENCH_USER"
	useradd -m -s /bin/bash -u "$BENCH_UID" -g "$BENCH_GID" "$BENCH_USER"
fi

# Pilot refuses to install as root: the first pass installs the system stack and creates
# the bench user, then stops and tells you to run it again as that user.
as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

curl -fsSL "$INSTALLER" | bash
as_bench_user "curl -fsSL '$INSTALLER' | bash"

# The admin domain is set here rather than at `setup production`, because the hostname
# alias below only renders for a domain the bench already claims.
as_bench_user "pilot --yes new '$BENCH' --database mariadb --admin-domain '$ADMIN_DOMAIN' --admin-password '$ADMIN_PASSWORD'"

# `new` only writes bench.toml; `init` clones and installs the framework app it names. The
# branch is config, not a flag, so it is set in between. No get-app: the bench brings
# frappe with it.
bench_toml="/home/$BENCH_USER/pilot/benches/$BENCH/bench.toml"
as_bench_user "sed -i '/^name = \"frappe\"\$/,/^\$/ s|^branch = .*|branch = \"$FRAPPE_VERSION\"|' '$bench_toml'"
# A silent miss would build the default branch while claiming this one, so check it took.
as_bench_user "grep -q '^branch = \"$FRAPPE_VERSION\"' '$bench_toml'"

as_bench_user "pilot --yes -b '$BENCH' init"
as_bench_user "pilot --yes -b '$BENCH' new-site '$SITE' --admin-password '$ADMIN_PASSWORD'"

# Auto bootstrapping: the host comes up managed by Central but without its credential, so
# Pilot serves the pending screen and polls instance metadata until Central writes one.
# The aliases map the VM hostname Atlas assigns onto the local site and admin panel. Both
# go in before `setup production`, which is what renders them into nginx.
cat > /tmp/central.py <<'PYTHON'
import os

from pilot.config.central import HostnameAlias
from pilot.config.common import CommonConfig
from pilot.utils import benches_dir

wildcard = os.environ["WILDCARD_DOMAIN"]
aliases = [
	HostnameAlias(
		type="admin",
		pattern=f"admin-vm-*.{wildcard}",
		target=os.environ["ADMIN_DOMAIN"],
		redirect=False,
	),
	HostnameAlias(
		type="site",
		pattern=f"site-*.{wildcard}",
		target=os.environ["SITE"],
		redirect=False,
	),
]

with CommonConfig.open(benches_dir()) as common:
	common.central.enabled = True
	common.central.bootstrapped = False
	common.central.hostname_aliases = aliases
PYTHON
chmod 644 /tmp/central.py
# The Pilot CLI is stdlib only and runs off the checkout, so its modules import from there.
as_bench_user "SITE='$SITE' ADMIN_DOMAIN='$ADMIN_DOMAIN' WILDCARD_DOMAIN='$WILDCARD_DOMAIN' PYTHONPATH=\$HOME/pilot python3 /tmp/central.py"
rm -f /tmp/central.py

# No TLS: the edge proxy terminates it, and these hostnames never resolve to this machine.
as_bench_user "pilot --yes -b '$BENCH' setup production"
as_bench_user "pilot --yes -b '$BENCH' build --force"

# A snapshot of a host whose nginx does not come back at boot serves nothing, and the
# enable verb only reaches production setup from v0.0.32-pre-alpha.
systemctl is-enabled nginx > /dev/null

# The hostnames below never resolve anywhere, so `--resolve` points them at this machine
# and nothing leaves it. Gunicorn and the workers take a moment to answer after production
# setup, so each probe waits rather than reading one cold start as a broken image.
probe() {
	host="$1"
	path="$2"
	for _ in $(seq 1 "$PROBE_ATTEMPTS"); do
		if body="$(curl -fsS -m 20 --resolve "$host:80:127.0.0.1" "http://$host$path")"; then
			echo "$body"
			return 0
		fi
		sleep "$PROBE_DELAY"
	done

	echo "$host$path never answered" >&2
	return 1
}

# Any label under the wildcard zone matches the alias, so this proves the vhost renders.
probe "site-verify.$WILDCARD_DOMAIN" /api/method/ping > /dev/null

# Pending is the whole point: the alias resolves and the host is waiting on Central.
bootstrap="$(probe "admin-vm-verify.$WILDCARD_DOMAIN" /api/v1/bootstrap)"
case "$bootstrap" in
	*'"pending"'*) ;;
	*) echo "This host is not awaiting a Central credential: $bootstrap" >&2; exit 1 ;;
esac

# Build litter only.
swapoff /swapfile
rm -f /swapfile
as_bench_user "yarn cache clean" > /dev/null 2>&1 || true
rm -rf "/home/$BENCH_USER/.cache" "/home/$BENCH_USER/.npm"
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
journalctl --vacuum-size=10M > /dev/null 2>&1 || true
