ESP32-S3 receiver firmware for ESP32-DATV-RX: esp32-sdr-trx 1.0.0-rc4 (https://github.com/jochenhammes/esp32-sdr-trx,
0BSD) with the cs4 format added (packed 4-bit I/Q, 500 ksps). Source: ../esp32-sdr-trx/, rebuild: ../tools/build-firmware.sh

  rx.bin               the receiver image (flash offset 0x10000, or loaded into RAM by espdr-rx / rx_dvbs2.py)
  bootloader.bin       ESP-IDF second-stage bootloader, DIO flash (offset 0x0)
  partition-table.bin  single-app partition table (offset 0x8000)
  esp32_sdr_trx-*.whl  espdr-rx and espdr.nb with these images; tools/install.sh installs it

How to load it: see "Loading the receiver firmware" in ../README.md.
