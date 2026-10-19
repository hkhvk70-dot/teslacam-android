#!/system/bin/sh
# msd.sh - bring up "USB mass storage + adb" composite gadget on Xiaomi/Qualcomm
# Usage: msd.sh [up|down|status]
# Must run as root.

DATA=/data/teslacam
BIN=$DATA/bin
CONF=$DATA/msd.conf
LOG=$DATA/msd.log

[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
CFG="${CFG:-mass_storage,adb}"
G=/config/usb_gadget/g1
M=$G/functions/mass_storage.0/lun.0

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

apply_sepolicy() {
  RULES=$BIN/sepolicy.rules
  [ -f "$RULES" ] || return 0
  if [ -x /data/adb/ksud ] || [ -e /data/adb/ksud ]; then
    /data/adb/ksud sepolicy apply "$RULES" >/dev/null 2>&1 && log "sepolicy applied" || log "sepolicy apply FAILED"
  fi
}

set_lun() {
  echo "" > $M/file 2>/dev/null
  echo 0 > $M/cdrom 2>/dev/null
  echo 0 > $M/nofua 2>/dev/null
  echo 1 > $M/removable 2>/dev/null
  echo 0 > $M/ro 2>/dev/null
  echo "$IMG" > $M/file 2>/dev/null
  s=$(cat $M/file 2>/dev/null)
  if [ "$s" = "$IMG" ]; then log "lun set -> $IMG"; else log "lun set FAILED (got [$s])"; fi
}

case "${1:-up}" in
  up)
    log "=== up: img=$IMG cfg=$CFG ==="
    [ -f "$IMG" ] || { log "ERROR image missing: $IMG"; echo "ERROR: image missing $IMG"; exit 1; }
    apply_sepolicy
    cur=$(getprop sys.usb.config)
    if [ "$cur" != "$CFG" ]; then
      setprop sys.usb.config "$CFG"
      sleep 3
    fi
    set_lun
    sleep 1
    log "state: cfg=[$(getprop sys.usb.config)] udc=[$(cat $G/UDC 2>/dev/null)] funcs=[$(ls $G/configs/b.1 2>/dev/null | tr '\n' ' ')]"
    ;;
  down)
    echo "" > $M/file 2>/dev/null
    setprop sys.usb.config adb
    log "=== down ==="
    ;;
  status)
    echo "img        : $IMG"
    echo "sys.usb.cfg: $(getprop sys.usb.config)"
    echo "vendor.cfg : $(getprop vendor.usb.config)"
    echo "UDC        : $(cat $G/UDC 2>/dev/null)"
    echo "functions  : $(ls $G/configs/b.1 2>/dev/null | tr '\n' ' ')"
    echo "lun file   : [$(cat $M/file 2>/dev/null)]"
    echo "lun ro     : $(cat $M/ro 2>/dev/null)"
    echo "selinux    : $(getenforce)"
    ;;
esac
