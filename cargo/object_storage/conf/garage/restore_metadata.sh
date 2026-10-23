#!/bin/bash
# Runs over SSH on one node. Puts one of Garage's metadata snapshots in place of the live
# database, keeping the live one beside it so the swap can be undone by hand.
set -euo pipefail

: "${GARAGE_BINARY:?GARAGE_BINARY is required}"
: "${METADATA_DIR:?METADATA_DIR is required}"
: "${SNAPSHOT:?SNAPSHOT is required}"

# Garage copies LMDB into one compacted file, while the live database is a directory that
# holds data.mdb, so the file goes in as data.mdb rather than as the directory itself.
SOURCE="$METADATA_DIR/snapshots/$SNAPSHOT/db.lmdb"
LIVE="$METADATA_DIR/db.lmdb"
KEPT="$LIVE.bak-$(date -u +%Y%m%dT%H%M%SZ)"

if [ ! -f "$SOURCE" ]; then
	echo "$(hostname) has no snapshot $SNAPSHOT. It has:" >&2
	ls -1 "$METADATA_DIR/snapshots" >&2 2>/dev/null || echo "(no snapshots)" >&2
	exit 1
fi

systemctl stop garage

if [ -e "$LIVE" ]; then
	mv "$LIVE" "$KEPT"
	echo "kept the live database at $KEPT"
fi

install -d -m 755 "$LIVE"
install -m 600 "$SOURCE" "$LIVE/data.mdb"
systemctl start garage

for _ in $(seq 1 30); do
	if "$GARAGE_BINARY" status > /dev/null 2>&1; then
		echo "$(hostname) is serving from snapshot $SNAPSHOT"
		exit 0
	fi
	sleep 2
done

echo "garage did not come up within 60s on snapshot $SNAPSHOT; the old database is at $KEPT" >&2
systemctl status garage --no-pager --lines 30 >&2
exit 1
