# GameSir Connect 1.16.7 분석 — 앱 구조 & Cyclone 2 설정 프로토콜

출처: 공식 설치 파일 `GameSir Connect Setup 1.16.7.exe`
(sha512 `+z/Oq4aT…yQltw==`, `https://pc-connect-update.gamesir-cdn.com/latest.yml`과 일치)

## 앱 구조
- 프레임워크: Electron **13.6.9** (Chrome 91), 32비트 Windows 빌드만 있음 (`app-32.7z`)
- UI: electron-vue 보일러플레이트 (Vue 2 + Vuex + vue-i18n), webpack 번들
  - `dist/electron/main.js` (~0.9MB)
  - `dist/electron/renderer.js` (~5MB)
- 자동 업데이트: electron-updater, generic provider `https://gamesir.release.szhjc-tech.com/connect/`
- 네이티브 모듈 (`app.asar.unpacked/node_modules`)

| 모듈 | 용도 | Linux |
|---|---|---|
| node-hid 2.x | 설정 프로토콜 전체 | 재빌드 필요 (hidraw 백엔드) |
| usb-detection | 핫플러그 감지 | 재빌드 필요 |
| ffi-napi / ref-napi | Windows DLL 호출 | linux-x64 prebuild가 이미 들어 있음 |

- `resources/dfu/`의 Windows 전용 DLL

| DLL | 용도 | Cyclone 2 관련 |
|---|---|---|
| `XInputUtil_x86.dll` | XInput 연결 확인 / 진동 / caps | 보조 기능 |
| `jl_firmware_upgrade_x86.dll` | JieLi U-disk 펌웨어 업데이트 | **C2 펌웨어 업데이트** |
| `JL_upgradeUtility_x86.dll` (+ macOS `.dylib`) | 구형 JL 업데이트 | 다른 기종 |
| `sapjoyuniapis_*` / `mod_t3xs.dll` | T3 Pro, T4N2 등 다른 기종 SDK | 무관 |

## Windows 전용 코드 (포팅 시 패치 대상)
1. 이동식 디스크 열거: `powershell Get-WmiObject Win32_LogicalDisk`, 실패하면 `wmic`로 대체한다. 드라이브 문자를 반환한다.
2. 자동 업데이트의 설치 위치 판별: `reg.exe query … InstallLocation` (이미 `win32` 분기 안에 있음)
3. 창 포커스 판별: `user32.GetForegroundWindow` (이미 `win32` 분기 안, 다른 OS에서는 Electron API로 대체됨)
4. 월페이퍼 임시 폴더: `%PUBLIC%` (win32 분기 안)
5. XInputUtil DLL: init이 try/catch로 감싸져 있어 실패해도 로그만 남는다.
6. worker의 모듈 경로가 `'\\app.asar.unpacked\\node_modules\\'`로 백슬래시 하드코딩되어 있다 (`dfu/*Woker.js`).

## Cyclone 2 장치 정보
VID `0x3537` (13623). 앱의 장치 테이블 기준이다.

| 모드 | PID | 펌웨어 패키지 |
|---|---|---|
| 유선 | `0x101d` (4125) | `GS_C2_DEVICE_V123_Key.ufw` (컨트롤러 v1.2.3) |
| 무선 (동글) | `0x102a` (4138) | `GS_C2_Dongle_V121_Key.ufw` (동글 v1.2.1) |
| 유선, ADC 변형 | `0x1053` (4179) | `GS_C2_ADC_DEVICE_V356_Key.ufw` (v3.5.6) |
| 무선 (동글) | **`0x100b` (4107)** ← 현재 연결된 장치 | 동글 v1.2.1 |

- 현재 장치는 `bcdDevice 1.21`이므로 동글 펌웨어는 이미 최신(1.2.1)으로 보인다.
- 설정 채널: `UsagePage 0xFFF0` 컬렉션 (Linux에서는 IF1 hidraw)
  - 출력 Report ID `0x0F`, 63바이트
  - 입력 Report ID `0x10`, `0x12`
- 지원 기능 (UI): 버튼 리맵(19키), 매크로, 프로필 1–4 + Shift + Light 프로필, RGB(키프레임), 데드존/커브, 진동(모터 4개), 연사, 캘리브레이션

## 명령 (Report `0x0F` + 바이트들)

| 명령 | 바이트 | 비고 |
|---|---|---|
| 펌웨어 버전 읽기 | `0F 09` | 읽기 |
| 현재 프로필 번호 읽기 | `0F 0B` | 읽기 |
| RGB 상태 읽기 | `0F 0D` | 읽기 |
| 장치 정보 읽기 | `0F 04 <n> 03 9D 07` | 읽기 |
| 하트비트 | `0F F2 <n>` | |
| 진동 | `0F 20 66 55 <L> <R>` | 1초 뒤 `0,0`으로 정지 |
| 라이트 재생/정지 | `0F 0D <play> <frame>` | |
| 장치 ID 쓰기 | `0F 03 <n> 03 9E 06 <ascii…>` | 쓰기 |
| 프로필 쓰기 | 64바이트 단위로 분할 (`getWriteProfileCommand`) | 쓰기 |
| **업데이트 모드 진입 (C2)** | `0F 17 55 88` | ⚠ UBOOT 재부팅 |
| 업그레이드 모드 진입 (구형) | `0F FC` | ⚠ |

- 프로필 읽기 순서 (앱 로드 시): 1, 2, 3, 4, Shift, Light 프로필 → FW 버전 → 현재 프로필
- 각 명령은 ACK 대기 큐(`writeWithAck`)로 전송된다.
- TODO: 프로필 바이너리 레이아웃은 Deadband `RESEARCH.md`와 교차 검증해야 한다.
