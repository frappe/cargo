#!/bin/bash
# Runs over SSH on one node. Uploads one finished metadata snapshot to the offsite bucket under
# its own name. Garage prunes the local copies itself. The bucket keys come from this machine's
# metadata.
set -euo pipefail

: "${METADATA_DIR:?METADATA_DIR is required}"
: "${BACKUP_ENDPOINT:?BACKUP_ENDPOINT is required}"
: "${BACKUP_REGION:?BACKUP_REGION is required}"
: "${BACKUP_PREFIX:?BACKUP_PREFIX is required}"
: "${TRANSFER_TIMEOUT:?TRANSFER_TIMEOUT is required}"
: "${SNAPSHOT:?SNAPSHOT is required}"

METADATA=http://169.254.169.254/latest
SOURCE="$METADATA_DIR/snapshots/$SNAPSHOT/db.lmdb"

if [ ! -f "$SOURCE" ]; then
	echo "$(hostname) has no snapshot $SNAPSHOT" >&2
	exit 1
fi

session=$(curl -fsS --max-time 5 -X PUT -H "X-metadata-token-ttl-seconds: 300" "$METADATA/api/token")

read_attribute() {
	# Atlas may accept a metadata change before Metal applies it, so a fresh key can lag a little.
	for _ in $(seq 1 30); do
		if curl -fsS --max-time 5 -H "X-metadata-token: $session" "$METADATA/meta-data/attributes/$1"; then
			return 0
		fi
		sleep 2
	done
	echo "no $1 in this machine metadata" >&2
	return 1
}

access_key=$(read_attribute metadata_bucket_access_key)
secret_key=$(read_attribute metadata_bucket_secret_key)

key="$BACKUP_PREFIX/$SNAPSHOT/db.lmdb"
# Keys go through a config on stdin so they never show in the process list.
curl -fsS --max-time "$TRANSFER_TIMEOUT" --aws-sigv4 "aws:amz:$BACKUP_REGION:s3" -K - \
	-T "$SOURCE" "${BACKUP_ENDPOINT%/}/$key" <<<"user = \"$access_key:$secret_key\""
echo "uploaded $SNAPSHOT as $key"
