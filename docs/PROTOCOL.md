# GameSir Connect 1.16.7 analysis — app structure & Cyclone 2 config protocol

Source: official installer `GameSir Connect Setup 1.16.7.exe`
(sha512 `+z/Oq4aT…yQltw==`, matches `https://pc-connect-update.gamesir-cdn.com/latest.yml`)

## App structure
- Framework: Electron **13.6.9** (Chrome 91), shipped as a 32-bit Windows build only (`app-32.7z`)
- UI: electron-vue boilerplate (Vue 2 + Vuex + vue-i18n), webpack bundles
  - `dist/electron/main.js` (~0.9 MB)
  - `dist/electron/renderer.js` (~5 MB)
- Auto-update: electron-updater, generic provider `https://gamesir.release.szhjc-tech.com/connect/`
- Native modules (`app.asar.unpacked/node_modules`)

| Module | Purpose | On Linux (this repo) |
|---|---|---|
| node-hid 2.x | The entire config protocol | Official N-API prebuild (hidraw backend) |
| usb-detection | Hotplug detection | Prebuild crashes on Electron 13.6.9 → sysfs-polling JS replacement |
| ffi-napi / ref-napi | Calling Windows DLLs | ffi-napi stubbed (crashes against system libffi); ref-napi linux-x64 prebuild is bundled |
| iconv | Used by ref-wchar-napi | Windows binary only → iconv-lite replacement |

- Windows-only DLLs in `resources/dfu/`

| DLL | Purpose | Relevance to Cyclone 2 |
|---|---|---|
| `XInputUtil_x86.dll` | XInput connection check / rumble / caps | Auxiliary |
| `jl_firmware_upgrade_x86.dll` | JieLi U-disk firmware update | **C2 firmware update** |
| `JL_upgradeUtility_x86.dll` (+ macOS `.dylib`) | Older JL update | Other models |
| `sapjoyuniapis_*` / `mod_t3xs.dll` | SDKs for T3 Pro, T4N2 and other models | None |

## Windows-only code (patch targets for the port)
1. Removable disk enumeration: `powershell Get-WmiObject Win32_LogicalDisk`, falling back to `wmic`. Returns drive letters.
2. Install location detection for auto-update: `reg.exe query … InstallLocation` (already inside a `win32` branch)
3. Window focus detection: `user32.GetForegroundWindow` (already inside a `win32` branch; other OSes use the Electron API)
4. Wallpaper temp folder: `%PUBLIC%` (inside a `win32` branch)
5. XInputUtil DLL: init is wrapped in try/catch, so failure only logs.
6. Worker module paths are hardcoded with backslashes as `'\\app.asar.unpacked\\node_modules\\'` (`dfu/*Woker.js`).

## Cyclone 2 device info
VID `0x3537` (13623), per the app's device table.

| Mode | PID | Firmware package |
|---|---|---|
| Wired | `0x101d` (4125) | `GS_C2_DEVICE_V123_Key.ufw` (controller v1.2.3) |
| Wireless (dongle) | `0x102a` (4138) | `GS_C2_Dongle_V121_Key.ufw` (dongle v1.2.1) |
| Wired, ADC variant | `0x1053` (4179) | `GS_C2_ADC_DEVICE_V356_Key.ufw` (v3.5.6) |
| Wireless (dongle) | **`0x100b` (4107)** ← tested device | dongle v1.2.1 |

- The dongle enumerates as `0x0575` ("GameSir-Cyclone 2") while the controller is off or asleep. The app does not recognize this PID.
- The tested dongle reports `bcdDevice 1.21`, so its firmware is the latest (1.2.1). The controller reports 3.5.6 (ADC variant, also the latest).
- Config channel: the `UsagePage 0xFFF0` collection (IF1 hidraw on Linux)
  - Output Report ID `0x0F`, 63 bytes
  - Input Report IDs `0x10`, `0x12`
- Features (UI): button remap (19 keys), macros, profiles 1–4 + Shift + Light profile, RGB (keyframes), deadzones/curves, rumble (4 motors), turbo, calibration

## Commands (Report `0x0F` + bytes)

| Command | Bytes | Notes |
|---|---|---|
| Read firmware version | `0F 09` | Read; reply holds ASCII digits, e.g. `30 33 35 36` = "0356" |
| Read current profile number | `0F 0B` | Read |
| Read RGB state | `0F 0D` | Read |
| Read device info | `0F 04 <n> 03 9D 07` | Read |
| Heartbeat | `0F F2 <n>` | |
| Rumble | `0F 20 66 55 <L> <R>` | Stopped with `0,0` after 1 s |
| Light play/stop | `0F 0D <play> <frame>` | |
| Write device ID | `0F 03 <n> 03 9E 06 <ascii…>` | Write |
| Write profile | Split into 64-byte chunks (`getWriteProfileCommand`) | Write; device ACKs with `10 06 …` |
| **Enter update mode (C2)** | `0F 17 55 88` | ⚠ Reboots into UBOOT |
| Enter upgrade mode (older) | `0F FC` | ⚠ |

- Profile read order when the app loads: profiles 1, 2, 3, 4, Shift, Light → FW version → current profile
- Each command goes through an ACK-waiting queue (`writeWithAck`).
- TODO: cross-check the profile binary layout against Deadband's `RESEARCH.md`.
