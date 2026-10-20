#!/system/bin/sh
# Managed collection/index loop. All services are started by teslacam.sh start.
DATA=/data/teslacam
B=$DATA/bin
EV=$DATA/events
STATE=$DATA/state
LOG=$DATA/pipeline.log
PY=/data/data/com.termux/files/usr/bin/python3
PYLIB=/data/data/com.termux/files/usr/lib
CONF=$DATA/msd.conf
[ -f "$CONF" ] && . "$CONF"

if [ ! -f "$STATE/control_managed" ]; then
  echo 'pipeline requires controller management; use teslacam.sh start' >&2
  exit 78
fi

mkdir -p "$EV"
i=0
while true; do
  if [ -f "$STATE/stop_requested" ] || [ -f "$STATE/export_forbidden.json" ]; then
    sleep 5
    continue
  fi
  i=$((i+1))
  if [ $((i % 4)) -eq 1 ]; then
    sh "$B/collect.sh"
  else
    sh "$B/live_collect.sh"
  fi
  OUT="$EV" QUIET_MIN="${QUIET_MIN:-5}" env LD_LIBRARY_PATH="$PYLIB" "$PY" "$B/index.py" 2>>"$LOG"
  sleep 15
done
