#!/bin/bash
# Runs over SSH on an Atlas VM. Its exit status is the build's, so no status file. Base
# starts from bare Ubuntu and builds the bench, Site starts from Base and adds a bare site,
# and Apps starts from Site and installs one signup app on the site.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
: "${VERSION:?VERSION is required}"
: "${WILDCARD_DOMAIN:?WILDCARD_DOMAIN is required}"
: "${FRAPPE_BRANCH:?FRAPPE_BRANCH is required}"
: "${FRAPPE_COMMIT:?FRAPPE_COMMIT is required}"
: "${PROXY_SUBNET:?PROXY_SUBNET is required}"
: "${DOMAIN_PROVIDER:?DOMAIN_PROVIDER is required}"
: "${IMAGE_TYPE:?IMAGE_TYPE is required}"

# Every image carries the same bench and site. Both are reached through a hostname alias,
# so neither has to be unique or to resolve anywhere.
BENCH="default-bench"
SITE="site.local"
ADMIN_DOMAIN="admin.local"
PREWARM_SCRIPT="/usr/local/lib/pilot/prewarm-frappe.sh"
PREWARM_UNIT="/etc/systemd/system/pilot-prewarm.service"
PREWARM_MARKER="/var/lib/pilot/prewarm-pending"
PROVIDER_SCRIPT="/usr/bin/bench-domain-provider"
MEMORY_SCRIPT="/usr/local/lib/pilot/vm-memory.sh"
MEMORY_UNIT="/etc/systemd/system/pilot-vm-memory.service"
GROW_UNIT="/etc/systemd/system/pilot-grow-file-system@.service"
GROW_RULE="/etc/udev/rules.d/90-pilot-grow-file-system.rules"
VM_SWAP_FILE="/var/lib/pilot/swap"

BENCH_USER="${BENCH_USER:-frappe}"
BENCH_UID="${BENCH_UID:-1000}"
BENCH_GID="${BENCH_GID:-1000}"
VM_SWAP_SIZE="${VM_SWAP_SIZE:-1G}"
PROBE_ATTEMPTS="${PROBE_ATTEMPTS:-30}"
PROBE_DELAY="${PROBE_DELAY:-5}"
# One `app repo branch commit` line per app, in install order: what an app requires comes first.
REQUIRED_APPS="${REQUIRED_APPS:-}"

case "$IMAGE_TYPE" in
Base | Site) [ -z "$REQUIRED_APPS" ] || { echo "A $IMAGE_TYPE image installs no apps" >&2; exit 1; } ;;
Apps) [ -n "$REQUIRED_APPS" ] || { echo "An Apps image needs REQUIRED_APPS" >&2; exit 1; } ;;
*) echo "Unknown IMAGE_TYPE: $IMAGE_TYPE" >&2; exit 1 ;;
esac

INSTALLER="https://raw.githubusercontent.com/frappe/pilot/${VERSION}/install.sh"

# Pilot wants an upper, a lower, a digit and a symbol. Nothing reads this again.
# `head` reads the device directly: a producer piped into it dies of SIGPIPE under `pipefail`.
ADMIN_PASSWORD="$(head -c 18 /dev/urandom | base64)aA1#"

as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

# Pilot checks site and admin domains against the provider's wildcard domains, which the .local
# names reached through an alias never match. The parent image carries it, so remove it until the end.
rm -f "$PROVIDER_SCRIPT"

BENCH_PATH="/home/$BENCH_USER/pilot/benches/$BENCH"

# A Base build that boots on an earlier Base image finds the bench there. It moves Pilot to the
# newest release and Frappe to the commit; the host setup below is already in the image.
if [ "$IMAGE_TYPE" = Base ] && [ -d "$BENCH_PATH" ]; then
	as_bench_user "pilot --yes -b '$BENCH' admin upgrade"
	as_bench_user "pilot --yes -b '$BENCH' init --no-dev --commit '$FRAPPE_COMMIT'"
# The Base image already holds the user, Pilot and the bench, so only Base builds them.
elif [ "$IMAGE_TYPE" = Base ]; then
	# Started now, so installs and migrations next to MariaDB swap to the file every VM uses.
	install -d -m 755 "$(dirname "$MEMORY_SCRIPT")"
	cat > "$MEMORY_SCRIPT" <<MEMORY_SCRIPT
#!/bin/bash
set -u

swap="$VM_SWAP_FILE"

# The kernel holds about 5% of memory free for huge pages, which nothing here uses.
echo never > /sys/kernel/mm/transparent_hugepage/enabled

# Swapped pages stay compressed in memory before they reach the disk. MariaDB's unit keeps it out of swap.
modprobe zstd 2> /dev/null && echo zstd > /sys/module/zswap/parameters/compressor
echo 20 > /sys/module/zswap/parameters/max_pool_percent
echo Y > /sys/module/zswap/parameters/enabled

# Every image carries the file its build machine made. This makes it if it is missing.
if [ ! -e "\$swap" ]; then
	install -d -m 755 "\$(dirname "\$swap")"
	if ! { fallocate -l "$VM_SWAP_SIZE" "\$swap" && chmod 600 "\$swap" && mkswap -q "\$swap"; }; then
		rm -f "\$swap"
		logger -t pilot-vm-memory "Could not create the swap file"
		exit 0
	fi
fi
swapon --show=NAME --noheadings | grep -qx "\$swap" || swapon "\$swap"
MEMORY_SCRIPT
	chmod 755 "$MEMORY_SCRIPT"

	cat > "$MEMORY_UNIT" <<MEMORY_UNIT
[Unit]
Description=Swap and memory settings for a small VM
DefaultDependencies=no
After=local-fs.target systemd-modules-load.service
Before=swap.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$MEMORY_SCRIPT

[Install]
WantedBy=swap.target
MEMORY_UNIT

	systemctl daemon-reload
	systemctl enable --now --quiet "$(basename "$MEMORY_UNIT")"

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

	curl -fsSL "$INSTALLER" | bash
	# Pilot asks for the deprecated zone aliases as the bench user, which cannot sudo apt.
	apt-get install -y tzdata-legacy
	# Get app also requires this to be installed.
	apt-get install -y cron
	# Pilot releases before fix-build-memory-stdlib import psutil on the host's python3 to cap asset builds.
	# Remove once those releases are retired.
	apt-get install -y python3-psutil
	as_bench_user "curl -fsSL '$INSTALLER' | bash"

	# Set here, not at `setup production`: the alias below only renders for a domain the bench claims.
	as_bench_user "pilot --yes new '$BENCH' --database mariadb --admin-domain '$ADMIN_DOMAIN' --admin-password '$ADMIN_PASSWORD'"

	# `new` writes bench.toml; `init` clones the framework app on the branch it names, then
	# checks out the commit on that branch. Pilot finds the commit's prebuilt assets by branch.
	bench_toml="$BENCH_PATH/bench.toml"
	as_bench_user "sed -i '/^name = \"frappe\"\$/,/^\$/ s|^branch = .*|branch = \"$FRAPPE_BRANCH\"|' '$bench_toml'"
	# A silent miss would build the default branch, so check the edit took.
	as_bench_user "grep -q '^branch = \"$FRAPPE_BRANCH\"' '$bench_toml'"
	as_bench_user "pilot --yes -b '$BENCH' init --no-dev --commit '$FRAPPE_COMMIT'"

	# A Firecracker guest has no screen, multipath storage, modem, removable disk or firmware to update.
	for unit in fwupd.service fwupd-refresh.timer multipathd.socket multipathd.service ModemManager.service udisks2.service getty@tty1.service; do
		systemctl disable --now "$unit" > /dev/null 2>&1 || true
	done
	systemctl mask --quiet fwupd.service fwupd-refresh.timer
fi

if [ "$IMAGE_TYPE" = Base ]; then
	# The installer and `admin upgrade` both take the newest release, which may have moved past VERSION.
	installed=$(cat "/home/$BENCH_USER/pilot/VERSION")
	[ "$installed" = "$VERSION" ] || { echo "Pilot is at $installed, not $VERSION" >&2; exit 1; }

	checked_out=$(as_bench_user "git -C '$BENCH_PATH/apps/frappe' rev-parse HEAD")
	[ "$checked_out" = "$FRAPPE_COMMIT" ] || { echo "Frappe is at $checked_out, not $FRAPPE_COMMIT" >&2; exit 1; }
fi

# A Base build from Ubuntu starts the VM swap above; every other build starts it at boot. Each build needs it on a 1 GB machine.
swapon --show=NAME --noheadings | grep -qx "$VM_SWAP_FILE"

# cloud-init grows the root file system only at boot. A warm start or a live resize grows the
# disk of a running guest, so grow the file system when the kernel reports the new size. Every
# build writes this, so an image built on an earlier Base image has it too.
cat > "$GROW_UNIT" <<GROW_UNIT
[Unit]
Description=Grow the file system on /dev/%i to fill its disk

[Service]
Type=oneshot
ExecStart=/usr/sbin/resize2fs /dev/%i
GROW_UNIT

cat > "$GROW_RULE" <<GROW_RULE
ACTION=="change", SUBSYSTEM=="block", KERNEL=="vda", ENV{RESIZE}=="1", RUN+="/usr/bin/systemctl --no-block start pilot-grow-file-system@%k.service"
GROW_RULE

if [ "$IMAGE_TYPE" = Site ]; then
	as_bench_user "pilot --yes -b '$BENCH' new-site '$SITE' --admin-password '$ADMIN_PASSWORD'"
fi

if [ "$IMAGE_TYPE" = Apps ]; then
	apps=()
	# The branch lets Pilot download the assets the app's CI built for the commit.
	while read -r app repo branch commit; do
		as_bench_user "pilot --yes -b '$BENCH' get-app '$repo' --branch '$branch' --commit '$commit'"
		apps+=("$app")
	done <<< "$REQUIRED_APPS"
	as_bench_user "pilot --yes -b '$BENCH' install-app '$SITE' ${apps[*]}"
fi

# No TLS: the edge proxy terminates it, and these hostnames never resolve to this machine.
as_bench_user "pilot --yes -b '$BENCH' setup production"
# as_bench_user "pilot --yes -b '$BENCH' build --force"

# A snapshot of a host whose nginx does not come back at boot serves nothing.
systemctl is-enabled nginx > /dev/null

# `ping` proves the workers and the database without reading a built asset. It goes to the
# loopback upstream, because nginx serves the Central pending screen once a Base image has
# run `setup central`, and the wait absorbs the worker cold start.
probe_site() {
	for _ in $(seq 1 "$PROBE_ATTEMPTS"); do
		if curl -fsS -m 20 -H "Host: $SITE" "http://127.0.0.1:8000/api/method/ping" | grep -q pong; then
			return 0
		fi
		sleep "$PROBE_DELAY"
	done

	echo "$SITE never answered /api/method/ping" >&2
	return 1
}

# A siteless image has nothing to answer the probe, so the nginx check above is all it promises.
if [ "$IMAGE_TYPE" != Base ]; then
	probe_site
fi

# This hands the host to Central and writes the hostname aliases. Site runs it again for
# the site alias, which Base had no site to write. It rewrites nginx itself.
as_bench_user "pilot --yes -b '$BENCH' setup central --admin-pattern 'admin-vm-*.$WILDCARD_DOMAIN' --site-pattern '*.$WILDCARD_DOMAIN'"

# The admin API answers on its own Gunicorn bind. The prewarm reaches it there, past nginx,
# for the same reason it reaches the site on the loopback upstream.
ADMIN_UPSTREAM="http://$(sed -n 's/^bind = "\(.*\)"$/\1/p' "$BENCH_PATH/config/admin-gunicorn.conf.py")"
[ "$ADMIN_UPSTREAM" != "http://" ]

# Atlas boots a separate guest for five minutes before it captures the warm image. The
# Central pending screen would intercept nginx during that boot, so warm the Frappe web
# worker through its loopback upstream instead. The authenticated warmup logs out before
# capture, so its Administrator session is not shared by warm-started machines.
install -d -m 755 "$(dirname "$PREWARM_SCRIPT")"
cat > "$PREWARM_SCRIPT" <<PREWARM_SCRIPT
#!/bin/bash
set -u

site="$SITE"
bench="$BENCH"
image_type="$IMAGE_TYPE"
upstream="http://127.0.0.1:8000"
admin="$ADMIN_UPSTREAM"
bench_user="$BENCH_USER"
marker="$PREWARM_MARKER"
attempts=12
delay=5

if [ ! -e "\$marker" ]; then
	exit 0
fi

# The password never reaches disk, and a siteless image never reads it.
password="\$(head -c 24 /dev/urandom | base64 | tr -d '\\n')aA1#"

set_temporary_password() {
	su - "\$bench_user" -c "pilot --site '\$site' set-password Administrator '\$password'"
}

request() {
	local path="\$1"
	local expected="\$2"
	local body status
	body="\$(mktemp)"
	status="\$(curl -sS -m 10 -o "\$body" -w '%{http_code}' -H "Host: \$site" "\$upstream\$path" || true)"

	case "\$expected" in
	pong)
		[ "\$status" = "200" ] && grep -q pong "\$body"
		;;
	ok)
		[ "\$status" = "200" ]
		;;
	esac
	local result=\$?
	rm -f "\$body"
	return "\$result"
}

login_and_warm_desk() {
	local body cookies status result
	body="\$(mktemp)"
	cookies="\$(mktemp)"
	result=1
	status="\$(curl -sS -m 10 -o "\$body" -c "\$cookies" -w '%{http_code}' -X POST \\
		-H 'Content-Type: application/json' -H 'Accept: application/json' -H "Host: \$site" \\
		--data "{\\"usr\\":\\"Administrator\\",\\"pwd\\":\\"\$password\\"}" \\
		"\$upstream/api/method/login" || true)"
	if [ "\$status" = "200" ] && grep -q 'Logged In' "\$body"; then
		status="\$(curl -sS -m 10 -o "\$body" -b "\$cookies" -w '%{http_code}' -H "Host: \$site" "\$upstream/desk" || true)"
		[ "\$status" = "200" ] && result=0
	fi
	curl -sS -m 10 -o /dev/null -b "\$cookies" -X POST -H "Host: \$site" "\$upstream/api/method/logout" || true
	rm -f "\$body" "\$cookies"
	return "\$result"
}

# The admin is socket activated, so the first caller after boot pays its whole import.
# Warming it here leaves it started in the snapshot. The open probe starts the worker; a
# throwaway site token then warms the authenticated read path, which is what a caller
# reaches for first. That token carries no jti, so it registers no session and the mint
# leaves nothing behind.
warm_admin() {
	local status token
	status="\$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "\$admin/api/v1/auth/session" || true)"
	[ "\$status" = "200" ] || return 1

	# The read path below is per site, so a siteless image stops at the open probe.
	[ "\$image_type" != Base ] || return 0

	token="\$(su - "\$bench_user" -c "pilot -b '\$bench' admin issue-site-token '\$site' --ttl 60" | tail -n 1)"
	[ -n "\$token" ] || return 1

	status="\$(curl -sS -m 30 -o /dev/null -w '%{http_code}' \\
		-H "Authorization: Bearer \$token" "\$admin/api/v1/sites/\$site" || true)"
	[ "\$status" = "200" ]
}

warm_site() {
	[ "\$image_type" != Base ] || return 0
	request "/api/method/ping" pong && request / ok && request /login ok && login_and_warm_desk
}

password_is_set=false
for _ in \$(seq 1 "\$attempts"); do
	if [ "\$image_type" != Base ] && [ "\$password_is_set" = false ]; then
		if ! set_temporary_password; then
			sleep "\$delay"
			continue
		fi
		password_is_set=true
	fi

	# The marker makes this a one-time action: Atlas captures its removal with the warm
	# guest, so resumed tenants do not reset Administrator on boot.
	rm -f "\$marker"
	sync

	if warm_site && warm_admin; then
		logger -t pilot-prewarm "Frappe is warm"
		exit 0
	fi
	sleep "\$delay"
done

logger -t pilot-prewarm "Frappe did not warm before the deadline"
exit 0
PREWARM_SCRIPT
chmod 755 "$PREWARM_SCRIPT"
install -d -m 755 "$(dirname "$PREWARM_MARKER")"
install -m 600 /dev/null "$PREWARM_MARKER"

cat > "$PREWARM_UNIT" <<PREWARM_UNIT
[Unit]
Description=Warm Frappe before an Atlas memory snapshot
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PREWARM_SCRIPT
TimeoutStartSec=4min
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
PREWARM_UNIT

systemctl daemon-reload
systemctl enable --quiet "$(basename "$PREWARM_UNIT")"

# Pilot asks this provider where a custom domain routes. Cargo bakes the zone and the
# proxy subnet into it, because neither changes for a VM of this image.
printf '%s' "$DOMAIN_PROVIDER" > "$PROVIDER_SCRIPT"
chmod 755 "$PROVIDER_SCRIPT"

# A silent miss would ship a provider that routes nothing.
"$PROVIDER_SCRIPT" wildcard-domains | grep -q "$WILDCARD_DOMAIN"
"$PROVIDER_SCRIPT" proxy-servers | grep -q "$PROXY_SUBNET"

# Build litter only.
as_bench_user "yarn cache clean" > /dev/null 2>&1 || true
rm -rf "/home/$BENCH_USER/.cache" "/home/$BENCH_USER/.npm"
apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
journalctl --vacuum-size=10M > /dev/null 2>&1 || true
# Lets `su - frappe` reach its user services. Quoted so the uid resolves at login, not as root here.
cat >> "/home/$BENCH_USER/.bashrc" <<'USER_BUS'
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
USER_BUS
chown "$BENCH_USER:" "/home/$BENCH_USER/.bashrc"
