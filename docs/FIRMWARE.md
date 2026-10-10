# Cyclone 2 펌웨어 업데이트 흐름 (GameSir Connect 1.16.7)

## 요약
Cyclone 2와 동글은 **JieLi(杰理) BR23/BR27 계열 칩**이다. 펌웨어는 JieLi `.ufw` 포맷이다.
업데이트는 기기를 칩 ROM의 **UBOOT(USB 저장장치) 모드**로 재부팅한 뒤, 그 디스크에 **SCSI 벤더 명령**으로 써 넣는 방식이다.

## 순서
1. 앱이 설정 채널로 `0F 17 55 88`을 보낸다 (`getQuickUpdateCommand`).
2. 기기가 재부팅하면서 **VID `0x4C4A`(19530, ASCII "JL")**의 USB 대용량 저장장치로 다시 잡힌다.
   - 장치 설명 문자열에 `BR23UBOOT` 또는 `BR27UBOOT`가 들어 있다.
   - 앱은 이 VID가 추가되는 것을 감지해 업그레이드 루틴을 시작한다.
3. 앱이 이동식 디스크의 드라이브 문자를 모두 순회하면서 다음을 한다.
   - `JL_loadUfwData(공통 UFW)` → `JL_queryDevicePidVid(드라이브)` → `JL_queryDeviceDesc(드라이브)`로 기기를 식별한다.
   - 공통 UFW 후보는 `Key | No_Key | KZ | ZW` × 칩 `695 | AD160 | AC632N | JL703N` 조합이다.
4. worker(`dfu/udiskUpgradWoker.js`)가 실제 업데이트를 한다.
   - `JL_loadUfwData(대상 ufw)` → `JL_getUfwPidVid` / `JL_getUfwCrc`
   - `JL_queryDevicePidVid(드라이브)`로 **UFW PID와 기기 PID가 일치하는지** 검사한다.
   - `JL_setDualBackupBurning(0)` (C2는 듀얼 백업 대상이 아님)
   - `JL_upgradeDevice(드라이브, false, true, null)`로 쓰기를 하고 진행률은 콜백으로 받는다.
5. 오류 코드는 `dfu/JL_ErrorCode.ts`에 있다 (-1 ~ -27). 예: `-3` KEY 불일치, `-25` 칩 불일치.

## Linux 포팅에서 막히는 지점
- `jl_firmware_upgrade_x86.dll`은 `CreateFile("\\.\X:")` + `DeviceIoControl`(SCSI pass-through)로 디스크에 접근한다.
  - 내부 클래스는 `jl_usb_scsi_windows` / `jl_usb_scsi_common`이다.
  - **입출력 계층이 분리된 구조**라서, 같은 SCSI CDB를 Linux `SG_IO`(`/dev/sgN` 또는 `/dev/sdX`)로 보내면 된다.
- export 목록에 `*ByPath` 변형(`JL_upgradeDeviceByPath`, `JL_queryDevicePidVidByPath` 등)이 있다. 드라이브 문자 대신 경로를 받는 API다.
- 필요한 리버싱 범위
  - (a) UFW 파일 파싱과 CRC
  - (b) UBOOT SCSI 명령 세트: 식별, 지우기, 쓰기, CRC 검증, 리셋
  - (c) "Key" 처리 (칩 키별 암호화 여부)
  - 커뮤니티의 JieLi UBOOT 연구(예: kagaimiq의 jl-uboot-tool)가 (b)의 참고 자료가 된다.

## DLL 분석 결과 (Ghidra)
- 공개 API(`JL_loadUfwData`, `JL_queryDevicePidVid`, `JL_upgradeDevice` …)는 모두 같은 패턴의 래퍼다.
  - 전역 변수에 인자를 넣는다.
  - 명령 코드(1=UFW 로드, 2=PID 조회, 3=업그레이드, 4=ConfigE 조회)를 설정한다.
  - `SetEvent`로 작업 스레드(`0x10001730`)를 깨운다.
  - `WaitForSingleObject`로 완료를 기다린다.
- DllMain은 자기 DLL 파일을 `fopen`으로 읽어 체크섬을 검사한다 (`0x10004140`). 실패하면 초기화하지 않는다.
- 칩별 로더 8개가 PE 리소스 `LOADER/103..114`에 암호화된 상태로 들어 있다.
- 칩 식별 문자열 등은 hex로 난독화되어 있다.
- SCSI: `IOCTL_SCSI_PASS_THROUGH_DIRECT`(0x4D014) 레이아웃

| 필드 | 값 |
|---|---|
| Cdb | 16바이트 |
| Sense | 32바이트 @ 0x30 |
| 버퍼 크기 | 0x50 |
| DataIn | 0 = 쓰기(OUT), 1 = 읽기(IN) |

- 장치 설명: `IOCTL_STORAGE_QUERY_PROPERTY`(0x2D1400)로 얻는다. Linux에서는 INQUIRY로 생성한다.

## Linux 구현: `jlemu.py`
- Unicorn으로 DLL을 로드하고 import를 Python 스텁으로 연결한다.
- GDT로 스레드별 FS(TEB)를 둔다.
- 작업 스레드는 협조적 전환으로 실행한다. 신호되지 않은 이벤트 대기 시 컨텍스트를 전환한다.
- 장치: `CreateFileA("\\.\E:")` → `/dev/sgN`, SPTD → `SG_IO`
- 구현하지 않은 API가 불리면 즉시 중단한다. C++ 예외(`RaiseException`)도 중단으로 처리한다.
- `--readonly`: DATA_OUT 명령을 장치에 보내지 않는다 (리허설용).
- 검증 완료 (장치 없이)

| 파일 | PID | 버전 | CRC |
|---|---|---|---|
| `GS_C2_DEVICE_V123_Key.ufw` | GS_C2_DEVICE | 1.23 | 30ea-e48eba69 |
| `GS_C2_Dongle_V121_Key.ufw` | GS_C2_Dongle | 1.21 | c15-c8584408 |
| `GS_C2_ADC_DEVICE_V356_Key.ufw` | GS_C2_ADC_DEVICE | 3.56 | 5819-83992bf5 |

- 미검증: 실제 UBOOT 장치를 대상으로 한 query/upgrade
  - 2026-10-11 기준으로 사용자 기기는 컨트롤러 3.5.6(ADC 변형), 동글 1.21이다. 둘 다 번들 최신이라 업데이트가 필요 없어 시험하지 않았다.

## 앱 동작 주의
- 앱은 시작할 때와 VID `0x4C4A` 장치가 연결될 때 이동식 디스크를 스캔한다.
- 그 결과 UBOOT 장치가 보이면 **사용자 확인 없이 맞는 패키지로 업그레이드를 시작한다** (원본 Windows 동작과 같음).

## 위험도
- UBOOT는 칩 ROM에 있다. 따라서 펌웨어 쓰기가 실패해도 **로고+메뉴 버튼을 누른 채 USB를 연결**하는 강제 업데이트로 대부분 재진입할 수 있다 (GameSir 공식 안내).
- 단, 이 복구 경로가 실제로 동작하는지는 아직 검증하지 않았다.
- PID 불일치나 KEY 불일치는 쓰기 전에 걸러진다. Linux 구현에서도 이 검사를 반드시 유지해야 한다.
