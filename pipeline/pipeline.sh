#!/system/bin/sh
# pipeline.sh - collector + indexer loop, and keeps the web server alive.
DATA=/data/teslacam
B=$DATA/bin
EV=$DATA/events
LOG=$DATA/pipeline.log
PY=/data/data/com.termux/files/usr/bin/python3
PYLIB=/data/data/com.termux/files/usr/lib
CONF=$DATA/msd.conf
[ -f "$CONF" ] && . "$CONF"
# Foreground segmented playback works without pre-encoding whole events.
ENABLE_EVENT_QUEUE=${ENABLE_EVENT_QUEUE:-0}
# Tailscale does not require a personal FRP server.
ENABLE_FRP=${ENABLE_FRP:-0}
ENCODE_LOCK=${ENCODE_LOCK:-/data/local/tmp/teslacam-encode.lock}

log() { echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"; }

mkdir -p "$EV"

start_server() {
  pgrep -f 'bin/serve.py' >/dev/null 2>&1 && return 0
  [ -s "$DATA/token" ] || { log 'web not started: private access-code file missing'; return 1; }
  log "starting web server on :8081"
  cd "$EV" 2>/dev/null
  TOKVAL=$(cat "$DATA/token" 2>/dev/null)
  ROOT="$EV" PORT=8081 TOKEN="$TOKVAL" ENCODE_LOCK="$ENCODE_LOCK" setsid env LD_LIBRARY_PATH="$PYLIB" "$PY" "$B/serve.py" >> "$DATA/serve.log" 2>&1 </dev/null &
  sleep 2
}

start_frpc() {
  [ "$ENABLE_FRP" = 1 ] || return 0
  pgrep -f 'bin/frpc' >/dev/null 2>&1 && return 0
  [ -f "$DATA/frpc.toml" ] || return 0
  log "starting frpc"
  setsid "$B/frpc" -c "$DATA/frpc.toml" >> "$DATA/frpc.out" 2>&1 </dev/null &
  sleep 2
}

start_queue() {
  [ "$ENABLE_EVENT_QUEUE" = 1 ] || return 0
  pgrep -f "bin/transcode_queue.py" >/dev/null 2>&1 && return 0
  log "starting transcode queue worker"
  QUIET_MIN="${QUIET_MIN:-5}" TC_ENCODE_LOCK="$ENCODE_LOCK" setsid sh "$B/transcode_queue.sh" >> "$DATA/transcode.log" 2>&1 </dev/null &
  sleep 1
}

log "pipeline start pid=$$"
start_server

i=0
while true; do
  i=$((i+1))
  if [ $((i % 4)) -eq 1 ]; then
    sh "$B/collect.sh"            # 每 4 轮（约 60 秒）全量：事件 + 清理
  else
    sh "$B/live_collect.sh"       # 其余轮次只搬 RecentClips（约 15 秒一次）
  fi
  OUT="$EV" QUIET_MIN="${QUIET_MIN:-5}" env LD_LIBRARY_PATH="$PYLIB" "$PY" "$B/index.py" 2>>"$LOG"
  start_server
  start_frpc
  start_queue
  sleep 15
done
