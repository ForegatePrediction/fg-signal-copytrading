#!/bin/sh
# Build a self-contained Apple silicon .app from the checked-out source.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src="$root/src"
app=${1:-"$root/dist/信号跟单.app"}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
. "$root/tools/build_common.sh"

if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    printf 'Build the macOS app on an Apple silicon Mac.\n' >&2
    exit 1
fi
if [ -e "$app" ] || [ -L "$app" ]; then
    printf 'Output path already exists: %s\n' "$app" >&2
    exit 1
fi

runtime=${TG_RUNTIME_CACHE:-"$tmp"}
python=cpython-3.12.14+20260825-aarch64-apple-darwin-install_only_stripped.tar.gz
fetch_runtime "$runtime" "$python" 8b0f1fa71eab7ca644e482c631807a1116fa848491051cd1c8d9429491de63a6 \
    "https://github.com/astral-sh/python-build-standalone/releases/download/20260825/$python"

bundle="$tmp/信号跟单.app"
resources="$bundle/Contents/Resources"
mkdir -p "$bundle/Contents/MacOS" "$resources/app" "$resources/runtime" "$resources/vendor"
install -m 644 "$src/macos/Info.plist" "$bundle/Contents/"
install -m 644 "$src/macos/AppIcon.icns" "$resources/"
tar -xzf "$runtime/$python" -C "$resources/runtime" --strip-components 1

if [ -z "${PYTHON:-}" ]; then
    PYTHON="$resources/runtime/bin/python3.12"
fi
install_dependencies "$resources/vendor" "$root/requirements-macos.txt"
for item in copytrader ui config fixtures; do
    mkdir -p "$resources/app/$item"
    rsync -a --exclude '.*' --exclude __pycache__ --exclude '*.pyc' "$src/$item/" "$resources/app/$item/"
done
swiftc -target arm64-apple-macos13.0 -module-cache-path "$tmp/modules" "$src/macos/Client.swift" \
    -o "$bundle/Contents/MacOS/TGCopyTrader"
codesign --force --sign - "$bundle"
codesign --verify --deep --strict "$bundle"
mkdir -p "$(dirname -- "$app")"
mv "$bundle" "$app"
