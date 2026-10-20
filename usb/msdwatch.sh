#!/system/bin/sh
# msdwatch.sh - 维持 U 盘导出（P2：所有导出动作统一走控制面 reassert）
#
# 本脚本不再直接写 configfs / setprop。
# 每次需要动作时调用 teslacam.py reassert，由它在控制锁内判定：
#   stop_requested / export_forbidden / op_inprogress → 一律不导出
#
# 已移除：msdwatch.pause 及"超过 600 秒自动失效"的机制（不再作为保护依据）。

DATA=/data/teslacam
CONF=$DATA/msd.conf
LOG=$DATA/msdwatch.log
[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
CFG="${CFG:-mass_storage,adb}"
G=/config/usb_gadget/g1
M=$G/functions/mass_storage.0/lun.0

P=/data/data/com.termux/files/usr
PY=$P/bin/python3
LIB=$P/lib
TL=$DATA/bin/teslacam.py

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

log "watchdog start pid=$$ (control-plane mode) img=$IMG cfg=$CFG"

LAST=""
while true; do
  sleep 5

  # 便宜的前置检查：只有发现异常才惊动 python
  cur=$(getprop sys.usb.config)
  f=$(cat $M/file 2>/dev/null)
  u=$(cat $G/UDC 2>/dev/null)
  if [ "$cur" = "$CFG" ] && [ "$f" = "$IMG" ] && [ -n "$u" ]; then
    LAST=""
    continue
  fi

  out=$(LD_LIBRARY_PATH=$LIB "$PY" "$TL" reassert \
          --state "$DATA/state" --run "$DATA/run" \
          --cfg "$G" --msd "$DATA/bin/msd.sh" 2>&1)

  # 只在结果变化时记日志，避免每 5 秒刷屏
  if [ "$out" != "$LAST" ]; then
    log "$out"
    LAST="$out"
  fi
done
