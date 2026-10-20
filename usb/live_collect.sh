#!/system/bin/sh
# live_collect.sh - 15 秒级的准实时快线：只搬 RecentClips，不扫事件、不做清理
# 判定"已收尾"：同一摄像头出现了更新的片段（说明上一条已关闭）
DATA=/data/teslacam
CONF=$DATA/msd.conf
IMG=${IMG:-/data/teslacam/cam_disk.img}
EV=$DATA/events
MNT=$DATA/mnt
ACT=/data/local/tmp/live_collect.act
PREV=$DATA/.live.prev
CUR=$DATA/.live.cur
[ -f "$CONF" ] && . "$CONF"

# ---- 挂载锁：与 collect.sh、维护共用同一把 ----
MOUNTED=""
LOOPDEV=""

# ---- 终止时清理自己挂的资源（stop 会 TERM 本进程）----
cleanup_mount() {
  [ -n "$MOUNTED" ] && umount "$MNT" 2>/dev/null
  [ -n "$MOUNTED" ] && umount -l "$MNT" 2>/dev/null
  [ -n "$LOOPDEV" ] && losetup -d "$LOOPDEV" >/dev/null 2>&1
}
trap 'cleanup_mount; exit 143' TERM INT
FLOCK=${FLOCK:-/data/data/com.termux/files/usr/bin/flock}
MOUNT_LOCK=${MOUNT_LOCK:-/data/local/tmp/teslacam-mount.lock}
if [ -z "$TC_MOUNT_LOCKED" ]; then
  if [ ! -x "$FLOCK" ]; then
    echo "ERROR: flock missing at $FLOCK" >> "$DATA/collect.log"
    exit 69
  fi
  TC_MOUNT_LOCKED=1 "$FLOCK" -n "$MOUNT_LOCK" -c "TC_MOUNT_LOCKED=1 sh '$0'"
  rc=$?
  [ $rc -eq 1 ] && exit 0
  exit $rc
fi

# ---- 停止屏障 ----
if [ -f "$DATA/state/stop_requested" ] || [ -f "$DATA/state/export_forbidden.json" ]; then
  echo "$(date '+%m-%d %H:%M:%S') blocked by control state (stop/hold), skip live collect" >> "$DATA/collect.log"
  exit 0
fi

: > "$ACT"; : > "$CUR"
[ -f "$IMG" ] || exit 1
mkdir -p "$EV/TeslaCam/RecentClips" "$MNT" 2>/dev/null

# 清理上次残留（挂载/loop）
umount "$MNT" 2>/dev/null
umount -l "$MNT" 2>/dev/null
for _l in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do losetup -d "$_l" >/dev/null 2>&1; done
sig=$(dd if="$IMG" bs=1 skip=82 count=5 2>/dev/null)
if [ "$sig" = "FAT32" ]; then
  OFF=0
else
  lba=$(od -An -tu4 -j 454 -N 4 "$IMG" 2>/dev/null | tr -d ' ')
  case "$lba" in ''|*[!0-9]*) exit 1 ;; esac
  OFF=$(( lba * 512 ))
fi

L=$(losetup -f -s -o "$OFF" "$IMG" 2>/dev/null)
case "$L" in /dev/block/loop*) ;; *) exit 1 ;; esac
mount -t vfat -o ro,utf8 "$L" "$MNT" 2>/dev/null || { losetup -d "$L" >/dev/null 2>&1; exit 1; }
MOUNTED=1; LOOPDEV="$L"

SRC="$MNT/TeslaCam/RecentClips"
DST="$EV/TeslaCam/RecentClips"

# 每个摄像头的最新片段名（作为"上一条已收尾"的依据）
for c in front back left_repeater right_repeater left_pillar right_pillar; do
  nf=$(ls "$SRC" 2>/dev/null | grep -F -- "-$c.mp4" | sort | tail -1)
  for f in $(ls "$SRC" 2>/dev/null | grep -F -- "-$c.mp4"); do
    [ "$f" = "$nf" ] && continue
    s=$(stat -c %s "$SRC/$f" 2>/dev/null) || continue
    [ "${s:-0}" -gt 0 ] || continue
    echo "$s|$f" >> "$CUR"
    d=$(stat -c %s "$DST/$f" 2>/dev/null || echo -1)
    [ "$s" = "$d" ] && continue
    if cp -f "$SRC/$f" "$DST/$f.part" 2>/dev/null && mv -f "$DST/$f.part" "$DST/$f" 2>/dev/null; then
      echo "live   $f" >> "$ACT"
    else
      rm -f "$DST/$f.part" 2>/dev/null
    fi
  done
done

umount "$MNT" 2>/dev/null
umount -l "$MNT" 2>/dev/null
losetup -d "$L" >/dev/null 2>&1
for _l in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do losetup -d "$_l" >/dev/null 2>&1; done
mv -f "$CUR" "$PREV" 2>/dev/null

n=$(wc -l < "$ACT" 2>/dev/null || echo 0)
[ "${n:-0}" -gt 0 ] && { echo "$(date '+%m-%d %H:%M:%S') --- live: $n ---" >> /data/teslacam/collect.log; cat "$ACT" >> /data/teslacam/collect.log; }
exit 0
