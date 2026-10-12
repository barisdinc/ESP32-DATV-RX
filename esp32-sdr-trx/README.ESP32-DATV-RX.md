# esp32-sdr-trx, as used by ESP32-DATV-RX

This folder is [esp32-sdr-trx](https://github.com/jochenhammes/esp32-sdr-trx) by Jochen Hammes (0BSD, see `LICENSE` and
`NOTICE`) at upstream commit `e1aebbf`, plus one change: the **cs4** stream format (packed 4-bit I/Q with a gain per packet),
which lets the ESP32-S3 deliver 500 ksps within the 0.87 MB/s of its USB Serial/JTAG port. `cs4.patch` is the whole
difference to upstream. `docs/images/` and `.github/` are left out.

What cs4 does (details in `firmware/protocol/narrowband.h`): per packet of about 1 ms, the cs16 samples minus a running DC
estimate are multiplied by a gain in 0.75 dB steps so that their RMS is about 3.5 LSB, floored to 4 bits and packed I | Q << 4.
The gain travels in the packet header. `python -m espdr.nb run --format cs4 --convert cf32` restores the samples.
The image still supports cs8 and cs16, so `espdr-rx` works as before.

The prebuilt image and wheel are in `../firmware/`. To rebuild them: `../tools/build-firmware.sh` (needs ESP-IDF v5.5 and
the xtensa-esp32s3 toolchain).
