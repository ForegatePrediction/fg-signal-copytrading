# 源码提交和 GitHub 发布

## 本地检查

从仓库根目录运行，建议使用 Python 3.12，并准备支持 `node --test` 的 Node.js。`requirements-macos.txt` 同时包含旧版网页流程的回归依赖。测试使用临时数据与离线夹具。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-macos.txt
PYTHONPATH=src python -X utf8 -m unittest discover -s tests
node --test tests/test_account_dom.cjs
```

在 Windows PowerShell 中验证源码时，可用 `$env:PYTHONPATH = 'src'`，然后执行 `python -X utf8 -m unittest discover -s tests` 和相同的 Node 测试命令。

## 首次上传

本次发布版本为 `0.8.2`，标签为 `v0.8.2`。本地 `main` 分支包含客户端源码、示例、测试、构建与安装脚本和文档。可用 `git ls-files` 查看提交清单。安装包位于被 Git 忽略的 `dist/github/`，用于创建 Release。

1. 在 GitHub 创建空的公开仓库，初始化选项留空。项目所有者可按希望授予的使用和分发权利选择源码许可证。
2. 在本机项目根目录运行以下命令，将 `OWNER/REPO` 替换为新仓库的所有者和名称。

   ```sh
   git remote add origin https://github.com/OWNER/REPO.git
   git push -u origin main
   git push origin v0.8.2
   ```

3. 打开 GitHub 的 Releases，选择已推送的 `v0.8.2`，将 `dist/github/` 中的两个安装包和 `SHA256SUMS.txt` 按原名上传，发布为 Latest。用户即可从 README 的「安装已发布版本」入口安装。GitHub 会从同一标签自动生成源码下载包。

## 构建与发布两个平台

1. 核对 macOS 清单 `src/macos/Info.plist`、Windows 清单 `src/windows/package.json`、两端界面版本和文档。以同一个源码提交构建 [macOS DMG](macos.md#从源码构建) 和 [Windows ZIP](windows.md#从源码构建-windows-x64-zip)。
2. 按各平台说明验收。至少确认安装或解压后的启动、账户选择、本机存储和 API 只读页面。将实盘交易验收与打包验收分别记在发布说明中。
3. 在仓库根目录执行以下命令，生成供安装脚本下载的三个文件，并校验 SHA-256。

   ```sh
   ./tools/prepare_release.sh
   ```

4. 在 GitHub 为构建所用的源码提交建立版本标签（例如 `v0.8.2`），创建正式 Release，设为 Latest。将 `dist/github/` 中的 `TGCopyTrader-macOS-arm64.dmg`、`TGCopyTrader-Windows-x64.zip` 和 `SHA256SUMS.txt` 按原名作为资产上传。源码标签应包含对应的安装与构建脚本。发布后分别在两端用 README 的命令验收下载和安装。 [GitHub Releases 说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
