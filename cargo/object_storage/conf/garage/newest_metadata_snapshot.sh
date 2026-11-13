#!/bin/bash
# Runs over SSH on one node. Prints `snapshot=<name>` for the newest snapshot Garage has
# finished, or nothing. A snapshot Garage still has open is being written and is skipped.
set -euo pipefail

: "${METADATA_DIR:?METADATA_DIR is required}"
SNAPSHOTS="$METADATA_DIR/snapshots"

open_files=""
if pid=$(pidof garage); then
	open_files=$(find "/proc/$pid/fd" -mindepth 1 -exec readlink {} + 2>/dev/null || true)
fi

for name in $(ls -1 "$SNAPSHOTS" 2>/dev/null | sort -r); do
	[ -f "$SNAPSHOTS/$name/db.lmdb" ] || continue
	if grep -qF "$SNAPSHOTS/$name/" <<<"$open_files"; then
		continue
	fi

	echo "snapshot=$name"
	exit 0
done
