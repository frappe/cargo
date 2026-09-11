#!/bin/bash
# What a finished Cargo host must look like. Runs inside the container.
set -uo pipefail

SITE="${SITE:-cargo.localhost}"
BENCH="${BENCH:-cargo}"
BENCH_USER="${BENCH_USER:-frappe}"
BENCH_PATH="/home/$BENCH_USER/pilot/benches/$BENCH"

passed=0
failed=0

check() {
	local label="$1"
	shift
	if "$@" > /tmp/check.out 2>&1; then
		echo "  ok    $label"
		passed=$((passed + 1))
	else
		echo "  FAIL  $label"
		sed 's/^/          /' /tmp/check.out | tail -5
		failed=$((failed + 1))
	fi
}

as_bench() {
	su - "$BENCH_USER" -c "$1"
}

# One connection to the site, reused by every settings check below.
site_query() {
	as_bench "cd $BENCH_PATH/sites && ../env/bin/python -m frappe.utils.bench_helper frappe --site $SITE execute frappe.client.get_value --kwargs \"{'doctype': 'Cargo Settings', 'fieldname': '$1'}\""
}

settings_is() {
	site_query "$1" | grep -q "$2"
}

echo "Host"
check "bench user exists" id -u "$BENCH_USER"
check "pilot is installed" test -x "/home/$BENCH_USER/pilot/bin/pilot"
check "bench was initialised" test -x "$BENCH_PATH/env/bin/python"
check "frappe was cloned" test -d "$BENCH_PATH/apps/frappe"
check "cargo was downloaded" test -d "$BENCH_PATH/apps/cargo"
check "site was created" test -f "$BENCH_PATH/sites/$SITE/site_config.json"

cargo_is_installed() {
	as_bench "cd $BENCH_PATH/sites && ../env/bin/python -m frappe.utils.bench_helper frappe --site $SITE list-apps" | grep -q cargo
}

wizard_is_complete() {
	as_bench "cd $BENCH_PATH/sites && ../env/bin/python -m frappe.utils.bench_helper frappe --site $SITE execute frappe.is_setup_complete" | grep -q true
}

site_answers() {
	curl -fsS -H "Host: $SITE" http://127.0.0.1/api/method/ping | grep -q pong
}

units_are_loaded() {
	as_bench "systemctl --user list-units --type=service --no-legend" | grep -q .
}

echo "Site"
check "cargo is installed on the site" cargo_is_installed
check "setup wizard is complete" wizard_is_complete

echo "Cargo Settings"
check "central url was recorded" settings_is central_url "central.invalid"
check "jwks url was recorded" settings_is jwks_url "jwks"
check "atlas url was recorded" settings_is atlas_url "atlas.invalid"
check "proxy url was recorded" settings_is proxy_url "proxy.invalid"
check "region was recorded" settings_is region "e2e"
check "tenant zero was recorded" settings_is atlas_tenant_id "0"

echo "Production"
check "systemd units are loaded" units_are_loaded
check "nginx is running" systemctl is-active nginx
check "the site answers over nginx" site_answers

echo
echo "$passed passed, $failed failed"
exit $((failed > 0))
