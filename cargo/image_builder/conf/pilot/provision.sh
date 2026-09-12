#!/bin/bash
# Runs over SSH on a bare Atlas VM. Its exit status is the build's, so no status file.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${VERSION:?VERSION is required}"
: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${FRAPPE_VERSION:?FRAPPE_VERSION is required}"

# Every image carries the same bench and site. Both are reached through a hostname alias,
# so neither has to be unique or to resolve anywhere.
BENCH="default-bench"
SITE="site.local"
ADMIN_DOMAIN="admin.local"

BENCH_USER="${BENCH_USER:-frappe}"
BENCH_UID="${BENCH_UID:-1000}"
BENCH_GID="${BENCH_GID:-1000}"
SWAP_SIZE="${SWAP_SIZE:-1536M}"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-30}"
PROBE_DELAY="${PROBE_DELAY:-5}"

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/${VERSION}/install.sh"

# Pilot wants an upper, a lower, a digit and a symbol. Nothing reads this again.
# `head` reads the device directly: a producer piped into it dies of SIGPIPE under `pipefail`.
ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64)aA1#"

# Assets need more memory than the image is baked with. Cleanup removes the file before the snapshot.
fallocate -l "$SWAP_SIZE" /swapfile
chmod 600 /swapfile
mkswap -q /swapfile
swapon /swapfile

# Cloud images ship their own first user. Clear every regular user out so the bench user
# owns the baked files at ids the image can rely on.
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

# Pilot refuses to install as root: the first pass lays down the system stack, the second
# runs as the bench user it created.
as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

curl -fsSL "$INSTALLER" | bash
as_bench_user "curl -fsSL '$INSTALLER' | bash"

# Set here, not at `setup production`: the alias below only renders for a domain the bench claims.
as_bench_user "pilot --yes new '$BENCH' --database mariadb --admin-domain '$ADMIN_DOMAIN' --admin-password '$ADMIN_PASSWORD'"

# `new` writes bench.toml; `init` clones the framework app it names. The branch is config,
# not a flag, so it is set in between.
bench_toml="/home/$BENCH_USER/pilot/benches/$BENCH/bench.toml"
as_bench_user "sed -i '/^name = \"frappe\"\$/,/^\$/ s|^branch = .*|branch = \"$FRAPPE_VERSION\"|' '$bench_toml'"
# A silent miss would build the default branch, so check the edit took.
as_bench_user "grep -q '^branch = \"$FRAPPE_VERSION\"' '$bench_toml'"

as_bench_user "pilot --yes -b '$BENCH' init"
as_bench_user "pilot --yes -b '$BENCH' new-site '$SITE' --admin-password '$ADMIN_PASSWORD'"

# No TLS: the edge proxy terminates it, and these hostnames never resolve to this machine.
as_bench_user "pilot --yes -b '$BENCH' setup production"
# as_bench_user "pilot --yes -b '$BENCH' build --force"

# A snapshot of a host whose nginx does not come back at boot serves nothing.
systemctl is-enabled nginx > /dev/null

# `ping` proves nginx, the workers and the database without reading a built asset.
# `--resolve` keeps it on this machine, and the wait absorbs the worker cold start.
probe_site() {
	for _ in $(seq 1 "$PROBE_ATTEMPTS"); do
		if curl -fsS -m 20 --resolve "$SITE:80:127.0.0.1" "http://$SITE/api/method/ping" | grep -q pong; then
			return 0
		fi
		sleep "$PROBE_DELAY"
	done

	echo "$SITE never answered /api/method/ping" >&2
	return 1
}

probe_site

# Last: this hands the host to Central, and the pending screen then replaces the site
# probed above. It rewrites nginx itself now that production is up.
as_bench_user "pilot --yes -b '$BENCH' setup central --admin-pattern 'admin-vm-*.$WILDCARD_DOMAIN' --site-pattern 'site-*.$WILDCARD_DOMAIN'"

# Build litter only.
swapoff /swapfile
rm -f /swapfile
as_bench_user "yarn cache clean" > /dev/null 2>&1 || true
rm -rf "/home/$BENCH_USER/.cache" "/home/$BENCH_USER/.npm"
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
journalctl --vacuum-size=10M > /dev/null 2>&1 || true
