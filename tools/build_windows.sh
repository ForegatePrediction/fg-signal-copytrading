#!/bin/sh
# Cross-package the Windows x64 desktop app on macOS or Linux.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src="$root/src"
electron=electron-v45.4.1-win32-x64.zip
python=python-3.13.17-embed-amd64.zip
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
runtime=${1:-"${TG_RUNTIME_CACHE:-$tmp}"}
package="$tmp/TGCopyTrader"

. "$root/tools/build_common.sh"

fetch_runtime "$runtime" "$electron" 2ba6223afa9121f42831fefe92c434334907540165294fc56697449d12e7cfe9 \
    "https://github.com/electron/electron/releases/download/v45.4.1/$electron"
fetch_runtime "$runtime" "$python" 9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38 \
    "https://www.python.org/ftp/python/3.13.17/$python"

unzip -q "$runtime/$electron" -d "$package"
mv "$package/electron.exe" "$package/TGCopyTrader.exe"
if [ -f "$package/electron_wer.dll" ]; then
    mv "$package/electron_wer.dll" "$package/TGCopyTrader_wer.dll"
fi
resources="$package/resources"
rm -f "$resources/default_app.asar"
mkdir -p "$resources/app/ui" "$resources/python" "$resources/backend" "$resources/vendor" "$root/dist"
unzip -q "$runtime/$python" -d "$resources/python"
printf '%s\r\n' python313.zip . ../backend ../vendor > "$resources/python/python313._pth"
for item in package.json main.cjs preload.cjs backend.cjs; do
    cp "$src/windows/$item" "$resources/app/"
done
for item in api.html api.css api.js AppIcon.png; do
    cp "$src/ui/$item" "$resources/app/ui/"
done
rsync -a --exclude '.*' --exclude __pycache__ --exclude '*.pyc' "$src/copytrader/" "$resources/backend/copytrader/"
install_dependencies "$resources/vendor" "$root/requirements.txt"

printf '\357\273\277' > "$package/README.txt"
cat >> "$package/README.txt" <<'EOF'
信号跟单 · ForeGate API 工作台

Windows 10/11（x64）
1. 把 ZIP 完整解压到自己的文件夹。
2. 打开解压目录，双击 TGCopyTrader.exe。窗口出现后选择连接已有账户或创建新 API 账户。
3. 使用 ForeGate 开通的 APP Key、APP Secret 和 API Key。连接已有账户需要原 marchantUserId 和 ForeGate UID。

运行组件已经随包提供。请保留入口旁的 resources 文件夹及其他文件。
本机数据保存在当前 Windows 用户的 %APPDATA%\TGCopyTrader 下。凭证通过 Windows DPAPI 加密。
本应用没有自有发布者证书，受管理的电脑可能需要管理员批准。
EOF

version=$(node -p 'require(process.argv[1]).version' "$src/windows/package.json")
name="信号跟单-${version}-Windows-x64.zip"
(cd "$tmp" && zip -q -r "$name" TGCopyTrader)
unzip -tq "$tmp/$name"
mv -f "$tmp/$name" "$root/dist/$name"
shasum -a 256 "$root/dist/$name"
