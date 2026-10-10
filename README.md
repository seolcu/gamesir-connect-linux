# GameSir Connect for Linux (local repackager)

Scripts to run GameSir's official configuration app, **GameSir Connect** (Windows-only), unmodified on Linux.
Developed and tested with a Cyclone 2 (`3537:100b` and related IDs).

> This repository contains no GameSir code. `build.sh` downloads the installer from GameSir's
> official server and converts it **on your machine only**. This is an unofficial project, not affiliated with GameSir.

## Install

```bash
./build.sh            # installs to ~/.local/share/gamesir-connect and registers a launcher
sudo install -m644 70-gamesir.rules /etc/udev/rules.d/ \
  && sudo udevadm control --reload-rules && sudo udevadm trigger   # once
gamesir-connect
```

Requirements: `curl 7z unzip node npm openssl python3` (uv is used if available).
When a new version comes out, run `./build.sh` again. The app's built-in auto-update is disabled.

## How it works

| Step | Details |
|---|---|
| Download | Installer verified against the sha512 in `pc-connect-update.gamesir-cdn.com/latest.yml` |
| Runtime | Same Electron as the original, 13.6.9 (linux-x64, sha256 verified) |
| node-hid | Replaced with the official N-API prebuild (hidraw backend) |
| usb-detection | Prebuild has symbol mismatches with Electron 13.6.9 → JS implementation polling `/sys/bus/usb` (`patches/overrides/usb-detection`) |
| iconv | Windows binary only → iconv-lite based implementation |
| ffi-napi | Only used for Windows DLLs, and crashes (SIGSEGV) against system libffi → stub (`Library` throws) |
| JS patches | `patches/patches.js` — exact string replacements; the build fails if the original changes |
| Fonts | Windows font names mapped to the app's bundled Source Han Sans for CJK text |
| Firmware | The official JieLi DLL is emulated with Unicorn (`patches/files/linux/jlemu.py`) |

### Firmware updates (`jlemu.py`)

The Cyclone 2 and its dongle use JieLi chips. The app reboots the controller into UBOOT (USB mass storage) mode,
and `jl_firmware_upgrade_x86.dll` writes the `.ufw` file with SCSI commands.
On Linux, **the same DLL runs unmodified in an x86 emulator**, and only device I/O is routed to `SG_IO`.
The upgrade logic (loader, KEY, CRC, block writes) is identical to the original.

```bash
PY=~/.local/share/gamesir-connect/resources/pyenv/bin/python
DFU=~/.local/share/gamesir-connect/resources/dfu
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll ufwinfo $DFU/packages/GS_C2_DEVICE_V123_Key.ufw
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll --device /dev/sgN --trace query            # read-only
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll --device /dev/sgN --trace upgrade --readonly X.ufw  # rehearsal with writes blocked
```

⚠ Like the original app, **when a UBOOT-mode device is connected, the app automatically writes the matching firmware.**
The real write path has not been tested on hardware yet (the test device was already up to date).

## Status
Tested on Fedora 44 with a Cyclone 2 over the wireless dongle: device detection, reading all settings,
and writing a setting (then reverting it, verified byte-identical) all work.

## Docs
- `docs/PROTOCOL.md` — app structure and the Cyclone 2 HID config protocol
- `docs/FIRMWARE.md` — firmware update flow and DLL analysis

## License
The scripts and patches in this repository are MIT licensed (`LICENSE`).
GameSir Connect itself, its DLLs and firmware are GameSir's work and are not included in this repository.
Firmware updates can brick your device; use them at your own risk.
