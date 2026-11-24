# 信号跟单 · ForeGate API 工作台

桌面客户端通过 ForeGate OpenAPI 管理账户、查看余额与持仓、查询订单和成交，并支持手动交易及 Telegram 自动跟单。macOS 与 Windows 使用同一套界面和 Python 业务逻辑，本地账户选择器支持连接已有 API 账户、创建新 API 账户和切换已保存账户。

## 把仓库链接交给 AI

复制下面任意一句，把「仓库链接」替换成当前 GitHub 仓库地址，交给能在你电脑上运行命令的 AI。

> 帮我安装「仓库链接」最新发布的 ForeGate 客户端。按 README 识别我的系统，下载并校验安装包，完成后告诉我从哪里打开。

> 请把「仓库链接」的源码打包成适合我系统的安装包。按 README 检查构建环境，运行对应脚本，并告诉我产物路径。

### 安装已发布版本

AI 从用户提供的 GitHub 地址取得实际的 `OWNER/REPO`。下载该仓库最新正式 Release 对应标签的源码并进入根目录，按本机系统运行以下一条命令。安装无需 ForeGate 凭证。

| 本机系统 | 在仓库根目录运行 | 安装位置 |
|---|---|---|
| Apple 芯片 Mac，macOS 13+ | `./tools/install_macos.sh OWNER/REPO` | 用户「应用程序」中的「信号跟单」 |
| Windows 10/11 x64（PowerShell） | `.\tools\install_windows.ps1 -Repository OWNER/REPO` | 开始菜单中的 TGCopyTrader |

脚本从指定仓库的最新正式 Release 下载对应安装包并核对 SHA-256。首次打开遇到系统确认时，按下方平台说明操作。启动后由用户在客户端连接自己的 API 账户。

### 从源码生成安装包

获取目标版本的完整源码，进入仓库根目录，按构建主机选择命令。构建使用仓库中的源文件和锁定依赖。

| 目标产物 | 构建主机及准备 | 命令 |
|---|---|---|
| macOS Apple 芯片 DMG | Apple 芯片 Mac，安装 Xcode Command Line Tools | `./tools/build_dmg.sh` |
| Windows x64 ZIP | macOS 或 Linux；Windows 用户可在已配置的 WSL Ubuntu 终端中执行。准备 Python 3.10+、pip、Node.js、curl、rsync、zip、unzip、shasum | `./tools/build_windows.sh` |

产物在 `dist/`。首次构建需要联网获取运行组件和依赖。选择源码构建的用户可以在首个 Release 发布前使用这两个入口。

## 安装与使用

| 平台 | 0.8.2 分发包 | 文档 |
|---|---|---|
| macOS 13+ · Apple 芯片（M 系列） | `TGCopyTrader-macOS-arm64.dmg` | [macOS 安装与构建](docs/macos.md) |
| Windows 10/11 · x64 | `TGCopyTrader-Windows-x64.zip` | [Windows 安装与构建](docs/windows.md) |

本地构建产物在 `dist/`。发布后从 GitHub Releases 下载对应系统的包。两个版本都附带运行组件。

首次打开后连接 ForeGate API 账户，先核对账户 UID、余额和订单。账户字段、市场交易和自动跟单的操作见 [使用指南](docs/user-guide.md)。平台接口见 [ForeGate OpenAPI 文档](https://docs.forepaas.org/)。Windows x64 包已通过结构校验，首次启动和凭证持久化仍待 Windows 真机验收。

## 源码结构

| 目录或文件 | 内容 |
|---|---|
| `src/copytrader/` | ForeGate API、Telegram 信号解析、交易与跟单、SQLite 账本 |
| `src/ui/` | 两端共用的 API 工作台，旧版页面集中在 `legacy/` |
| `src/macos/` | Swift、AppKit / WKWebView 宿主、钥匙串与应用清单 |
| `src/windows/` | Electron 宿主、Windows 凭证存储与子进程桥接 |
| `src/config/`、`src/fixtures/` | 示例配置和离线解析样本 |
| `tests/` | Python 测试、DOM 回归和测试夹具 |
| `tools/`、`requirements*.txt` | 两端打包脚本和锁定的 Python 依赖 |
| `docs/` | 安装、使用、构建和 GitHub 发布说明 |

macOS 宿主与 Windows 宿主均通过标准输入和输出与本地 Python 进程通信。发布流程见 [GitHub 发布说明](docs/release.md)。
