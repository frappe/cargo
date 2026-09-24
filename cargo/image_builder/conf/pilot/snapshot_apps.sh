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

as_bench_user() {
	su - "$BENCH_USER" -c "$1"
}

frappe_site() {
	as_bench_user "pilot --yes -b '$BENCH' frappe --site '$SITE' $1"
}

sorted_words() {
	printf '%s\n' $1 | sort
}

case "$ACTION" in
install)
	# Pilot turns an app back on when the site holds it disabled, and installs it otherwise.
	as_bench_user "pilot --yes -b '$BENCH' install-app '$SITE' $APPS"
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
	for app in $(printf '%s\n' $APPS | tac); do
		frappe_site "uninstall-app $app --yes --no-backup"
	done
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
