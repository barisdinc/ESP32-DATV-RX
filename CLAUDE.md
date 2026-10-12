# ESP32-DATV-RX — context for Claude Code sessions

The user (Baris Dinc, TA7W, yATOLYE / Turkish ham radio community) writes in Turkish. Answer in Turkish;
code, commits and READMEs are in English, and user-facing guides are bilingual (EN + TR).

## Goal

Receive the DVB-S2 signal of our ESP32-C3 transmitter with an **ESP32-S3**, which only captures I/Q and
streams it over USB. Demodulation, LDPC, TS and video run on the PC.

## Related directories (all under /home/barisdinc/SDR)

| Path | What | Notes |
|---|---|---|
| `ESP32-DATV/` | **Transmitter**: ESP32-DATV-SA, repo `barisdinc/ESP32-DATV-SA` (fork of SP8ESA/ESP32-DATV) | ESP32-C3 SuperMini. Firmware in `firmware/` (ESP-IDF 6.1), host tools in `host/` (`dvbs2.py` = reference DVB-S2 encoder, `make_beacon.py` = beacon generator, `tx_dvbs.py` = PC-driven TX). Release `v0.2.0-sa`. |
| `esp32-sdr-trx/` | Clone of jochenhammes/esp32-sdr-trx (0BSD) | **Receiver base**: `espdr-rx` = rtl_tcp server, 250/333 ksps int8 I/Q. Docs: `docs/receiver.md`, `docs/internals.md` (decimator CIC/16 + FIR /4 or /3, USB limit 0.87 MB/s, `NB_SET_DECIM`, `NB_SET_FORMAT`). |
| `ESP32-DATV-RX/` | This project, repo `barisdinc/ESP32-DATV-RX` | |

Upstream radio research: https://github.com/h0m3us3r/eSpDR (`docs/RADIO.md`: dump engine at `0x60033d5c`,
80/16 Msps, 10-bit I/Q in 32-bit words, four 16384-pair SRAM banks).

## Transmitter facts (as of 2026-10-12)

- Stand-alone beacon at power-up: **2300.500 MHz, DVB-S2 QPSK 3/4, normal frames, pilots, 333 kS/s,
  roll-off 0.35, level −27 dB** (amplitude 13 of 300). The loop is 4 PLFRAMEs (0.4 s) from RAM.
- Service `yATOLYE-TV` / `Technology Community`. The test card reads
  `ESP32 DATV-SA` / `Project by yATOLYE` / `Turkish Hamradio Community`.
- The optional OLED and encoder front panel can set the frequency (2300–2450 MHz), level, symbol rate
  (125/250/333/400/500 kS/s) and pilots. At symbol rates other than 333k the TS timestamps run off real time.
- The PC-driven mode (`host/tx_dvbs.py`, GUI) supports any rate from 33 kS/s up.
- Build: `export IDF_PYTHON_ENV_PATH=$HOME/.espressif/python_env/idf6.1_py3.12_env; source ~/.espressif/v6.1/esp-idf/export.sh`,
  then run `idf.py -p /dev/ttyACM0 build flash` in `ESP32-DATV/firmware`. Python host tools use `ESP32-DATV/.venv`.

## Key constraint: bandwidth

333 kS/s × 1.35 ≈ 450 kHz occupied. espdr-rx gives ±100 kHz at 250 ksps or ±133 kHz at 333 ksps. 500 ksps
int8 (1.0 MB/s) exceeds the S3 USB Serial/JTAG limit of 0.87 MB/s. Options:
- Step 1: transmitter at **125 kS/s** (≈ 170 kHz) works with an unmodified espdr-rx at 333 ksps.
- Step 3: a packed **4-bit I/Q** mode at 500–667 ksps (0.5–0.67 MB/s) in a fork of the esp32-sdr-trx firmware.

## Plan

1. PoC: espdr-rx + SDRangel DATV demod (installed: `/usr/bin/sdrangel`) or leandvb (not installed yet) +
   ffplay (installed). The transmitter is at 125 kS/s.
2. `rx_dvbs2.py`: espdr-rx → leandvb → ffplay as one command.
3. 4-bit wide mode in the receiver firmware.
4. Optional: look for the dump engine on the ESP32-C3 (single core, no SIMD, so on-chip decimation at 16 Msps
   is unlikely to fit).

## Hardware on the bench

- TX: ESP32-C3 SuperMini (revision v0.4), usually at `/dev/ttyACM0`.
- RX: ESP32-S3 with two USB ports (UART bridge + native USB). Check `lsusb` / `ls /dev/ttyACM* /dev/ttyUSB*`
  before you flash: **do not flash the C3 with S3 firmware or the other way round.**

## Rules

- Transmitting requires the user's amateur licence. Keep levels low and antennas close on the bench.
- Ask before force-pushing or before overwriting releases.
