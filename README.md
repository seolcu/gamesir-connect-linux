# GameSir Connect for Linux (로컬 리패키저)

GameSir 공식 설정 앱 **GameSir Connect**(Windows 전용)를 Linux에서 원본 그대로 실행하기 위한 변환 스크립트입니다.
Cyclone 2 (`3537:100b` 등)를 기준으로 작업했습니다.

> 이 저장소에는 GameSir의 코드가 들어 있지 않습니다. `build.sh`가 공식 서버에서 설치 파일을 받아
> **사용자의 PC에서만** 변환합니다. GameSir와 무관한 비공식 프로젝트입니다.

## 설치

```bash
./build.sh            # ~/.local/share/gamesir-connect 에 설치, 런처 등록
sudo install -m644 70-gamesir.rules /etc/udev/rules.d/ \
  && sudo udevadm control --reload-rules && sudo udevadm trigger   # 최초 1회
gamesir-connect
```

필요한 것: `curl 7z unzip node npm openssl python3` (uv가 있으면 사용).
새 버전이 나오면 `./build.sh`를 다시 실행하세요. 앱 자체의 자동 업데이트는 꺼져 있습니다.

## 동작 방식

| 단계 | 내용 |
|---|---|
| 다운로드 | `pc-connect-update.gamesir-cdn.com/latest.yml`의 sha512로 설치 파일 검증 |
| 런타임 | 원본과 같은 Electron 13.6.9 (linux-x64, sha256 검증) |
| node-hid | 공식 N-API prebuild(hidraw 백엔드)로 교체 |
| usb-detection | prebuild가 Electron 13.6.9와 심볼 불일치 → `/sys/bus/usb` 폴링 JS 구현 (`patches/overrides/usb-detection`) |
| iconv | Windows 바이너리뿐 → iconv-lite 기반 구현 |
| ffi-napi | Windows DLL 호출 전용 + 시스템 libffi와 충돌로 SIGSEGV → 스텁 (Library는 오류) |
| JS 패치 | `patches/patches.js` — 문자열 정확 일치 치환, 원본이 바뀌면 빌드 실패 |
| 펌웨어 | 공식 JieLi DLL을 Unicorn으로 에뮬레이션 (`patches/files/linux/jlemu.py`) |

### 펌웨어 업데이트 (`jlemu.py`)

Cyclone 2/동글은 JieLi 칩입니다. 앱은 컨트롤러를 UBOOT(USB 저장장치) 모드로 재부팅한 뒤,
`jl_firmware_upgrade_x86.dll`이 SCSI 명령으로 `.ufw`를 씁니다.
Linux에서는 **같은 DLL을 x86 에뮬레이터에서 그대로 실행**하고, 장치 입출력만 `SG_IO`로 연결합니다.
업그레이드 로직(로더, KEY, CRC, 블록 쓰기)은 원본과 동일합니다.

```bash
PY=~/.local/share/gamesir-connect/resources/pyenv/bin/python
DFU=~/.local/share/gamesir-connect/resources/dfu
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll ufwinfo $DFU/packages/GS_C2_DEVICE_V123_Key.ufw
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll --device /dev/sgN --trace query            # 읽기 전용
$PY -I $DFU/linux/jlemu.py --dll $DFU/jl_firmware_upgrade_x86.dll --device /dev/sgN --trace upgrade --readonly X.ufw  # 쓰기 차단 리허설
```

⚠ 원본 앱과 마찬가지로, **UBOOT 모드 장치가 연결되면 앱이 자동으로 맞는 펌웨어를 씁니다.**

## 문서
- `docs/PROTOCOL.md` — 앱 구조, Cyclone 2 HID 설정 프로토콜
- `docs/FIRMWARE.md` — 펌웨어 업데이트 흐름과 DLL 분석

## 라이선스
이 저장소의 스크립트와 패치는 MIT 라이선스입니다 (`LICENSE`).
GameSir Connect 본체, DLL, 펌웨어는 GameSir의 저작물이며 이 저장소에 포함되지 않습니다.
펌웨어 업데이트는 기기를 망가뜨릴 수 있으니 본인 책임으로 사용하세요.
