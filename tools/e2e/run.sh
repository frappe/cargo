#!/bin/bash
# Run setup.sh on a bare Ubuntu container and check what it produced.
set -euo pipefail

IMAGE="cargo-e2e:latest"
NAME="cargo-e2e"
SITE="cargo.localhost"
ADMIN_DOMAIN="pilot.cargo.localhost"
BENCH="cargo"
# Both the site and pilot's admin panel are served by the one nginx on port 80; the Host
# header is what picks between them. HTTP_PORT is where that lands on your machine.
HTTP_PORT="${HTTP_PORT:-8200}"
PILOT_ADMIN_PASSWORD="E2ePanel#2026"
SITE_PASSWORD="E2eSite#2026"
KEEP=""

[ "${1:-}" = "--keep" ] && KEEP=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

cleanup() {
	[ -n "$KEEP" ] && { echo "Container '$NAME' left running."; return; }
	docker rm -f "$NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT

docker rm -f "$NAME" > /dev/null 2>&1 || true

echo "==> Building the host image"
docker build -q -t "$IMAGE" "$REPO_ROOT/tools/e2e"

# systemd needs a writable cgroup hierarchy and its own tmpfs mounts.
echo "==> Booting the host"
docker run -d --name "$NAME" \
	--privileged \
	--cgroupns=host \
	-v /sys/fs/cgroup:/sys/fs/cgroup:rw \
	--tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
	-p "$HTTP_PORT":80 \
	"$IMAGE" > /dev/null

echo "==> Waiting for systemd"
for _ in $(seq 1 60); do
	docker exec "$NAME" systemctl is-system-running --wait > /dev/null 2>&1 && break
	sleep 1
done
docker exec "$NAME" systemctl is-system-running || true

# The working tree, not the published branch: this exists to test what is unpushed.
# setup.sh clones whatever REPO points at, so the copy is committed on a throwaway
# branch and cloned from there -- otherwise uncommitted work is invisible to the run.
echo "==> Copying this working tree in"
BRANCH="e2e-under-test"
docker exec "$NAME" mkdir -p /opt/cargo-src
docker cp "$REPO_ROOT/." "$NAME:/opt/cargo-src/"
# The copy arrives owned by root, which git treats as untrusted.
in_src() {
	docker exec "$NAME" git -C /opt/cargo-src \
		-c safe.directory=/opt/cargo-src \
		-c user.email=e2e@cargo.test -c user.name=e2e "$@"
}

# pilot clones this as the bench user, which is a third owner again. A throwaway
# container, so every path is trusted rather than naming each one git objects to.
docker exec "$NAME" git config --system --add safe.directory "*"
docker exec "$NAME" chmod -R a+rX /opt/cargo-src

in_src checkout -q -B "$BRANCH"
in_src add -A
# --no-verify: the repo's own hooks came along in the copy and have no venv here.
in_src commit -qm "e2e: the working tree under test" --allow-empty --no-verify

echo "==> Running setup.sh"
docker exec \
	-e PILOT_ADMIN_PASSWORD="$PILOT_ADMIN_PASSWORD" \
	-e SITE_PASSWORD="$SITE_PASSWORD" \
	-e ADMIN_DOMAIN="$ADMIN_DOMAIN" \
	-e SITE="$SITE" \
	-e BENCH="$BENCH" \
	-e REPO=/opt/cargo-src \
	-e BRANCH="$BRANCH" \
	-e CENTRAL_URL=http://central.invalid \
	-e JWKS_URL=http://atlas.invalid/api/atlas/jwks.json \
	-e ATLAS_URL=http://atlas.invalid \
	-e CARGO_URL=http://cargo.invalid \
	-e CENTRAL_WEBHOOK_SECRET=e2e-webhook-secret \
	-e REGION=e2e \
	-e REGION_ID=1 \
	-e ATLAS_TOKEN=e2e-atlas-token \
	-e ATLAS_TENANT_ID=0 \
	-e PROXY_URL=http://proxy.invalid \
	-e PROXY_TOKEN=e2e-proxy-token \
	"$NAME" bash /opt/cargo-src/setup.sh

echo
echo "==> Checking what it built"
docker cp "$REPO_ROOT/tools/e2e/verify.sh" "$NAME:/opt/verify.sh"
# set -e would abort on a failed check before the URLs below are printed.
status=0
docker exec -e SITE="$SITE" -e BENCH="$BENCH" "$NAME" bash /opt/verify.sh || status=$?

echo
echo "Open in a browser -- .localhost resolves to 127.0.0.1 without touching /etc/hosts:"
echo "  Cargo site   http://$SITE:$HTTP_PORT"
echo "  Pilot admin  http://$ADMIN_DOMAIN:$HTTP_PORT  (password: $PILOT_ADMIN_PASSWORD)"
exit $status
