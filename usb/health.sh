#!/system/bin/sh
# health.sh - self check + machine readable status.
# Writes $DATA/health.json and prints a summary. Exit 0 = OK, 1 = WARN, 2 = BAD.
DATA=/data/teslacam
CONF=$DATA/msd.conf
[ -f "$CONF" ] && . "$CONF"
IMG="${IMG:-/data/teslacam/cam_disk.img}"
G=/config/usb_gadget/g1
M=$G/functions/mass_storage.0/lun.0
OUT=$DATA/health.json
CFG="${CFG:-mass_storage,adb}"

warn=0; bad=0
chk() { if [ "$2" = "$3" ]; then echo "OK   $1 = $2"; else echo "FAIL $1 = $2 (want $3)"; bad=$((bad+1)); fi; }
note() { echo "     $*"; }

# --- root ---
if su -c id 2>/dev/null | grep -q 'uid=0'; then root=ok; else root=no; bad=$((bad+1)); fi

# --- watchdog ---
wd=0
pgrep -f 'msdwatch\.sh' >/dev/null 2>&1 && wd=1
[ "$wd" = 0 ] && bad=$((bad+1))

# --- collector + web ---
pl=0
pgrep -f 'bin/pipeline\.sh' >/dev/null 2>&1 && pl=1
[ "$pl" = 0 ] && bad=$((bad+1))
web=0
pgrep -f 'bin/serve\.py' >/dev/null 2>&1 && web=1
[ "$web" = 0 ] && bad=$((bad+1))

# --- usb ---
usbcfg=$(getprop sys.usb.config)
udc=$(cat $G/UDC 2>/dev/null)
lun=$(cat $M/file 2>/dev/null)
ro=$(cat $M/ro 2>/dev/null)

# --- image ---
if [ -f "$IMG" ]; then
  isize=$(stat -c %s "$IMG" 2>/dev/null)
  imt=$(stat -c %Y "$IMG" 2>/dev/null)
  idle=$(( ( $(date +%s) - imt ) / 60 ))
else
  isize=0; idle=-1; bad=$((bad+1))
fi

# --- space ---
free_kb=$(df -k /data 2>/dev/null | tail -1 | awk '{print $4}')
free_gb=$(( ${free_kb:-0} / 1024 / 1024 ))

# --- battery ---
blevel=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)
btemp=$(cat /sys/class/power_supply/battery/temp 2>/dev/null)
bstat=$(cat /sys/class/power_supply/battery/status 2>/dev/null)
[ -n "$btemp" ] && btemp_c=$(( btemp / 10 )) || btemp_c=-1

# --- power ---
stay=$(settings get global stay_on_while_plugged_in 2>/dev/null)
wakelock=$(cat /sys/power/wake_lock 2>/dev/null)

# --- last logs ---
maint_last=$(tail -1 $DATA/maint.log 2>/dev/null | tr -d '"')
recover_last=$(tail -1 /data/local/tmp/recover.log 2>/dev/null | tr -d '"')

# --- verdict ---
[ "$root" = no ] && bad=$((bad+1))
[ "$usbcfg" != "$CFG" ] && bad=$((bad+1))
[ "$lun" != "$IMG" ] && bad=$((bad+1))
[ -z "$udc" ] && bad=$((bad+1))
[ "$idle" -gt 8640 ] && [ "$idle" -gt 0 ] && { bad=$((bad+1)); }
[ "$btemp_c" -ge 45 ] 2>/dev/null && bad=$((bad+1))
[ "$free_gb" -lt 10 ] && warn=$((warn+1))

if   [ "$bad" -gt 0 ];  then verdict=BAD
elif [ "$warn" -gt 0 ]; then verdict=WARN
else verdict=OK; fi

cat > "$OUT" <<EOF
{
  "verdict": "$verdict",
  "ts": $(date +%s),
  "time": "$(date '+%Y-%m-%d %H:%M:%S')",
  "root": "$root",
  "watchdog": $wd,
  "pipeline": $pl,
  "web": $web,
  "usb_config": "$usbcfg",
  "udc": "$udc",
  "lun": "$lun",
  "lun_ro": "$ro",
  "image": "$IMG",
  "image_size": $isize,
  "image_idle_min": $idle,
  "data_free_gb": $free_gb,
  "battery_level": "$blevel",
  "battery_temp_c": $btemp_c,
  "battery_status": "$bstat",
  "stay_on_plugged_in": "$stay",
  "wakelock": "$wakelock",
  "uptime_sec": $(cut -d' ' -f1 /proc/uptime),
  "maint_last": "$maint_last",
  "recover_last": "$recover_last"
}
EOF
chmod 644 "$OUT" 2>/dev/null

echo "verdict : $verdict"
chk root "$root" ok
chk usb_config "$usbcfg" "$CFG"
chk lun "$lun" "$IMG"
note "udc=$udc ro=$ro watchdog=$wd pipeline=$pl web=$web"
note "image size=$isize idle=${idle}min  /data free=${free_gb}GB"
note "battery ${blevel}% ${btemp_c}C $bstat  stay_on_plugged_in=$stay"
[ -n "$maint_last" ] && note "maint: $maint_last"
[ "$verdict" = OK ] && exit 0
[ "$verdict" = WARN ] && exit 1
exit 2
