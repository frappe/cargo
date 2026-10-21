#!/bin/bash
# Grows the filesystem under Garage's data directory to fill its disk, after Atlas grew the disk.
# Atlas disks are ext4 with no partition table, so resize2fs is all it takes. Mounted and online;
# does nothing when already grown.
set -euo pipefail

: "${DATA_DIR:?DATA_DIR is required}"
: "${DISK_MIB:?DISK_MIB is required}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"

# The root mount reads as /dev/root, so the device is found by its number.
mount_point="$(findmnt --noheadings --output TARGET --target "$DATA_DIR")"
device="/dev/$(basename "$(readlink -f "/sys/dev/block/$(mountpoint --fs-devno "$mount_point")")")"

# Atlas reports the requested size as soon as Metal accepts it; the guest sees it later.
wanted=$((DISK_MIB * 1024 * 1024))
deadline=$((SECONDS + WAIT_SECONDS))
until [ "$(blockdev --getsize64 "$device")" -ge "$wanted" ]; do
	if [ "$SECONDS" -ge "$deadline" ]; then
		echo "$device is $(blockdev --getsize64 "$device") bytes after ${WAIT_SECONDS}s, wanted $wanted." >&2
		exit 1
	fi
	sleep 5
done

resize2fs "$device"
df --block-size=1 --output=size,avail "$DATA_DIR"
