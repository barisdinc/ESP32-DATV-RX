#!/bin/sh
# Rebuilds the receiver firmware (esp32-sdr-trx/ with cs4) and the espdr wheel into firmware/.
# Needs ESP-IDF v5.5 (headers, ROM linker scripts and libphy.a; release/v5.5 at 2553c5ad was used) and the
# xtensa-esp32s3-elf toolchain on PATH, for example:
#   git clone --depth 1 -b release/v5.5 https://github.com/espressif/esp-idf.git ~/esp-idf-v5.5
#   git -C ~/esp-idf-v5.5 submodule update --init --depth 1 components/esp_phy/lib
#   IDF_PATH=~/esp-idf-v5.5 tools/build-firmware.sh
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
: "${IDF_PATH:?set IDF_PATH to an ESP-IDF v5.5 tree}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3
SRC=$(mktemp -d)
trap 'rm -rf "$SRC"' EXIT
cp -r "$ROOT/esp32-sdr-trx/." "$SRC/"
make -C "$SRC/firmware" -s images IDF_PATH="$IDF_PATH" ESPTOOL="$PY -m esptool"
bash "$SRC/scripts/stage-images.sh" >/dev/null
"$PY" -m pip wheel -q --no-deps -w "$SRC/dist" "$SRC"
cd "$ROOT/firmware"
cp "$SRC"/dist/*.whl .
cp "$SRC/src/espdr/images/rx.bin" "$SRC/src/espdr/images/bootloader.bin" "$SRC/src/espdr/images/partition-table.bin" .
sha256sum rx.bin bootloader.bin partition-table.bin *.whl > SHA256SUMS
echo "firmware/ updated:"; ls -l
