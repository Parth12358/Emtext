# emtext firmware

Firmware for the **emtext pendant**: an M5StickS3 (ESP32-S3) worn on a lanyard that listens
to a conversation, streams 16 kHz audio to a private emtext server over a secure WebSocket,
and shows the server's one-line read of the speaker's emotional subtext on its screen. The
device is ears and a screen; all transcription, voice analysis and interpretation happen on
the server, which is not part of this repository.

**This repository is a read-only mirror.** It is regenerated from the private emtext
repository on every change (one snapshot commit per push, so there is no useful history
here). Issues and pull requests are welcome and will be read, but changes land upstream and
flow back out.

## Flash a release

Every [release](../../releases) ships:

| file | what |
|---|---|
| `emtext-firmware-<tag>.merged.bin` | whole flash image (bootloader + partitions + app), write at `0x0` |
| `emtext-firmware-<tag>.app.bin` | app only, write at `0x10000` |
| `emtext-firmware-<tag>-source.zip` | this repository at the release commit |
| `FLASHING.md`, `SHA256SUMS.txt` | the esptool one-liners and checksums |

```
pip install esptool
esptool.py --chip esp32s3 --port <COM/tty> write_flash 0x0 emtext-firmware-<tag>.merged.bin
```

Releases carry no Wi-Fi credentials or server token. Hold the button at boot to open the
captive portal (`emtext-setup` network) and enter them, or build from source with a
`secrets.h`.

## Build from source (Arduino IDE)

- Board package: M5Stack (`https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/arduino/package_m5stack_index.json`), board **M5StickS3**.
- Libraries: `M5Unified`, `ArduinoJson` (v7), `WebSockets` (Links2004), `ArduinoHttpClient`.
- Copy `emtext/secrets.example.h` to `emtext/secrets.h` and fill in Wi-Fi, server host and token.
- Open `emtext/emtext.ino`; the IDE compiles `emtext/src/` recursively.

Equivalent command line: `arduino-cli compile --fqbn m5stack:esp32:m5stack_sticks3 emtext`.

## Docs

- [`docs/firmware.md`](docs/firmware.md) — architecture, modules, build stages, the wire contract from the device side, known deviations.
- [`docs/firmware-ui-ux.md`](docs/firmware-ui-ux.md) — screens, controls and state machine.
- [`docs/firmware-requirements.md`](docs/firmware-requirements.md) — the requirements the device is built against.

`SOURCE.txt` records the upstream commit each snapshot came from.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE). You may use, modify and share this firmware for
noncommercial purposes; commercial use needs the copyright holder's permission.
Copyright © 2026 Parth Kshirsagar.
