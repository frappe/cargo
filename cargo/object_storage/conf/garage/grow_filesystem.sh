#!/bin/bash
# Grows the filesystem under Garage's data directory to fill its disk, after Atlas grew the disk.
# Atlas disks are ext4 with no partition table, and the running guest already sees the new
# size, so resize2fs is all it takes. Mounted and online; does nothing when already grown.
set -euo pipefail

: "${DATA_DIR:?DATA_DIR is required}"

# The root mount reads as /dev/root, so the device is found by its number.
mount_point="$(findmnt --noheadings --output TARGET --target "$DATA_DIR")"
device="/dev/$(basename "$(readlink -f "/sys/dev/block/$(mountpoint --fs-devno "$mount_point")")")"

resize2fs "$device"
df --block-size=1 --output=size,avail "$DATA_DIR"
