# macOS · 安装与构建

## 安装和首次运行

适用 Apple 芯片（M 系列）的 Mac，最低系统版本为 macOS 13。

1. 下载 `TGCopyTrader-macOS-arm64.dmg`。如发布页附有 `SHA256SUMS.txt`，可在下载目录运行 `shasum -a 256 "TGCopyTrader-macOS-arm64.dmg"`，核对对应的摘要。
2. 打开 DMG，将「信号跟单」拖到「应用程序」，然后从「应用程序」启动。
3. 首次进入时按 [使用指南](user-guide.md) 连接已有 API 账户或创建新账户。通过「账户概览」核对 UID 和余额。

本版采用临时签名，尚未使用 Developer ID 签名或提交 Apple 公证。遇到系统拦截时，确认来源可信后，在「系统设置 → 隐私与安全性」检查是否可以选择「仍要打开」。受管理设备的系统策略可能限制开启。 [Apple 安全开启应用说明](https://support.apple.com/zh-cn/102445)

首次保存或读取 API 凭证时，macOS 可能请求钥匙串访问权限。业务数据在 `~/Library/Application Support/TG信号跟单/`，API 凭证保存在本机钥匙串。退出应用后可以替换「应用程序」里的应用包，原账户的本机记录仍在该数据目录中。

## 从源码构建

在 Apple 芯片 Mac 的终端中执行。构建需要 Xcode Command Line Tools（`xcode-select --install`）、`curl`、`rsync`、`shasum`、`codesign` 和 `hdiutil`。首次构建从 GitHub 获取已固定且校验 SHA-256 的 Python 3.12.14 独立运行时，并从 PyPI 安装锁定依赖。 [Python Build Standalone 官方发布](https://github.com/astral-sh/python-build-standalone/releases/tag/20260825)

在仓库根目录生成 DMG：

```sh
./tools/build_dmg.sh
```

只构建 `.app` 供本机开发，输出路径需尚不存在：

```sh
./tools/rebuild_client.sh "dist/信号跟单-开发.app"
```

下载慢或需重复构建时，可设置 `TG_RUNTIME_CACHE` 指向本地运行时缓存目录。有离线依赖轮文件时可设置 `TG_WHEELHOUSE`，其中应包含 `requirements-macos.txt` 所列版本和适用于 macOS arm64、Python 3.12 的构建。默认使用下载的独立 Python 来安装依赖，可用 `PYTHON` 指定已配好 pip 的等价构建解释器。

构建脚本会验证应用的临时签名与 DMG。发布前在一台 Apple 芯片 Mac 上安装，至少检查首次启动、账户选择与退出重开后的凭证读取。
