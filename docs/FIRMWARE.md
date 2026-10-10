# Cyclone 2 firmware update flow (GameSir Connect 1.16.7)

## Summary
The Cyclone 2 and its dongle use **JieLi BR23/BR27-family chips**. Firmware is in JieLi's `.ufw` format.
Updating reboots the device into the chip ROM's **UBOOT (USB mass storage) mode**, then writes to that disk with **vendor SCSI commands**.

## Sequence
1. The app sends `0F 17 55 88` over the config channel (`getQuickUpdateCommand`).
2. The device reboots and re-enumerates as a USB mass storage device with **VID `0x4C4A` (19530, ASCII "JL")**.
   - Its device description contains `BR23UBOOT` or `BR27UBOOT`.
   - The app detects this VID being added and starts the upgrade routine.
3. The app walks every removable drive letter and does the following:
   - Identifies the device with `JL_loadUfwData(common UFW)` → `JL_queryDevicePidVid(drive)` → `JL_queryDeviceDesc(drive)`.
   - Common UFW candidates are `Key | No_Key | KZ | ZW` × chip `695 | AD160 | AC632N | JL703N`.
4. A worker (`dfu/udiskUpgradWoker.js`) performs the actual update:
   - `JL_loadUfwData(target ufw)` → `JL_getUfwPidVid` / `JL_getUfwCrc`
   - `JL_queryDevicePidVid(drive)` checks that **the UFW PID matches the device PID**.
   - `JL_setDualBackupBurning(0)` (C2 does not use dual backup)
   - `JL_upgradeDevice(drive, false, true, null)` writes the firmware; progress is reported via callback.
5. Error codes are in `dfu/JL_ErrorCode.ts` (-1 to -27). For example, `-3` is KEY mismatch and `-25` is chip mismatch.

## Obstacles for a Linux port
- `jl_firmware_upgrade_x86.dll` accesses the disk via `CreateFile("\\.\X:")` + `DeviceIoControl` (SCSI pass-through).
  - Internal classes are `jl_usb_scsi_windows` / `jl_usb_scsi_common`.
  - The **I/O layer is separate**, so the same SCSI CDBs can be sent with Linux `SG_IO` (`/dev/sgN` or `/dev/sdX`).
- The exports include `*ByPath` variants (`JL_upgradeDeviceByPath`, `JL_queryDevicePidVidByPath`, etc.) that take a path instead of a drive letter.
- What would need reverse engineering for a reimplementation:
  - (a) UFW parsing and CRC
  - (b) The UBOOT SCSI command set: identify, erase, write, CRC verify, reset
  - (c) "Key" handling (per-chip-key encryption)
  - Community JieLi UBOOT research (e.g. kagaimiq's jl-uboot-tool) covers (b).

## DLL analysis (Ghidra)
- The public API (`JL_loadUfwData`, `JL_queryDevicePidVid`, `JL_upgradeDevice`, ...) are all wrappers with the same pattern:
  - Store the arguments in globals.
  - Set a command code (1 = load UFW, 2 = query PID, 3 = upgrade, 4 = query ConfigE).
  - Wake the worker thread (`0x10001730`) with `SetEvent`.
  - Wait for completion with `WaitForSingleObject`.
- DllMain reads its own DLL file with `fopen` and verifies a checksum (`0x10004140`). If that fails, it does not initialize.
- Eight per-chip loaders are stored encrypted as PE resources `LOADER/103..114`.
- Chip identification strings and the like are hex-obfuscated.
- SCSI: `IOCTL_SCSI_PASS_THROUGH_DIRECT` (0x4D014) layout

| Field | Value |
|---|---|
| Cdb | 16 bytes |
| Sense | 32 bytes @ 0x30 |
| Buffer size | 0x50 |
| DataIn | 0 = write (OUT), 1 = read (IN) |

- Device description comes from `IOCTL_STORAGE_QUERY_PROPERTY` (0x2D1400). On Linux it is synthesized from INQUIRY.

## Linux implementation: `jlemu.py`
- Loads the DLL with Unicorn and binds its imports to Python stubs.
- Uses a GDT to give each thread its own FS (TEB).
- Runs the worker thread with cooperative switching: waiting on an unsignaled event switches context.
- Device: `CreateFileA("\\.\E:")` → `/dev/sgN`, SPTD → `SG_IO`
- Any unimplemented API call aborts immediately. C++ exceptions (`RaiseException`) also abort.
- `--readonly`: DATA_OUT commands are never sent to the device (for rehearsals).
- Verified (without a device):

| File | PID | Version | CRC |
|---|---|---|---|
| `GS_C2_DEVICE_V123_Key.ufw` | GS_C2_DEVICE | 1.23 | 30ea-e48eba69 |
| `GS_C2_Dongle_V121_Key.ufw` | GS_C2_Dongle | 1.21 | c15-c8584408 |
| `GS_C2_ADC_DEVICE_V356_Key.ufw` | GS_C2_ADC_DEVICE | 3.56 | 5819-83992bf5 |

- Not yet verified: query/upgrade against a real UBOOT device.
  - As of 2026-10-11 the test device runs controller 3.5.6 (ADC variant) and dongle 1.21. Both match the latest bundled firmware, so no update was needed and none was attempted.

## App behavior warning
- The app scans removable disks at startup and whenever a VID `0x4C4A` device is connected.
- If it finds a UBOOT device, it **starts upgrading with the matching package without asking** (same as the original Windows behavior).

## Risk
- UBOOT lives in the chip ROM, so even if a firmware write fails, the device can usually re-enter it via the forced update method of **holding the logo + menu buttons while plugging in USB** (per GameSir's official instructions).
- This recovery path has not been verified yet.
- PID and KEY mismatches are rejected before writing. The Linux implementation must keep these checks.
