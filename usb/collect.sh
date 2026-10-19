#!/system/bin/sh
# collect.sh - copy finished Tesla clips from the FAT image into /data/teslacam/events
#
# Completeness detection uses SIZE STABILITY, not mtime:
# FAT stores local time, Linux mounts it as UTC, so on a UTC+8 phone every file
# looks 8 hours in the future and an "age > N seconds" test never fires.
# A clip counts as finished once two consecutive scans (>=60s apart) see the
# same size; it is then copied atomically (.part -> rename).
DATA=/data/teslacam
CONF=$DATA/msd.conf
LOG=$DATA/collect.log
ACT=/data/local/tmp/collect.act
PREV=$DATA/.collect.prev
CUR=$DATA/.collect.cur
[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
EV="$DATA/events"
MNT="$DATA/mnt"
RECENT_KEEP_MIN=${RECENT_KEEP_MIN:-180}
KEEP_DAYS=${KEEP_DAYS:-60}
FREE_MIN_GB=${FREE_MIN_GB:-15}

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

[ -f "$IMG" ] || { log "img missing: $IMG"; exit 1; }
mkdir -p "$EV/TeslaCam" "$MNT" 2>/dev/null
: > "$ACT"
: > "$CUR"

# --- layout detection -------------------------------------------------------
# 清理上次残留（挂载/loop）
umount "$MNT" 2>/dev/null
umount -l "$MNT" 2>/dev/null
for _l in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do losetup -d "$_l" >/dev/null 2>&1; done
sig=$(dd if="$IMG" bs=1 skip=82 count=5 2>/dev/null)
if [ "$sig" = "FAT32" ]; then
  OFF=0
else
  lba=$(od -An -tu4 -j 454 -N 4 "$IMG" 2>/dev/null | tr -d ' ')
  case "$lba" in ''|*[!0-9]*) log "cannot parse image layout"; exit 1 ;; esac
  OFF=$(( lba * 512 ))
fi

L=$(losetup -f -s -o "$OFF" "$IMG" 2>/dev/null)
case "$L" in
  /dev/block/loop*) ;;
  *) log "losetup failed: $L"; exit 1 ;;
esac

if ! mount -t vfat -o ro,utf8 "$L" "$MNT" 2>/dev/null; then
  log "mount failed on $L"
  losetup -d "$L" >/dev/null 2>&1
  exit 1
fi

copy_kind() {
  kind="$1"
  src="$MNT/TeslaCam/$kind"
  [ -d "$src" ] || return 0
  dst="$EV/TeslaCam/$kind"
  mkdir -p "$dst"
  find "$src" -type f 2>/dev/null | while IFS= read -r f; do
    rel=${f#"$src"/}
    s=$(stat -c %s "$f" 2>/dev/null) || continue
    echo "$s|$kind|$rel" >> "$CUR"
    [ "${s:-0}" -gt 0 ] || continue
    # a clip is finished when either
    #   (a) its size did not change between two scans, or
    #   (b) it is RecentClips and a newer clip for the same camera already exists
    complete=0
    p=$(grep -F -m1 "|$kind|$rel" "$PREV" 2>/dev/null | cut -d'|' -f1)
    [ "$p" = "$s" ] && complete=1
    if [ "$complete" = 0 ] && [ "$kind" = "RecentClips" ]; then
      case "$rel" in
        *-front.mp4)          nm="$NF" ;;
        *-back.mp4)           nm="$NB" ;;
        *-left_repeater.mp4)  nm="$NL" ;;
        *-right_repeater.mp4) nm="$NR" ;;
        *)                    nm="" ;;
      esac
      [ -n "$nm" ] && [ "$rel" != "$nm" ] && complete=1
    fi
    [ "$complete" = 1 ] || continue
    out="$dst/$rel"
    d=$(stat -c %s "$out" 2>/dev/null || echo -1)
    [ "$s" = "$d" ] && continue
    mkdir -p "$(dirname "$out")" 2>/dev/null
    if cp -f "$f" "$out.part" 2>/dev/null && mv -f "$out.part" "$out" 2>/dev/null; then
      echo "copy   $kind/$rel" >> "$ACT"
    else
      rm -f "$out.part" 2>/dev/null
      echo "FAILED $kind/$rel" >> "$ACT"
    fi
  done
}

RC="$MNT/TeslaCam/RecentClips"
NF=$(ls "$RC" 2>/dev/null | grep -F -- '-front.mp4'          | sort | tail -1)
NB=$(ls "$RC" 2>/dev/null | grep -F -- '-back.mp4'           | sort | tail -1)
NL=$(ls "$RC" 2>/dev/null | grep -F -- '-left_repeater.mp4'  | sort | tail -1)
NR=$(ls "$RC" 2>/dev/null | grep -F -- '-right_repeater.mp4' | sort | tail -1)

copy_kind RecentClips
copy_kind SentryClips
copy_kind SavedClips

umount "$MNT" 2>/dev/null
umount -l "$MNT" 2>/dev/null
losetup -d "$L" >/dev/null 2>&1
for _l in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do losetup -d "$_l" >/dev/null 2>&1; done

mv -f "$CUR" "$PREV" 2>/dev/null

# --- prune old RecentClips copies (our copies carry correct local mtime) ----
REC="$EV/TeslaCam/RecentClips"
if [ -d "$REC" ]; then
  find "$REC" -type f -mmin +"$RECENT_KEEP_MIN" 2>/dev/null | while IFS= read -r f; do
    rm -f "$f" 2>/dev/null && echo "prune  recent/$(basename "$f")" >> "$ACT"
  done
fi

# --- disk pressure: drop oldest event folders ------------------------------
free_kb=$(df -k /data 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "$free_kb" ] && [ "$free_kb" -lt $(( FREE_MIN_GB * 1024 * 1024 )) ]; then
  for kind in SentryClips SavedClips; do
    d="$EV/TeslaCam/$kind"
    [ -d "$d" ] || continue
    ls -1dt "$d"/*/ 2>/dev/null | tail -n +6 | while IFS= read -r old; do
      rm -rf "$old" 2>/dev/null && echo "space  $kind/$(basename "$old")" >> "$ACT"
    done
  done
fi

# --- age based retention ---------------------------------------------------
if [ "$KEEP_DAYS" -gt 0 ]; then
  for kind in SentryClips SavedClips; do
    d="$EV/TeslaCam/$kind"
    [ -d "$d" ] || continue
    find "$d" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" 2>/dev/null | while IFS= read -r old; do
      rm -rf "$old" 2>/dev/null && echo "age    $kind/$(basename "$old")" >> "$ACT"
    done
  done
fi

n=$(wc -l < "$ACT" 2>/dev/null || echo 0)
if [ "${n:-0}" -gt 0 ]; then
  log "--- cycle: $n action(s) ---"
  cat "$ACT" >> "$LOG"
fi
exit 0
