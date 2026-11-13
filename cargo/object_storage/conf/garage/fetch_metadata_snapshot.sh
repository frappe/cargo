#!/bin/bash
# Runs over SSH on one node before Garage stops for a restore. Garage keeps only its two newest
# snapshots, so an older one is fetched from the node's own backup, never another node's. A
# missing backup fails here, while every node still serves.
set -euo pipefail

: "${METADATA_DIR:?METADATA_DIR is required}"
: "${SNAPSHOT:?SNAPSHOT is required}"
: "${BACKUP_ENDPOINT:?BACKUP_ENDPOINT is required}"
: "${BACKUP_REGION:?BACKUP_REGION is required}"
: "${BACKUP_PREFIX:?BACKUP_PREFIX is required}"
: "${TRANSFER_TIMEOUT:?TRANSFER_TIMEOUT is required}"

SOURCE="$METADATA_DIR/snapshots/$SNAPSHOT/db.lmdb"
if [ -f "$SOURCE" ]; then
	echo "$(hostname) still holds snapshot $SNAPSHOT"
	exit 0
fi

METADATA=http://169.254.169.254/latest
session=$(curl -fsS --max-time 5 -X PUT -H "X-metadata-token-ttl-seconds: 300" "$METADATA/api/token")
# Atlas may accept a metadata change before Metal applies it, so fresh keys can lag a little.
access_key=$(curl -fsS --max-time 5 --retry 30 --retry-delay 2 --retry-all-errors -H "X-metadata-token: $session" "$METADATA/meta-data/attributes/metadata_bucket_access_key")
secret_key=$(curl -fsS --max-time 5 --retry 30 --retry-delay 2 --retry-all-errors -H "X-metadata-token: $session" "$METADATA/meta-data/attributes/metadata_bucket_secret_key")
install -d -m 700 "$(dirname "$SOURCE")"
curl -fsS --max-time "$TRANSFER_TIMEOUT" --aws-sigv4 "aws:amz:$BACKUP_REGION:s3" -K - -o "$SOURCE.partial" \
	"${BACKUP_ENDPOINT%/}/$BACKUP_PREFIX/$SNAPSHOT/db.lmdb" <<<"user = \"$access_key:$secret_key\""
mv "$SOURCE.partial" "$SOURCE"
echo "fetched $BACKUP_PREFIX/$SNAPSHOT from the backup bucket"
