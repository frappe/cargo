#!/bin/sh
# Shown at each SSH login by update-motd: the live slot and the commands to inspect the app.
upstream=/var/lib/deploy-agent/upstream.conf
slot=""
grep -q "127.0.0.1:8001;" "$upstream" 2>/dev/null && slot=blue
grep -q "127.0.0.1:8002;" "$upstream" 2>/dev/null && slot=green

echo
echo "Frappe Cloud push-to-deploy VM"
if [ -z "$slot" ]; then
	echo "  No release is live yet."
	slot="{blue,green}"
else
	image=$(sed -n 's/^IMAGE=//p' "/var/lib/deploy-agent/slots/$slot.env" 2>/dev/null)
	echo "  Live: app@$slot (container app-$slot) serves ${image:-an unknown image}"
fi
cat <<HELP

  Shell in the app     podman exec -it app-$slot sh
  App output           journalctl -u app@$slot -f
  Containers           podman ps
  Builds and releases  journalctl -u 'deploy-build-*' -u 'release-build-*' -e
  Deploy agent         journalctl -u deploy-agent -e

  Changes inside the container are lost on the next deploy. Push to the branch instead.

HELP
