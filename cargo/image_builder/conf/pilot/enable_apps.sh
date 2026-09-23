#!/bin/bash
# Runs over SSH on the build machine before each snapshot. Its exit status is the snapshot's.
set -euo pipefail

: "${INSTALLED_APPS:?INSTALLED_APPS is required}"
ENABLED_APPS="${ENABLED_APPS:-}"

# The same bench, site and user the provision script made.
BENCH="default-bench"
SITE="site.local"
BENCH_USER="${BENCH_USER:-frappe}"
PREWARM_MARKER="/var/lib/pilot/prewarm-pending"

frappe_site() {
	su - "$BENCH_USER" -c "pilot --yes -b '$BENCH' frappe --site '$SITE' $1"
}

is_enabled() {
	case " $ENABLED_APPS " in
	*" $1 "*) return 0 ;;
	*) return 1 ;;
	esac
}

# Frappe refuses to turn an app off while an app still on requires it, so the last
# installed goes first. Turning off an app already off changes nothing.
for app in $(printf '%s\n' $INSTALLED_APPS | tac); do
	if ! is_enabled "$app"; then
		frappe_site "disable-app $app"
	fi
done

# It also refuses to turn one on before what it requires, so install order.
for app in $INSTALLED_APPS; do
	if is_enabled "$app"; then
		frappe_site "enable-app $app"
	fi
done

# Workers started under the previous snapshot's apps would carry them into this one.
su - "$BENCH_USER" -c "pilot --yes -b '$BENCH' restart"

# The guest Atlas boots to warm this snapshot runs the prewarm only while the marker exists.
install -m 600 /dev/null "$PREWARM_MARKER"
