#!/system/bin/sh
# maint.sh - 安静窗口 FAT 维护
#
# 安全窗口（任一满足即可动手）：
#   a) 镜像 idle >= MAINT_IDLE_MIN 分钟（车没在录）
#   b) USB 主机已断开（/sys/class/power_supply/usb/online = 0）
#   c) 电池处于放电状态（说明车那边断电了）
#   d) 手动强制：FORCE=1 maint.sh
# 频率闸门：距离上次成功 fsck 不足 MAINT_HOURS 小时则跳过
DATA=/data/teslacam
CONF=$DATA/msd.conf
LOG=$DATA/maint.log
[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
M=/config/usb_gadget/g1/functions/mass_storage.0/lun.0
PAUSE=$DATA/msdwatch.pause
STAMP=$DATA/.last_fsck
IDLE_MIN=${MAINT_IDLE_MIN:-45}
MIN_HOURS=${MAINT_HOURS:-24}
FORCE=${FORCE:-0}

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

[ -f "$IMG" ] || { log "img missing: $IMG"; exit 1; }

now=$(date +%s)

# --- 频率闸门 ---------------------------------------------------------------
if [ "$FORCE" != "1" ] && [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  since_h=$(( (now - last) / 3600 ))
  if [ "$since_h" -lt "$MIN_HOURS" ]; then
    log "skip: last fsck ${since_h}h ago (< ${MIN_HOURS}h)"
    exit 0
  fi
fi

# --- 安全窗口判定 -----------------------------------------------------------
usb_online=$(cat /sys/class/power_supply/usb/online 2>/dev/null)
batt=$(cat /sys/class/power_supply/battery/status 2>/dev/null)
mt=$(stat -c %Y "$IMG" 2>/dev/null)
[ -z "$mt" ] && { log "stat failed"; exit 1; }
idle=$(( (now - mt) / 60 ))

why=""
if [ "$FORCE" = "1" ]; then
  why="forced"
elif [ "$idle" -ge "$IDLE_MIN" ]; then
  why="idle ${idle}m"
elif [ "$usb_online" = "0" ]; then
  why="usb detached"
elif [ "$batt" = "Discharging" ]; then
  why="battery discharging (car off)"
fi

if [ -z "$why" ]; then
  log "no safe window: idle=${idle}m usb_online=${usb_online} batt=${batt}"
  exit 0
fi

# 最后确认：最近 60 秒内没有任何写入
if [ "$FORCE" != "1" ] && [ "$idle" -lt 1 ]; then
  log "skip: image written within 60s (idle=${idle}m)"
  exit 0
fi

log "=== fsck start (window: ${why}, img=$IMG) ==="
touch "$PAUSE"
trap 'rm -f "$PAUSE"' EXIT INT TERM

sync
echo "" > $M/file
sleep 2
if [ -n "$(cat $M/file 2>/dev/null)" ]; then
  log "WARN could not detach LUN, aborting"
  exit 2
fi

out=$(sh "$DATA/bin/fsck_img.sh" "$IMG" 2>&1)
rc=$?

echo 0 > $M/cdrom 2>/dev/null
echo 0 > $M/nofua 2>/dev/null
echo 1 > $M/removable 2>/dev/null
echo 0 > $M/ro 2>/dev/null
echo "$IMG" > $M/file
sleep 1
attached=$(cat $M/file 2>/dev/null)
rm -f "$PAUSE"

echo "$out" >> "$LOG"
[ "$rc" -le 1 ] && echo "$now" > "$STAMP"
log "=== fsck done rc=$rc, reattached=[$attached] ==="
exit $rc
