# Blender MCP Windows 部署包构建与重新安装

本文面向维护 `E:\Work\blender-mcp` 仓库的开发者，说明如何从当前源码重新构建 wheel、更新本机部署目录、生成可分发 ZIP，以及如何强制重新安装 Blender Add-on。

所有 PowerShell 命令兼容 **Windows PowerShell 5.1**。不要求 PowerShell 7。

## 1. 目录和文件职责

| 路径 | 用途 |
|---|---|
| `pyproject.toml` | Python 包版本和构建配置的权威来源 |
| `addon.py` | 仓库根目录的 Add-on 源文件 |
| `src/blender_mcp/bundled/addon.py` | 随 wheel 一起分发的 Add-on 副本 |
| `src/blender_mcp/bundled/asset_prepare_worker.py` | 随 wheel 一起分发的 Worker |
| `deployment/windows/` | 受 Git 管理的 Windows 分发文件 |
| `deployment/windows/blender-mcp/` | 本机解压/运行目录，可能包含个人 `blender-mcp.local.psd1`；该目录被 Git 忽略 |
| `dist/` | Python 构建产物目录 |

`deployment/windows/build-release.ps1` 是仓库维护脚本，不会放进接收者使用的 ZIP。

## 2. 一键重新构建 wheel 和分发 ZIP

先确认已安装 `uv`，并且当前 PowerShell 能找到它：

```powershell
Get-Command uv
uv --version
```

在仓库根目录执行：

```powershell
Set-Location E:\Work\blender-mcp
.\deployment\windows\build-release.ps1
```

脚本会自动定位仓库根目录；即使从其他工作目录调用，也会以脚本自身路径为准。

### 构建脚本执行内容

1. 从 `pyproject.toml` 读取项目版本，自动确定 wheel 文件名，例如当前版本 `1.9.1` 对应 `blender_mcp-1.9.1-py3-none-any.whl`。
2. 比较根目录 `addon.py` 与 `src/blender_mcp/bundled/addon.py` 的 SHA256。两者不一致时立即停止，不会把不确定的 Add-on 版本打进 wheel。
3. 在系统临时目录中执行 `uv build --wheel`，不清空或覆盖其他 `dist` 文件。
4. 检查 wheel 内必须存在：
   - `blender_mcp/bundled/addon.py`
   - `blender_mcp/bundled/asset_prepare_worker.py`
5. 分别比较 wheel 内这两个文件与源码文件的 SHA256。
6. 将 wheel 更新到 `dist/` 和 `deployment/windows/`。
7. 如果本机运行目录 `deployment/windows/blender-mcp/` 存在，则同步 wheel、初始化/安装/启动脚本、使用手册和配置模板；**保留其中已有的 `blender-mcp.local.psd1`，不会覆盖本机 Tunnel ID**。
8. 从明确的文件白名单构建 `blender-mcp.zip`，而不是把本机运行目录整体压缩。
9. 检查 ZIP 文件列表、配置模板占位符，并拒绝包含本机配置文件的 ZIP。

成功时输出：

```text
WINDOWS_RELEASE_BUILD_SUCCESS
Version:       ...
Wheel:         ...
Wheel SHA256:  ...
ZIP:           ...
ZIP SHA256:    ...
Addon SHA256:  ...
Worker SHA256: ...
```

只在出现 `WINDOWS_RELEASE_BUILD_SUCCESS` 且脚本正常退出后，才应将 ZIP 作为正式分发包。

## 3. 重新安装或更新本机 Blender Add-on

完成构建后，在仓库根目录执行：

```powershell
.\deployment\windows\install-addon.ps1
```

安装器会自动选择 `deployment/windows/` 中唯一的 `blender_mcp-*-py3-none-any.whl`。

也可以显式指定 wheel：

```powershell
.\deployment\windows\install-addon.ps1 -WheelPath .\deployment\windows\blender_mcp-1.9.1-py3-none-any.whl
```

安装器调用的核心命令为：

```powershell
uvx --reinstall-package blender-mcp --from <wheel路径> blender-mcp install-addon
```

`--reinstall-package blender-mcp` 用于强制刷新 uv 环境中的本地包，避免只因版本号和 wheel 文件名没变，就继续使用旧的缓存安装。

### 已安装过 Add-on 是否需要先卸载？

通常不需要先卸载。现有安装逻辑会更新识别到的标准单文件 `.py` Add-on；内容发生变化时会尝试备份旧文件。

注意：

- 手工修改过 Add-on 时，先独立备份。
- `.py.bak` 只是安装器的辅助备份，不应替代独立备份。
- 安装器不保证同时更新所有 Blender 版本的插件副本。
- Blender 4.2 及以上通过 Extensions 安装的目录型扩展，不保证能由传统 `.py` 安装流程原位更新。
- 安装完成后，重启 Blender；或者在 Preferences 中禁用再启用 `Interface: MCP for Blender`，使 Blender 重新加载代码。

## 4. 更新本机 tunnel-client profile

本机运行目录是：

```text
E:\Work\blender-mcp\deployment\windows\blender-mcp
```

构建脚本会在该目录存在时同步 wheel 和部署脚本，但不会覆盖个人配置文件。

### wheel 版本号未变化

新版 `init.ps1` 生成的 MCP 命令包含：

```text
uvx --reinstall-package blender-mcp --from <wheel文件名> blender-mcp
```

因此即使仍然是 `1.9.1`，只要本地 wheel 内容更新，下次由 tunnel-client 启动 MCP stdio 进程时也会强制刷新 `blender-mcp` 包。

### wheel 版本号发生变化

如果 `pyproject.toml` 的版本号变化，wheel 文件名也会变化。需要重新初始化本机 profile，让 profile 引用新的 wheel 文件名：

```powershell
Set-Location E:\Work\blender-mcp\deployment\windows\blender-mcp
.\init.ps1 -SkipAddonInstall
```

`-SkipAddonInstall` 只跳过 Add-on 安装，不跳过 profile 初始化。已有 `blender-mcp.local.psd1` 会继续使用，不需要重新填写 Tunnel ID。

初始化使用 `--force`，会重新生成同名 `blender-mcp` profile。如果该 profile 已被其他服务使用，应先备份并确认允许替换。

检查并启动：

```powershell
.\start.ps1 -Doctor
.\start.ps1
```

`OPENAI_API_KEY` 仍然从 Windows 环境变量读取。不要把 API Key 写入 `blender-mcp.local.psd1`、脚本、ZIP 或 profile 配置；profile 保存的是 `env:OPENAI_API_KEY` 引用。

## 5. 生成和检查分发包

正式分发文件：

```text
E:\Work\blender-mcp\deployment\windows\blender-mcp.zip
```

ZIP 内固定只包含以下内容：

- `blender-mcp/<当前版本 wheel>`
- `blender-mcp/init.ps1`
- `blender-mcp/install-addon.ps1`
- `blender-mcp/start.ps1`
- `blender-mcp/blender-mcp.local.example.psd1`
- `blender-mcp/使用手册.md`

明确不包含：

- `blender-mcp.local.psd1`
- 本机 Tunnel ID
- API Key
- 仓库源码、`.venv`、缓存和构建临时文件
- 开发者专用的 `build-release.ps1`

查看最终文件哈希：

```powershell
Get-FileHash .\deployment\windows\blender_mcp-1.9.1-py3-none-any.whl -Algorithm SHA256
Get-FileHash .\deployment\windows\blender-mcp.zip -Algorithm SHA256
```

查看 ZIP 清单：

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead(
    (Resolve-Path .\deployment\windows\blender-mcp.zip)
)
try {
    $zip.Entries | Select-Object -ExpandProperty FullName
}
finally {
    $zip.Dispose()
}
```

分发前确认清单中有 `blender-mcp.local.example.psd1`，没有 `blender-mcp.local.psd1`。

接收者解压 ZIP 后，按 ZIP 中的 `使用手册.md` 配置自己的 Tunnel ID 和 API Key。

## 6. 常见构建问题

### `uv` 找不到

安装 uv，确认 `Get-Command uv` 能找到命令，然后重新打开 PowerShell。

### 提示 `addon.py` 与 bundled 副本不一致

这是刻意设置的构建保护。先确认哪一份源码是本次预期版本，再有意识地同步文件；不要直接绕过校验继续发布。

如果确认根目录 `addon.py` 是本次权威版本，可以手工同步：

```powershell
Copy-Item .\addon.py .\src\blender_mcp\bundled\addon.py -Force
```

随后重新执行构建脚本。

### 提示 wheel 缺少 bundled 文件或 SHA256 不一致

检查 `pyproject.toml` 的 `[tool.setuptools.package-data]` 是否继续包含：

```toml
blender_mcp = ["bundled/addon.py", "bundled/asset_prepare_worker.py"]
```

修正后重新构建，不要手工修改 wheel 内部内容。

### 提示目录中存在多个 wheel

安装器和初始化脚本要求部署目录中只有一个 `blender_mcp-*-py3-none-any.whl`。重新运行 `build-release.ps1`，它会清除部署目录中的旧版本 wheel，只保留当前项目版本。

### 安装后 Blender 仍执行旧代码

关闭并重新打开 Blender，或禁用再启用 Add-on。若安装了多个 Blender 版本，确认当前运行的 Blender 使用的是安装器更新的插件目录。

---

## 7. 日常操作速查

| 目的 | 命令 |
|---|---|
| 重建 wheel 和 ZIP | `.\deployment\windows\build-release.ps1` |
| 强制重新安装本机 Add-on | `.\deployment\windows\install-addon.ps1` |
| 版本变化后更新本机 tunnel profile | 在 `deployment\windows\blender-mcp` 中执行 `.\init.ps1 -SkipAddonInstall` |
| 检查 tunnel | `.\start.ps1 -Doctor` |
| 启动 tunnel | `.\start.ps1` |
