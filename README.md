# ESP32-DATV-RX

A DVB-S2 receiver built around an **ESP32-S3**. It is the receiving half of
[ESP32-DATV-SA](https://github.com/barisdinc/ESP32-DATV-SA), the stand-alone ESP32-C3 DATV transmitter.

*Türkçe özet aşağıda — [Türkçe](#türkçe).*

> **Status:** planning. Nothing here works yet.

## Idea

The ESP32-S3 has an undocumented "radio dump" engine. It writes the Wi-Fi receiver's raw I/Q samples into
SRAM at 80 or 16 Msps. [eSpDR](https://github.com/h0m3us3r/eSpDR) by h0m3us3r found it, and
[esp32-sdr-trx](https://github.com/jochenhammes/esp32-sdr-trx) by Jochen Hammes turned it into a
one-board receiver: the chip decimates 16 Msps to 250 or 333 ksps of int8 I/Q and streams it over USB as an
`rtl_tcp` server (1.84 to 2.79 GHz).

The ESP32 only moves samples. All the heavy work runs on the PC with existing tools:

```
ESP32-C3 (ESP32-DATV-SA)  ~~~ 2300.500 MHz DVB-S2 ~~~>  ESP32-S3: I/Q capture, decimation
                                                              │ USB (int8 or packed 4-bit I/Q)
                                                              ▼
                                         PC: leandvb / SDRangel / gr-dvbs2rx  →  TS  →  ffplay / VLC
```

## The bandwidth problem

| Transmitter symbol rate | Occupied bandwidth (roll-off 0.35) | Fits the receiver? |
|---|---|---|
| 125 kS/s (selectable on the TX front panel) | ≈ 170 kHz | yes, as is: 333 ksps, about 2.7 samples per symbol |
| 333 kS/s (TX default) | ≈ 450 kHz | no: esp32-sdr-trx is limited to ±133 kHz |
| 333 kS/s with 4-bit I/Q | ≈ 450 kHz | should fit: 500 to 667 ksps × 1 byte = 0.5 to 0.67 MB/s |

The limit is USB Full-Speed (0.87 MB/s measured by esp32-sdr-trx), not the CPU. 8-bit I/Q at 500 ksps
would need 1.0 MB/s. QPSK loses very little with 4-bit samples.

## Plan

1. **Proof of concept, no new code:** esp32-sdr-trx on an ESP32-S3; the transmitter at 125 kS/s; receive with
   SDRangel (DATV demodulator) or `leandvb`. Measure the margin (both sides run at low level) and the
   crystal offset (up to ±24 kHz).
2. **One-command receiver:** `rx_dvbs2.py`, which runs the I/Q stream through `leandvb` into `ffplay`.
3. **Wide mode in the receiver firmware:** packed 4-bit I/Q at 500 to 667 ksps, so the default 333 kS/s
   transmission can be received.
4. *(Research, optional)* Look for the same dump engine on the ESP32-C3.

## Hardware

- **Transmitter:** ESP32-C3 SuperMini with [ESP32-DATV-SA](https://github.com/barisdinc/ESP32-DATV-SA)
  (2300.500 MHz, DVB-S2 QPSK 3/4, 333 kS/s, pilots).
- **Receiver:** ESP32-S3 board with **two USB ports** (a USB-UART bridge and native USB), as required by
  esp32-sdr-trx.
- A Linux PC with FFmpeg and leandvb or SDRangel.

## Credits

The receive path is built on [eSpDR](https://github.com/h0m3us3r/eSpDR) (h0m3us3r) and
[esp32-sdr-trx](https://github.com/jochenhammes/esp32-sdr-trx) (Jochen Hammes, 0BSD). The transmitter
is a fork of [SP8ESA/ESP32-DATV](https://github.com/SP8ESA/ESP32-DATV).

---

## Türkçe

**ESP32-DATV-RX**, [ESP32-DATV-SA](https://github.com/barisdinc/ESP32-DATV-SA) vericisinin yayınını almak
için ESP32-S3 tabanlı bir DVB-S2 alıcı projesidir. **Durum: planlama aşamasında.**

- **Fikir:** ESP32-S3'ün belgelenmemiş "radio dump" donanımı Wi-Fi alıcısının ham I/Q örneklerini veriyor
  (eSpDR, esp32-sdr-trx). ESP32 bu örnekleri sadece USB'den PC'ye aktarır. Demodülasyon, LDPC ve video
  çözme PC'de leandvb / SDRangel / ffplay ile yapılır.
- **Sorun:** Vericinin 333 kS/s yayını ≈ 450 kHz yer kaplar. Mevcut alıcı en fazla ±133 kHz görür; sınırı
  USB Full-Speed koyar. Çözüm: 4 bit I/Q ile 500–667 ksps.
- **Adımlar:** (1) verici 125 kS/s'deyken hazır araçlarla ilk deneme, (2) tek komutluk alıcı betiği,
  (3) alıcı firmware'ine 4 bit geniş mod, (4) isteğe bağlı: aynı donanımı ESP32-C3'te aramak.
- **Donanım:** verici ESP32-C3 SuperMini, alıcı **iki USB portlu** bir ESP32-S3 kartı.
