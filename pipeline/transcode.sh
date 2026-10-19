#!/system/bin/sh
# transcode.sh <SentryClips|SavedClips> <event_name>
# Event proxy generator. Never mounts, changes, or deletes original recordings.
# All published MP4s are validated temporary files renamed within OUT.
PREFIX=${PREFIX:-/data/data/com.termux/files/usr}
export PATH="$PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/lib"

EV=${EV:-/data/teslacam/events}
LOG=${TC_LOG:-/data/teslacam/transcode.log}
TMP_BASE=${TC_TMP_BASE:-/data/local/tmp}
ENCODE_LOCK=${TC_ENCODE_LOCK:-/data/local/tmp/teslacam-encode.lock}
TC_TIMEOUT=${TC_TIMEOUT:-900}
FFMPEG=${FFMPEG:-$PREFIX/bin/ffmpeg}
FFPROBE=${FFPROBE:-$PREFIX/bin/ffprobe}
PY=${TC_PYTHON:-$PREFIX/bin/python3}
KIND="$1"; NAME="$2"
log() { printf '%s %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

case "$KIND" in SentryClips|SavedClips) ;; *) echo 'invalid clip kind' >&2; exit 2 ;; esac
case "$NAME" in *[!0-9_-]*|'') echo 'invalid event name' >&2; exit 2 ;; esac
case "$NAME" in ????-??-??[_-]??-??-??) ;; *) echo 'invalid event name' >&2; exit 2 ;; esac
case "$TC_TIMEOUT" in *[!0-9]*|'') echo 'invalid TC_TIMEOUT' >&2; exit 2 ;; esac
[ "$TC_TIMEOUT" -gt 0 ] || exit 2

SRC="$EV/TeslaCam/$KIND/$NAME"
OUT="$EV/_proxy/$KIND/$NAME"
CAMS='front back left_repeater right_repeater'
[ -d "$SRC" ] || { log "missing $KIND/$NAME"; exit 1; }
mkdir -p "$OUT" || exit 1
NICE=$(command -v nice) || { log 'guard: nice unavailable'; exit 69; }
TIMEOUT=$(command -v timeout) || { log 'guard: timeout unavailable'; exit 69; }
FLOCK=$(command -v flock) || { log 'guard: flock unavailable, refusing concurrent encode'; exit 69; }
[ -x "$FFMPEG" ] && [ -x "$FFPROBE" ] && [ -x "$PY" ] || {
    log 'guard: missing ffmpeg/ffprobe/python3'; exit 69;
}

# This flock file is also used by the HTTP live worker (Python fcntl.flock).
# Lock only one encode at a time. Release and yield between camera encodes so
# the quasi-live worker can take priority over a complete historical event.
run_encoder() (
    # Do not inherit the parent job's temporary-directory cleanup trap.
    trap - EXIT INT TERM HUP
    exec 9>"$ENCODE_LOCK" || exit 1
    if ! "$FLOCK" -x -w 30 9; then
        log "busy $KIND/$NAME: encoder lock unavailable after 30s"
        exit 75
    fi
    "$TIMEOUT" -k 5 "$TC_TIMEOUT" "$NICE" -n 15 "$FFMPEG" "$@" 2>>"$LOG"
    encode_rc=$?
    "$FLOCK" -u 9
    exec 9>&-
    sleep 1
    exit "$encode_rc"
)

TMP=$(mktemp -d "$TMP_BASE/tc-guard.XXXXXX") || exit 1
cleanup() {
    # Only this invocation's explicit temporary names are removed.
    for c in $CAMS; do rm -f "$OUT/.cam_${c}.$$.tmp.mp4"; done
    rm -f "$OUT/.mosaic.$$.tmp.mp4" "$OUT/.mosaic.$$.tmp.json" "$OUT/.done.$$.tmp"
    rm -f "$TMP"/*
    rmdir "$TMP" 2>/dev/null
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

# Require both a finite positive duration and at least two declared video
# frames. Unknown frame counts are rejected rather than guessed as playable.
# ffprobe reads container metadata only; it does not decode entire recordings.
probe_duration() {
    if ! "$TIMEOUT" -k 2 15 "$NICE" -n 15 "$FFPROBE" -v error \
        -select_streams v:0 \
        -show_entries stream=codec_type,duration,nb_frames:format=duration \
        -of json "$1" > "$TMP/probe.json" 2>>"$LOG"; then
        return 1
    fi
    "$PY" - "$TMP/probe.json" <<'PY_PROBE'
import json, math, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    stream = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
    if int(stream.get("nb_frames", "0")) < 2:
        raise ValueError("too few or unknown frames")
    candidates = [stream.get("duration"), data.get("format", {}).get("duration")]
    for value in candidates:
        try:
            duration = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(duration) and duration > 0:
            print("%.6f" % duration)
            sys.exit(0)
except (OSError, ValueError, TypeError, StopIteration, KeyError):
    pass
sys.exit(1)
PY_PROBE
}

fingerprint() {
    # Paths are validated Tesla-generated names, so this text format is safe.
    while IFS= read -r path; do
        stat -c '%s:%Y:%y' "$path" || return 1
    done < "$1"
}

: > "$TMP/generated"
valid_total=0
for c in $CAMS; do
    : > "$TMP/list_$c.txt"
    : > "$TMP/durations_$c.tsv"
    printf '%s\n' "$SRC"/*-"$c".mp4 | sort > "$TMP/candidates_$c.txt"
    while IFS= read -r f; do
        [ -f "$f" ] || continue
        bn=$(basename "$f")
        # Reject unexpected paths rather than inserting quotes into concat files.
        case "$bn" in *[!0-9A-Za-z_.-]*) log "skip unsafe filename $KIND/$NAME/$c"; continue ;; esac
        d=$(probe_duration "$f") || { log "skip invalid source $KIND/$NAME/$bn"; continue; }
        printf '%s\n' "$f" >> "$TMP/list_$c.txt"
        printf '%s\t%s\n' "$f" "$d" >> "$TMP/durations_$c.tsv"
        valid_total=$((valid_total+1))
    done < "$TMP/candidates_$c.txt"
done

if [ "$valid_total" -eq 0 ]; then
    rm -f "$OUT/.done"
    log "SKIP $KIND/$NAME: no valid source video; no completion/failure marker written"
    exit 1
fi

# Each camera is finite even if an input is malformed. Limit decode, filter,
# and encode threads, reduce proxy frame rate, and schedule below tepla.
for c in $CAMS; do
    [ -s "$TMP/list_$c.txt" ] || continue
    sed "s|^|file '|; s|$|'|" "$TMP/list_$c.txt" > "$TMP/cc_$c.txt"
    total=$(awk -F '\t' '{s+=$2} END {printf "%.6f", s}' "$TMP/durations_$c.tsv")
    fingerprint "$TMP/list_$c.txt" > "$TMP/before_$c.txt" || continue
    tmp_video="$OUT/.cam_${c}.$$.tmp.mp4"
    run_encoder \
        -nostdin -hide_banner -loglevel error -y -threads 2 \
        -f concat -safe 0 -i "$TMP/cc_$c.txt" -filter_threads 1 \
        -vf 'fps=12,scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1' \
        -t "$total" -c:v libx264 -threads 2 -preset ultrafast \
        -b:v 600k -maxrate 750k -bufsize 1500k -pix_fmt yuv420p -an \
        -movflags +faststart "$tmp_video"
    rc=$?
    fingerprint "$TMP/list_$c.txt" > "$TMP/after_$c.txt" || rc=1
    if [ "$rc" -eq 0 ] && cmp -s "$TMP/before_$c.txt" "$TMP/after_$c.txt"; then
        duration=$(probe_duration "$tmp_video") || duration=''
        if [ -n "$duration" ] && mv -f "$tmp_video" "$OUT/cam_$c.mp4"; then
            printf '%s\t%s\n' "$c" "$duration" >> "$TMP/generated"
            log "proxy $KIND/$NAME/$c guarded rc=0 duration=$duration"
            continue
        fi
    fi
    rm -f "$tmp_video"
    log "proxy FAILED $KIND/$NAME/$c rc=$rc (invalid output or changing source)"
done

if [ ! -s "$TMP/generated" ]; then
    rm -f "$OUT/.done"
    log "FAILED $KIND/$NAME: no valid proxy; no permanent failure marker written"
    exit 1
fi

min_duration=$(awk -F '\t' 'NR==1{m=$2} $2<m{m=$2} END{printf "%.6f",m}' "$TMP/generated")
set --
filter=''; idx=0
for c in $CAMS; do
    # Preserve front/back/left/right tile positions when a camera is missing.
    if awk -F '\t' -v c="$c" '$1==c{found=1} END{exit !found}' "$TMP/generated"; then
        set -- "$@" -threads 2 -i "$OUT/cam_$c.mp4"
    else
        # A finite color source PLUS xstack shortest PLUS -t prevents the old
        # infinite lavfi-black encode, including partially missing events.
        set -- "$@" -f lavfi -i "color=c=black:s=640x360:r=12:d=$min_duration"
    fi
    filter="${filter}[$idx:v]fps=12,scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS[v$idx];"
    idx=$((idx+1))
done
filter="${filter}[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1[out]"
tmp_mosaic="$OUT/.mosaic.$$.tmp.mp4"
run_encoder \
    -nostdin -hide_banner -loglevel error -y "$@" \
    -filter_complex_threads 1 -filter_complex "$filter" \
    -map '[out]' -t "$min_duration" -shortest -c:v libx264 -threads 2 \
    -preset ultrafast -b:v 1000k -maxrate 1200k -bufsize 2400k \
    -pix_fmt yuv420p -an -movflags +faststart "$tmp_mosaic"
rc=$?
duration=''
[ "$rc" -eq 0 ] && duration=$(probe_duration "$tmp_mosaic")
if [ -z "$duration" ]; then
    rm -f "$OUT/.done"
    log "mosaic FAILED $KIND/$NAME rc=$rc: invalid output; no completion marker written"
    exit 1
fi

# Same schema as before; include only validated front inputs and truncate the
# final map entry at the finite mosaic duration. Never invent a 60-second clip.
if ! "$PY" - "$TMP/durations_front.tsv" "$duration" "$OUT/.mosaic.$$.tmp.json" <<'PY_MAP'
import json, math, os, sys
entries = []
start = 0.0
limit = float(sys.argv[2])
with open(sys.argv[1], encoding="utf-8") as source:
    for line in source:
        path, duration = line.rstrip("\n").split("\t", 1)
        length = min(float(duration), max(0.0, limit - start))
        if not math.isfinite(length) or length <= 0:
            break
        key = os.path.basename(path)[:-len("-front.mp4")]
        entries.append({"key": key, "start": round(start, 6), "dur": round(length, 6)})
        start += length
with open(sys.argv[3], "w", encoding="utf-8") as target:
    json.dump({"minutes": entries}, target, separators=(",", ":"))
PY_MAP
then
    rm -f "$OUT/.done"
    log "mosaic FAILED $KIND/$NAME: map generation failed"
    exit 1
fi

mv -f "$tmp_mosaic" "$OUT/mosaic.mp4" || exit 1
mv -f "$OUT/.mosaic.$$.tmp.json" "$OUT/mosaic.json" || exit 1
printf 'guarded-v1\n' > "$OUT/.done.$$.tmp"
mv -f "$OUT/.done.$$.tmp" "$OUT/.done" || exit 1
rm -f "$OUT/.failed"
log "mosaic $KIND/$NAME guarded rc=0 duration=$duration valid_inputs=$valid_total"
