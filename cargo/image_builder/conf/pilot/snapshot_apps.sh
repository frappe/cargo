#!/bin/bash
# Runs over SSH on the build machine around each snapshot. Its exit status is the snapshot's.
set -euo pipefail

# install, disable or uninstall APPS on the site, or verify the site holds exactly APPS.
: "${ACTION:?ACTION is required}"
# Space separated, in install order: what an app requires comes before it.
: "${APPS:?APPS is required}"

# The same bench, site and user the provision script made.
BENCH="default-bench"
SITE="site.local"
BENCH_USER="${BENCH_USER:-frappe}"
PREWARM_MARKER="/var/lib/pilot/prewarm-pending"
SWAP_FILE="/swapfile"
SWAP_SIZE="${SWAP_SIZE:-1536M}"

as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

frappe_site() {
	as_bench_user "pilot --yes -b '$BENCH' frappe --site '$SITE' $1"
}

sorted_words() {
	printf '%s\n' $1 | sort
}

# Installing an app outgrows the build machine's memory, as the provision script's build did.
start_swap() {
	# A run that failed mid-install leaves its swap behind.
	stop_swap
	fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
	chmod 600 "$SWAP_FILE"
	mkswap -q "$SWAP_FILE"
	swapon "$SWAP_FILE"
}

# Swap on the disk would land in the snapshot, so it goes before this script returns.
stop_swap() {
	if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
		swapoff "$SWAP_FILE"
	fi
	rm -f "$SWAP_FILE"
}

case "$ACTION" in
install)
	# Pilot turns an app back on when the site holds it disabled, and installs it otherwise.
	start_swap
	as_bench_user "pilot --yes -b '$BENCH' install-app '$SITE' $APPS"
	stop_swap
	;;
disable)
	# Frappe runs an app's disable hooks again even when the app is already off, so skip those.
	disabled="$(frappe_site list-apps | awk '/\(disabled\)$/ {print $1}')"

	# Frappe refuses to turn off an app another still needs, so the last installed goes first.
	for app in $(printf '%s\n' $APPS | tac); do
		if printf '%s\n' $disabled | grep -qx "$app"; then
			continue
		fi
		frappe_site "disable-app $app"
	done
	;;
uninstall)
	# Frappe's own command: Pilot's uninstall-app would also delete the app from the bench.
	start_swap
	for app in $(printf '%s\n' $APPS | tac); do
		frappe_site "uninstall-app $app --yes --no-backup"
	done
	stop_swap
	;;
verify)
	installed="$(frappe_site list-apps | awk 'NF {print $1}')"
	if [ "$(sorted_words "$installed")" != "$(sorted_words "$APPS")" ]; then
		echo "$SITE holds" $installed "but should hold only" $APPS >&2
		exit 1
	fi
	exit 0
	;;
*)
	echo "Unknown ACTION: $ACTION" >&2
	exit 1
	;;
esac

# Workers started under the previous apps would carry them into the snapshot.
as_bench_user "pilot --yes -b '$BENCH' restart"

# The guest Atlas boots to warm this snapshot runs the prewarm only while the marker exists.
install -m 600 /dev/null "$PREWARM_MARKER"
