#!/bin/sh
# Package the Apple silicon app in a disk image and verify its checksum.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
stage="$tmp/stage"
mkdir -p "$stage" "$root/dist"
"$root/tools/rebuild_client.sh" "$stage/信号跟单.app"
version=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$stage/信号跟单.app/Contents/Info.plist")
name="信号跟单-${version}-macOS-arm64.dmg"
ln -s /Applications "$stage/Applications"
cat > "$stage/安装说明.txt" <<'EOF'
信号跟单 · ForeGate API 工作台

适用 Apple 芯片（M 系列）Mac，运行 macOS 13 或更新版本。
1. 将「信号跟单」拖入「Applications（应用程序）」。
2. 从「应用程序」打开「信号跟单」。
3. 本版采用临时签名。若系统拦截，确认来自你信任的发布者后，
   在「系统设置 → 隐私与安全性」查看是否可以选择「仍要打开」。
   受管理的 Mac 可能不提供这项选择。
4. 首次启动，选择「连接已有账户」或「创建新 API 账户」。
   准备 ForeGate 开通的 APP Key、APP Secret 和 API Key。
   已有账户还需要原 marchantUserId 和 ForeGate UID。
   凭证保存在本机钥匙串。
EOF
hdiutil create -volname "信号跟单 $version" -fs HFS+ -format UDZO -srcfolder "$stage" "$tmp/$name"
hdiutil verify "$tmp/$name"
mv -f "$tmp/$name" "$root/dist/$name"
shasum -a 256 "$root/dist/$name"
