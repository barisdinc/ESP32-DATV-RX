#!/bin/sh
# Live DVB-S2 receive: espdr-rx (rtl_tcp) -> leandvb -> ffmpeg (retime) -> ffplay.
# Usage: tools/watch.sh [FREQ_HZ] [GAIN_DB] [SYMBOL_RATE]   (espdr-rx --decim 3 must be running)
# LOG=dir keeps the leandvb log and the TS.
FREQ=${1:-2301.382e6}
GAIN=${2:-10}
SR=${3:-125000}
HERE=$(dirname "$0")
LEANDVB=${LEANDVB:-$HOME/SDR/leansdr/src/apps/leandvb}
LOG=${LOG:-/tmp}

"$HERE/../.venv/bin/python" "$HERE/rtltcp_cat.py" "$FREQ" "$GAIN" |
  "$LEANDVB" --u8 -f 333333 --sr "$SR" --standard DVB-S2 --const QPSK --cr 3/4 --ldpc-bf 100 --fastdrift -d 2>"$LOG/rx_leandvb.log" |
  tee "$LOG/rx_live.ts" |
  ffmpeg -hide_banner -loglevel error -probesize 64000 -analyzeduration 3000000 -fflags +igndts+genpts -i - -vf "setpts=N/25/TB" -an -c:v rawvideo -pix_fmt yuv420p -f nut - |
  SDL_VIDEO_X11_FORCE_EGL=1 ffplay -hide_banner -loglevel info -fflags nobuffer -window_title "yATOLYE-TV DVB-S2 (ESP32-S3 RX)" -vf showinfo -i - 2>"$LOG/rx_ffplay.log"
