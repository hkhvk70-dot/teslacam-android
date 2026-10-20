#!/bin/sh
# teslacam.sh —— 统一入口（A8）
# 逻辑在 teslacam.py；本脚本只负责切到 Termux 的 python 环境。
# 用法：teslacam.sh status|stop|clear-hold [--state DIR] [--run DIR] [--cfg DIR] [--msd PATH]

P=/data/data/com.termux/files/usr
D=$(dirname "$0")

if [ ! -x "$P/bin/python3" ]; then
  echo "python3 not found at $P/bin/python3" >&2
  exit 1
fi

exec env LD_LIBRARY_PATH="$P/lib" "$P/bin/python3" "$D/teslacam.py" "$@"
