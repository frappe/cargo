#!/bin/bash
# Runs over SSH on a bare Atlas VM. Its exit status is the build's, so no status file.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${VERSION:?VERSION is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
FRAPPE_VERSION="${FRAPPE_VERSION:-}"
SITE="${SITE:-}"
BENCH="${BENCH:-pilot}"
BENCH_USER="${BENCH_USER:-frappe}"
BENCH_UID="${BENCH_UID:-1001}"
BENCH_GID="${BENCH_GID:-1001}"

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/${VERSION}/install.sh"

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

# `new` only writes bench.toml; `init` clones and installs the framework app it names. The
# branch is config, not a flag, so it is set in between (config/bench.py defaults to
# version-16). No get-app: the bench brings frappe with it.
as_bench_user "pilot --yes new '$BENCH' --database mariadb --admin-password '$ADMIN_PASSWORD'"

if [ -n "$FRAPPE_VERSION" ]; then
	bench_toml="/home/$BENCH_USER/pilot/benches/$BENCH/bench.toml"
	as_bench_user "sed -i '/^name = \"frappe\"\$/,/^\$/ s|^branch = .*|branch = \"$FRAPPE_VERSION\"|' '$bench_toml'"
	# A silent miss would build version-16 while claiming this branch, so check it took.
	as_bench_user "grep -q '^branch = \"$FRAPPE_VERSION\"' '$bench_toml'"
fi

as_bench_user "pilot --yes -b '$BENCH' init"

if [ -n "$SITE" ]; then
	as_bench_user "pilot --yes -b '$BENCH' new-site '$SITE' --admin-password '$ADMIN_PASSWORD'"
fi

# The snapshot should not carry build litter or this machine's identity.
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
truncate -s 0 /etc/machine-id
rm -f /root/.ssh/authorized_keys /etc/ssh/ssh_host_*
sync
