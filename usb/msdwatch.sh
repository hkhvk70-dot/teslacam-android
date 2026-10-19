#!/system/bin/sh
# msdwatch.sh - keep the msd+adb gadget alive. Runs as root in background.
DATA=/data/teslacam
CONF=$DATA/msd.conf
LOG=$DATA/msdwatch.log
[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
CFG="${CFG:-mass_storage,adb}"
G=/config/usb_gadget/g1
M=$G/functions/mass_storage.0/lun.0
PAUSE=$DATA/msdwatch.pause

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

log "watchdog start pid=$$ img=$IMG cfg=$CFG"
while true; do
  sleep 5
  now=$(date +%s)

  # pause flag (maintenance running) - ignore stale flags
  if [ -f "$PAUSE" ]; then
    pt=$(stat -c %Y "$PAUSE" 2>/dev/null)
    if [ -n "$pt" ] && [ $((now - pt)) -gt 600 ]; then
      rm -f "$PAUSE"
      log "stale pause removed (>10min)"
    else
      continue
    fi
  fi

  # 1) composition
  cur=$(getprop sys.usb.config)
  if [ "$cur" != "$CFG" ]; then
    setprop sys.usb.config "$CFG"
    log "re-assert composition: [$cur] -> [$CFG]"
    sleep 2
  fi

  # 2) LUN backing file
  f=$(cat $M/file 2>/dev/null)
  if [ "$f" != "$IMG" ]; then
    echo 0 > $M/cdrom 2>/dev/null
    echo 0 > $M/nofua 2>/dev/null
    echo 1 > $M/removable 2>/dev/null
    echo 0 > $M/ro 2>/dev/null
    echo "$IMG" > $M/file 2>/dev/null
    log "re-assert lun: [$f] -> [$(cat $M/file 2>/dev/null)]"
  fi

  # 3) UDC bound?
  u=$(cat $G/UDC 2>/dev/null)
  if [ -z "$u" ]; then
    setprop sys.usb.config "$CFG"
    sleep 2
    log "re-assert UDC (was unbound)"
  fi
done
