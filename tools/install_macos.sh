#!/bin/sh
# Install the public Apple silicon release for the current user.
set -eu
repo=${1:-}
if ! printf '%s\n' "$repo" | grep -Eq '^[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*$'; then
    echo "Usage: $0 OWNER/REPO" >&2
    exit 2
fi
if [ "$(uname -s)" != Darwin ] || [ "$(sysctl -n hw.optional.arm64)" != 1 ] || [ "$(sw_vers -productVersion | cut -d . -f 1)" -lt 13 ]; then
    echo "Requires macOS 13+ on Apple silicon." >&2
    exit 1
fi
base="https://github.com/$repo/releases/latest/download"
name=TGCopyTrader-macOS-arm64.dmg
apps=${TG_INSTALL_DIR:-"$HOME/Applications"}
dest="$apps/信号跟单.app"
mkdir -p "$apps"
tmp=$(mktemp -d "$apps/.tg-install.XXXXXXXX")
mounted=0
cleanup() {
    if [ "$mounted" = 1 ]; then
        hdiutil detach "$tmp/mount" -quiet || return 1
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT
for file in SHA256SUMS.txt "$name"; do
    curl --proto '=https' --proto-redir '=https' -fsSL --retry 2 "$base/$file" -o "$tmp/$file"
done
expected=$(awk -v name="$name" '$2 == name {print $1}' "$tmp/SHA256SUMS.txt")
actual=$(shasum -a 256 "$tmp/$name" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || { echo 'Release checksum mismatch.' >&2; exit 1; }
mkdir "$tmp/mount"
hdiutil attach -quiet -readonly -nobrowse -mountpoint "$tmp/mount" "$tmp/$name"
mounted=1
source="$tmp/mount/信号跟单.app"
codesign --verify --deep --strict "$source"
if [ -e "$dest" ] || [ -L "$dest" ]; then
    if [ -d "$dest" ] && diff -qr "$source" "$dest" >/dev/null; then
        printf 'Already installed: %s\n' "$dest"
        exit 0
    fi
    echo "A different application exists at $dest. Move it before installing." >&2
    exit 1
fi
ditto "$source" "$tmp/app"
xattr -w com.apple.quarantine "0081;$(printf '%x' "$(date +%s)");TGCopyTraderInstaller;" "$tmp/app"
mv "$tmp/app" "$dest"
printf 'Installed: %s\n' "$dest"
