#!/bin/sh
# Stage stable asset names for GitHub Releases.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root/dist"
version=$(node -p 'require(process.argv[1]).version' "$root/src/windows/package.json")
for platform in macOS-arm64.dmg Windows-x64.zip; do
    test -f "信号跟单-$version-$platform" || { echo "Missing $version $platform" >&2; exit 1; }
done
mkdir -p github
for platform in macOS-arm64.dmg Windows-x64.zip; do
    cp "信号跟单-$version-$platform" "github/TGCopyTrader-$platform"
done
cd github
shasum -a 256 TGCopyTrader-macOS-arm64.dmg TGCopyTrader-Windows-x64.zip > SHA256SUMS.txt
shasum -a 256 -c SHA256SUMS.txt
