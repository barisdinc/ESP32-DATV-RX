#!/bin/sh
# Install what rx_dvbs2.py needs, inside this folder:
#   .venv/                 Python venv with espdr-rx and espdr.nb (firmware/*.whl, which carries the receiver firmware) and numpy
#   build/leansdr/         leandvb from leansdr (work branch, DVB-S2 enabled)
# System packages it expects: python3 python3-venv git g++ make ffmpeg
#   sudo apt install python3 python3-venv git g++ make ffmpeg
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

ESPDR_WHEEL=firmware/esp32_sdr_trx-1.0.0rc4+cs4-py3-none-any.whl   # esp32-sdr-trx with cs4, see esp32-sdr-trx/
LEANSDR_URL=https://github.com/pabr/leansdr.git
LEANSDR_COMMIT=84c59e1c7a1a79338d5722d63f28640cc9d350f3   # branch "work", DVB-S2 receiver

for t in python3 git g++ make ffmpeg ffplay; do
  command -v $t >/dev/null || { echo "missing: $t  (sudo apt install python3 python3-venv git g++ make ffmpeg)"; exit 1; }
done

echo "== Python venv and espdr ($ESPDR_WHEEL)"
[ -f "$ESPDR_WHEEL" ] || { echo "missing $ESPDR_WHEEL"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q "$ESPDR_WHEEL"                                # with its dependencies
.venv/bin/pip install -q --force-reinstall --no-deps "$ESPDR_WHEEL"   # replaces an upstream esp32-sdr-trx
.venv/bin/pip install -q numpy

echo "== leandvb (leansdr $LEANSDR_COMMIT)"
if [ ! -d build/leansdr/.git ]; then
  mkdir -p build
  git clone -q "$LEANSDR_URL" build/leansdr
fi
git -C build/leansdr fetch -q origin work
git -C build/leansdr checkout -q "$LEANSDR_COMMIT"
make -C build/leansdr/src/apps leandvb \
  CXXFLAGS='-O3 -I.. -DVERSION="\"leansdr-work\"" -DLEANSDR_EXTENSIONS -Wno-sign-compare -Wno-array-bounds -Wno-unused-variable' \
  >build/leandvb-build.log 2>&1 || { echo "leandvb build failed, see build/leandvb-build.log"; exit 1; }

echo "== done"
echo "Optional, once: the udev rule of espdr (keeps ModemManager away, no dialout group needed):"
echo "  sudo cp $(.venv/bin/python -c 'import espdr,os;print(os.path.dirname(espdr.__file__))')/udev/70-espdr.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger"
echo "Then: ./rx_dvbs2.py --freq 2300.5"
