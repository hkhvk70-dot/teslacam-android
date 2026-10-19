#!/system/bin/sh
# fsck_img.sh <image.img> - check/repair a FAT32 disk image.
# Handles both layouts:
#   * superfloppy (FAT32 boot sector at offset 0)   -> fsck the file directly
#   * MBR partitioned (FAT32 partition at LBA N)    -> attach loop device with offset
IMG="$1"
[ -n "$IMG" ] || { echo "usage: fsck_img.sh <image>"; exit 1; }
[ -f "$IMG" ] || { echo "missing: $IMG"; exit 1; }

sig=$(dd if="$IMG" bs=1 skip=82 count=5 2>/dev/null)
DEV="$IMG"
if [ "$sig" != "FAT32" ]; then
  lba=$(od -An -tu4 -j 454 -N 4 "$IMG" 2>/dev/null | tr -d ' ')
  case "$lba" in ''|*[!0-9]*) echo "cannot parse MBR"; exit 3 ;; esac
  off=$(( lba * 512 ))
  DEV=$(losetup -f -s -o "$off" "$IMG" 2>/dev/null)
  if [ -z "$DEV" ]; then
    echo "losetup failed (no free loop device?)"
    exit 3
  fi
  echo "# partitioned image: partition1 lba=$lba -> $DEV"
fi

/system/bin/fsck_msdos -p -f "$DEV" 2>&1
rc=$?

if [ "$DEV" != "$IMG" ]; then
  losetup -d "$DEV" >/dev/null 2>&1
fi
exit $rc
