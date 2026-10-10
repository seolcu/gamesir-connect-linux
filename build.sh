#!/usr/bin/env bash
# GameSir Connect (Windows) -> Linux local repackager.
# Downloads the official installer and converts it on this machine only.
# No GameSir code is included in this repository.
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
  command -v "$c" >/dev/null || die "required command not found: $c"
done

mkdir -p "$CACHE"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 1. Official installer (verified against the sha512 in latest.yml)
if [[ -n "${INSTALLER:-}" ]]; then
  setup="$INSTALLER"
  log "Using provided installer: $setup"
else
  log "Checking update feed"
  feed="$(curl -fsSL "$FEED/latest.yml" | tr -d '\r')"
  ver="$(sed -n 's/^version: *//p' <<<"$feed")"
  rel="$(sed -n 's/^path: *//p' <<<"$feed")"
  sha="$(sed -n 's/^sha512: *//p' <<<"$feed")"
  [[ -n "$ver" && -n "$rel" && -n "$sha" ]] || die "failed to parse latest.yml"
  setup="$CACHE/GameSir-Connect-Setup-$ver.exe"
  if [[ ! -f "$setup" ]] || [[ "$(openssl dgst -sha512 -binary "$setup" | base64 -w0)" != "$sha" ]]; then
    log "Downloading GameSir Connect $ver"
    curl -fL --progress-bar -o "$setup.part" "$FEED/${rel// /%20}"
    mv "$setup.part" "$setup"
  fi
  got="$(openssl dgst -sha512 -binary "$setup" | base64 -w0)"
  [[ "$got" == "$sha" ]] || die "installer sha512 mismatch"
  log "sha512 verified ($ver)"
fi

# 2. Unpack the installer
log "Extracting installer"
7z x -y -o"$work/nsis" "$setup" >/dev/null
appz="$(find "$work/nsis" -name 'app-*.7z' | head -1)"
[[ -n "$appz" ]] || die "app-*.7z not found in installer"
7z x -y -o"$work/win" "$appz" >/dev/null
res="$work/win/resources"

# 3. Electron (linux-x64), sha256 verified
ez="$CACHE/electron-v$ELECTRON_VER-linux-x64.zip"
if [[ ! -f "$ez" ]]; then
  log "Downloading Electron $ELECTRON_VER"
  curl -fL --progress-bar -o "$ez.part" "https://github.com/electron/electron/releases/download/v$ELECTRON_VER/electron-v$ELECTRON_VER-linux-x64.zip"
  mv "$ez.part" "$ez"
fi
curl -fsSL -o "$CACHE/SHASUMS256-$ELECTRON_VER.txt" "https://github.com/electron/electron/releases/download/v$ELECTRON_VER/SHASUMS256.txt"
want="$(grep " \*electron-v$ELECTRON_VER-linux-x64.zip\$" "$CACHE/SHASUMS256-$ELECTRON_VER.txt" | cut -d' ' -f1)"
[[ "$(sha256sum "$ez" | cut -d' ' -f1)" == "$want" ]] || die "Electron sha256 mismatch"

stage="$work/stage"
mkdir -p "$stage"
unzip -q "$ez" -d "$stage"
rm -f "$stage/resources/default_app.asar"

# 4. app.asar -> resources/app (kept as a directory, not repacked)
log "Extracting app.asar"
asar="$CACHE/tools/node_modules/.bin/asar"
[[ -x "$asar" ]] || npm --prefix "$CACHE/tools" install --silent @electron/asar@3.2.17 >/dev/null
"$asar" extract "$res/app.asar" "$stage/resources/app"
cp -r "$res/dfu" "$stage/resources/dfu"
cp "$res/app-update.yml" "$stage/resources/" 2>/dev/null || true

# 5. Replace native modules with Linux builds
log "Replacing native modules"
nm="$stage/resources/app/node_modules"
fetch() { [[ -f "$CACHE/$1" ]] || curl -fsSL -o "$CACHE/$1" "$2"; }
fetch "node-hid-$NODE_HID_VER.tgz" "https://github.com/node-hid/node-hid/releases/download/v$NODE_HID_VER/node-hid-v$NODE_HID_VER-napi-v3-linux-x64.tar.gz"
# Remove Windows binaries: node-gyp-build prefers build/Release over prebuilds
find "$nm" \( -path '*/bin/win32-*' -o -path '*/build/Release' \) -prune -exec rm -rf {} +
tar xzf "$CACHE/node-hid-$NODE_HID_VER.tgz" -C "$nm/node-hid"
# Replace some native modules with JS implementations:
#  - usb-detection: prebuild has V8 symbol mismatches with Electron 13.6.9 -> sysfs polling
#  - iconv: ships Windows binaries only -> iconv-lite
#  - ffi-napi: only used for Windows DLLs, crashes against system libffi -> stub
for o in "$HERE"/patches/overrides/*/; do
  m="$(basename "$o")"
  rm -rf "$nm/$m"
  cp -r "$o" "$nm/$m"
done

# 6. JS patches
log "Applying JS patches"
node "$HERE/patches/apply.js" "$stage/resources"

# 7. Python environment for firmware updates (emulates the official JieLi DLL with Unicorn)
log "Preparing firmware update tooling (unicorn, pefile)"
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
  || die "firmware tooling self-test failed (jlemu ufwinfo)"

# 8. Install
log "Installing to $PREFIX"
rm -rf "$PREFIX.new"
mv "$stage" "$PREFIX.new"
rm -rf "$PREFIX"
mv "$PREFIX.new" "$PREFIX"
chmod 4755 "$PREFIX/chrome-sandbox" 2>/dev/null || true

install -Dm755 "$HERE/gamesir-connect" "$HOME/.local/bin/gamesir-connect"
sed "s|@PREFIX@|$PREFIX|g; s|@BIN@|$HOME/.local/bin/gamesir-connect|g" "$HERE/gamesir-connect.desktop.in" \
  > "$HOME/.local/share/applications/gamesir-connect.desktop"
install -Dm644 "$PREFIX/resources/app/dist/electron/static/icon.ico" "$PREFIX/icon.ico"

log "Done. Run: gamesir-connect"
if [[ ! -f /etc/udev/rules.d/70-gamesir.rules ]]; then
  echo
  echo "Install the udev rule once so your user can access the controller:"
  echo "  sudo install -m644 '$HERE/70-gamesir.rules' /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi
