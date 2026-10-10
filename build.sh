#!/usr/bin/env bash
# GameSir Connect (Windows) → Linux 로컬 리패키저.
# 공식 설치 파일을 받아서 이 PC에서만 변환한다. GameSir 코드는 이 저장소에 포함되지 않는다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local/share/gamesir-connect}"
CACHE="${CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/gamesir-connect-build}"
FEED="https://pc-connect-update.gamesir-cdn.com"

ELECTRON_VER=13.6.9
NODE_HID_VER=2.2.0

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

for c in curl 7z unzip node npm openssl sha256sum base64; do
  command -v "$c" >/dev/null || die "필요한 명령이 없습니다: $c"
done

mkdir -p "$CACHE"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 1. 공식 설치 파일 (latest.yml의 sha512로 검증)
if [[ -n "${INSTALLER:-}" ]]; then
  setup="$INSTALLER"
  log "지정된 설치 파일 사용: $setup"
else
  log "업데이트 피드 확인"
  feed="$(curl -fsSL "$FEED/latest.yml" | tr -d '\r')"
  ver="$(sed -n 's/^version: *//p' <<<"$feed")"
  rel="$(sed -n 's/^path: *//p' <<<"$feed")"
  sha="$(sed -n 's/^sha512: *//p' <<<"$feed")"
  [[ -n "$ver" && -n "$rel" && -n "$sha" ]] || die "latest.yml 파싱 실패"
  setup="$CACHE/GameSir-Connect-Setup-$ver.exe"
  if [[ ! -f "$setup" ]] || [[ "$(openssl dgst -sha512 -binary "$setup" | base64 -w0)" != "$sha" ]]; then
    log "GameSir Connect $ver 다운로드"
    curl -fL --progress-bar -o "$setup.part" "$FEED/${rel// /%20}"
    mv "$setup.part" "$setup"
  fi
  got="$(openssl dgst -sha512 -binary "$setup" | base64 -w0)"
  [[ "$got" == "$sha" ]] || die "설치 파일 sha512 불일치"
  log "sha512 검증 완료 ($ver)"
fi

# 2. 설치 파일 풀기
log "설치 파일 추출"
7z x -y -o"$work/nsis" "$setup" >/dev/null
appz="$(find "$work/nsis" -name 'app-*.7z' | head -1)"
[[ -n "$appz" ]] || die "app-*.7z 를 찾지 못함"
7z x -y -o"$work/win" "$appz" >/dev/null
res="$work/win/resources"

# 3. Electron (linux-x64), sha256 검증
ez="$CACHE/electron-v$ELECTRON_VER-linux-x64.zip"
if [[ ! -f "$ez" ]]; then
  log "Electron $ELECTRON_VER 다운로드"
  curl -fL --progress-bar -o "$ez.part" "https://github.com/electron/electron/releases/download/v$ELECTRON_VER/electron-v$ELECTRON_VER-linux-x64.zip"
  mv "$ez.part" "$ez"
fi
curl -fsSL -o "$CACHE/SHASUMS256-$ELECTRON_VER.txt" "https://github.com/electron/electron/releases/download/v$ELECTRON_VER/SHASUMS256.txt"
want="$(grep " \*electron-v$ELECTRON_VER-linux-x64.zip\$" "$CACHE/SHASUMS256-$ELECTRON_VER.txt" | cut -d' ' -f1)"
[[ "$(sha256sum "$ez" | cut -d' ' -f1)" == "$want" ]] || die "Electron sha256 불일치"

stage="$work/stage"
mkdir -p "$stage"
unzip -q "$ez" -d "$stage"
rm -f "$stage/resources/default_app.asar"

# 4. app.asar → resources/app (재패킹하지 않고 폴더로 둔다)
log "app.asar 추출"
asar="$CACHE/tools/node_modules/.bin/asar"
[[ -x "$asar" ]] || npm --prefix "$CACHE/tools" install --silent @electron/asar@3.2.17 >/dev/null
"$asar" extract "$res/app.asar" "$stage/resources/app"
cp -r "$res/dfu" "$stage/resources/dfu"
cp "$res/app-update.yml" "$stage/resources/" 2>/dev/null || true

# 5. 네이티브 모듈을 Linux 빌드로 교체
log "네이티브 모듈 교체"
nm="$stage/resources/app/node_modules"
fetch() { [[ -f "$CACHE/$1" ]] || curl -fsSL -o "$CACHE/$1" "$2"; }
fetch "node-hid-$NODE_HID_VER.tgz" "https://github.com/node-hid/node-hid/releases/download/v$NODE_HID_VER/node-hid-v$NODE_HID_VER-napi-v3-linux-x64.tar.gz"
# Windows 바이너리 제거: node-gyp-build는 build/Release를 prebuilds보다 먼저 고른다
find "$nm" \( -path '*/bin/win32-*' -o -path '*/build/Release' \) -prune -exec rm -rf {} +
tar xzf "$CACHE/node-hid-$NODE_HID_VER.tgz" -C "$nm/node-hid"
# 네이티브 모듈을 JS 구현으로 교체:
#  - usb-detection: prebuild가 Electron 13.6.9 V8과 심볼 불일치 → sysfs 폴링
#  - iconv: Windows 바이너리뿐 → iconv-lite
for o in "$HERE"/patches/overrides/*/; do
  m="$(basename "$o")"
  rm -rf "$nm/$m"
  cp -r "$o" "$nm/$m"
done

# 6. JS 패치
log "JS 패치 적용"
node "$HERE/patches/apply.js" "$stage/resources"

# 7. 펌웨어 업데이트용 파이썬 환경 (공식 JieLi DLL을 Unicorn으로 에뮬레이션)
log "펌웨어 업데이트 도구 환경 준비 (unicorn, pefile)"
pyenv="$stage/resources/pyenv"
PY_REQS=(unicorn==2.1.4 pefile==2024.8.26)
if command -v uv >/dev/null; then
  uv venv -q --python "$(command -v python3)" "$pyenv"
  VIRTUAL_ENV="$pyenv" uv pip install -q --only-binary :all: "${PY_REQS[@]}"
else
  python3 -m venv "$pyenv"
  "$pyenv/bin/pip" install -q --only-binary :all: "${PY_REQS[@]}"
fi
"$pyenv/bin/python" -I "$stage/resources/dfu/linux/jlemu.py" --dll "$stage/resources/dfu/jl_firmware_upgrade_x86.dll" \
  ufwinfo "$stage/resources/dfu/packages/GS_C2_DEVICE_V123_Key.ufw" 2>/dev/null | grep -q '"ret": 0' \
  || die "펌웨어 도구 자체 검사 실패 (jlemu ufwinfo)"

# 8. 설치
log "설치: $PREFIX"
rm -rf "$PREFIX.new"
mv "$stage" "$PREFIX.new"
rm -rf "$PREFIX"
mv "$PREFIX.new" "$PREFIX"
chmod 4755 "$PREFIX/chrome-sandbox" 2>/dev/null || true

install -Dm755 "$HERE/gamesir-connect" "$HOME/.local/bin/gamesir-connect"
sed "s|@PREFIX@|$PREFIX|g; s|@BIN@|$HOME/.local/bin/gamesir-connect|g" "$HERE/gamesir-connect.desktop.in" \
  > "$HOME/.local/share/applications/gamesir-connect.desktop"
install -Dm644 "$PREFIX/resources/app/dist/electron/static/icon.ico" "$PREFIX/icon.ico"

log "완료. 실행: gamesir-connect"
if [[ ! -f /etc/udev/rules.d/70-gamesir.rules ]]; then
  echo
  echo "컨트롤러 접근 권한을 위해 udev 규칙을 한 번 설치하세요:"
  echo "  sudo install -m644 '$HERE/70-gamesir.rules' /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi
