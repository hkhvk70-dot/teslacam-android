#!/system/bin/sh
# Optional whole-event worker. transcode.sh alone owns the shared encode lock.
PREFIX=${PREFIX:-/data/data/com.termux/files/usr}
DATA=${TC_DATA:-/data/teslacam}
exec env LD_LIBRARY_PATH="$PREFIX/lib" "$PREFIX/bin/python3" "$DATA/bin/transcode_queue.py" "$@"
