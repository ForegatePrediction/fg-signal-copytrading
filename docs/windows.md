# Windows · 安装与构建

## 安装和首次运行

适用 Windows 10/11 的 x64 系统。在「设置 → 系统 → 关于」查看系统类型。此包未提供 ARM64 原生版和 32 位版。

1. 下载 `TGCopyTrader-Windows-x64.zip`。如发布页附有 `SHA256SUMS.txt`，可在下载目录的 PowerShell 运行 `Get-FileHash -Algorithm SHA256 -LiteralPath '.\TGCopyTrader-Windows-x64.zip'`，核对对应的摘要。
2. 右键 ZIP，选择「全部解压」。打开解压出的 `TGCopyTrader` 文件夹，再双击 `TGCopyTrader.exe`。目录内已带齐运行组件。
3. 按 [使用指南](user-guide.md) 连接已有账户或创建新账户，再通过「账户概览」核对 UID 和余额。

此包没有独立发布者签名，可能出现 Microsoft Defender SmartScreen 提示。先核对下载来源和 SHA-256。受管理的电脑可能需要管理员批准。 [Microsoft Defender SmartScreen 说明](https://learn.microsoft.com/zh-cn/windows/security/operating-system-security/virus-and-threat-protection/microsoft-defender-smartscreen/)

业务数据和加密凭证保存在当前 Windows 用户的 `%APPDATA%\TGCopyTrader` 下。凭证受当前 Windows 用户的 DPAPI 保护。升级时先退出应用，再将新版 ZIP 完整解压到新目录运行。迁移到其他电脑时，通过账户选择器重新连接。

## 从源码构建 Windows x64 ZIP

当前构建脚本在 macOS 或 Linux 上运行，输出可在 Windows 启动的 ZIP。构建主机需要 Python 3.10 或更新版本及 pip、Node.js、`curl`、`rsync`、`shasum`、`zip`、`unzip`。依赖默认从 PyPI 安装，Windows 运行时从 Electron 和 Python 官方渠道下载并校验 SHA-256。

在仓库根目录执行：

```sh
./tools/build_windows.sh
```

也可传入缓存目录，脚本会使用其中已存在的官方运行时并验证摘要：

```sh
./tools/build_windows.sh /path/to/runtime-cache
```

缓存使用 `electron-v45.4.1-win32-x64.zip` 和 `python-3.13.17-embed-amd64.zip` 两个文件名。可用 `PYTHON` 指定构建解释器，或用 `TG_WHEELHOUSE` 指定包含 `requirements.txt` 所列依赖的离线轮文件目录。Windows 包使用纯 Python 依赖，内含 `tzdata` 以支持业务时区。

发布前用 Windows x64 真机解压，检查 EXE 启动、账户界面、凭证跨重启读取和 API 只读页面。Windows 真机验收仍待完成。
