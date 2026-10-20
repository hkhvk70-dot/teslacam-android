#!/system/bin/sh
# Retired compatibility entry. Never run maintenance from a legacy caller.
if [ "$#" -ne 0 ]; then
  echo "maint.sh is retired and accepts no arguments" >&2
  exit 64
fi
echo "maint.sh is retired; use /data/teslacam/bin/teslacam.sh maintain explicitly" >&2
exit 78
