#!/system/bin/sh
# Retired compatibility entry. Never dispatch adb or recovery from a legacy caller.
if [ "$#" -ne 0 ]; then
  echo "self_recover.sh is retired and accepts no arguments" >&2
  exit 64
fi
echo "self_recover.sh is retired; use /data/teslacam/bin/teslacam.sh start explicitly" >&2
exit 78
