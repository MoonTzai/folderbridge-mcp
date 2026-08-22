# FolderBridge MCP

简体中文 | [English](README.md)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

**在 AI 客户端与一个由你明确选择的本地文件夹之间，建立更安全的本地优先桥梁。**

FolderBridge MCP 是一个零第三方依赖的 Python MCP 服务器和桌面启动器。它让 ChatGPT 网页端或其他支持本地 stdio MCP 的客户端，在明确边界内查看并谨慎修改本地工作区。项目主动舍弃了公网 HTTP 服务器、任意 Shell、遥测以及静默常驻服务。

> [!IMPORTANT]
> 项目目前处于早期公开测试阶段。它可以缩小攻击面，但不是操作系统级沙箱。只应开放你信任的文件夹和代码仓库。

## 为什么选择 FolderBridge？

- **单文件夹边界：** 每个服务器进程只允许访问一个规范化工作区。
- **默认只读：** 必须在启动器中明确切换后才开启写入。
- **防冲突编辑：** 修改已有文件时必须携带最近一次读取返回的 SHA-256；文件已变化就拒绝覆盖。
- **没有任意 Shell：** 可选任务必须按名称定义、在本机人工检查，并以配置文件精确哈希批准。
- **不监听公网端口：** MCP 服务器只使用 stdio。
- **没有遥测：** FolderBridge 自身不发起网络请求。
- **密钥隔离：** OpenAI Runtime API Key 仅驻留启动器内存，并在启动本地 MCP 进程前清除。
- **傻瓜式桌面界面：** 文件夹、权限、Tunnel 配置、诊断、启停、进程状态和脱敏日志集中在一个窗口。
- **适配 Windows 缩放：** 字体和窗口自动跟随当前显示器 DPI，跨不同 Scale 的显示器移动时自动刷新。

## 快速开始

运行要求：

- Python 3.11 或更高版本；
- Windows 可获得已经测试过的双击启动体验，stdio 服务器本身可跨平台运行；
- Git 为可选依赖，只用于有界的 `status` 和 `diff` 查看。

### Windows EXE（推荐）

从 [GitHub 最新版本](https://github.com/MoonTzai/folderbridge-mcp/releases/latest)下载 `FolderBridge.exe` 和 `FolderBridge.exe.sha256`。无需安装 Python；校验哈希后直接双击 `FolderBridge.exe`。

EXE 包含 FolderBridge 及其 Python 运行时，但**不会**捆绑 OpenAI 的 `tunnel-client`。使用 ChatGPT 网页端时，仍需从 OpenAI 官方 Release 单独下载并在启动器中选择。

> [!NOTE]
> 当前社区构建尚未进行代码签名，因此 Windows 会显示发布者未知。如果你不信任未签名二进制文件，请按照下方步骤从已审核源码自行构建。

### 从源码运行

克隆并启动图形界面：

```powershell
git clone https://github.com/MoonTzai/folderbridge-mcp.git
cd folderbridge-mcp
python .\folderbridge_launcher.py gui
```

选择工作区，首次使用建议保持“只读”，然后按照状态面板完成设置。启动器偏好保存在仓库之外，Runtime API Key 永远不会落盘。

`folderbridge_gui.pyw` 仅作为源码环境的便利入口保留，要求 Windows 的 `.pyw` 文件关联指向带 Tkinter 的 Python。普通用户应双击独立的 `FolderBridge.exe`。

## 连接 ChatGPT 网页端

ChatGPT 不能直接连接本地 stdio 进程。FolderBridge 通过 OpenAI 官方 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 完成这条链路。你需要：

- 从 [OpenAI Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)取得 `tunnel_id`；
- 从同一组织的 [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys)创建供 `tunnel-client` 使用的 Key；
- 官方 [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest)；
- 与账号或工作区相匹配的 ChatGPT 开发者模式权限。

点击启动器右上角“网页端一键引导”，可以直接查看以下四步和快捷按钮。

### 1. 下载官方 Windows x64 客户端

1. 打开 [`openai/tunnel-client` 最新 Release](https://github.com/openai/tunnel-client/releases/latest)，展开 **Assets**。
2. 下载名为 `tunnel-client-v<版本>-windows-amd64.zip` 的完整包。Release 中的 `amd64` 就是 Windows x64，适用于绝大多数 Intel/AMD 电脑。
3. 可同时下载 `SHA256SUMS.txt` 校验文件。不要选择 `tunnel-client-runtime-*`、`windows-arm64`、`all.zip`、Source code 或许可证文件。
4. 将 ZIP 全部解压到固定目录；无需安装，也不要直接在压缩包内运行。向导可创建并打开推荐目录 `%LOCALAPPDATA%\FolderBridge\bin`。
5. 回到 FolderBridge，点击“选择已解压的 EXE”，选择 `tunnel-client.exe`。

### 2. 创建 Platform Tunnel

在 [OpenAI Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)点击 **Create tunnel**，当前表单按下表填写：

| 选项 | 建议值 | 说明 |
| --- | --- | --- |
| Name | `FolderBridge` | 可自定义，只用于识别 |
| Description | `Local FolderBridge MCP for private workspace access` | 必填，可自定义 |
| Organizations | 个人账号选 `Personal` | 团队账号选择实际管理此 Tunnel 的 Platform organization |
| ChatGPT workspaces | 选择将创建 App 的目标工作区 | 不要留空；个人账号通常选择列表中唯一的 workspace ID |

创建后复制 `tunnel_` 开头的 Tunnel ID。运行和选择 Tunnel 至少需要 **Tunnels Read + Use**；创建或修改还需要 **Manage**。只关联确实需要访问这个本地工作区的组织和 ChatGPT workspace。

另需在同一 Platform organization 的 [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys)创建供 `tunnel-client` 使用的 Key；创建者需要 **Tunnels Read + Use**。它只粘贴到 FolderBridge 主界面，保留在当前进程内存；**不要**使用 Admin API Key，也不要把 Runtime Key 填进 ChatGPT App 的 Authentication。

### 3. 启动 FolderBridge

1. 文件夹只选择一个明确的工作区，首次使用保持“只读（推荐）”。
2. `tunnel-client` 选择上一步的 EXE；Profile 保持 `folderbridge` 即可。
3. 填写相同的 Tunnel ID 和 Runtime API Key，“高级：允许任务”保持关闭。
4. 点击“启动连接”；启动器会执行官方 `init`、`doctor`、`run` 流程。顶部变成“运行中”后再进行下一步。

### 4. 创建 ChatGPT 开发态 App

打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，点击 **+** 创建 App，关键选项如下：

| 选项 | 必须选择 |
| --- | --- |
| Connection / 连接方式 | **Tunnel / 隧道** |
| Available Tunnel | 选择相同 Tunnel，或粘贴 `tunnel_...` ID |
| Authentication / 身份验证 | **No authentication / 无身份验证** |

> [!WARNING]
> 不要保留表单默认的 **OAuth**，也不要把 `https://tunnel-service...` 地址填入 Server URL。FolderBridge 不实现用户 OAuth；这样配置会出现 `does not implement OAuth` 错误。

ChatGPT 使用工作区期间需要保持启动器运行；关闭启动器会停止 Tunnel。开发者模式和 App 安装属于账号级安全操作，最终确认仍在 ChatGPT 网页中完成；FolderBridge 不会接管浏览器登录态或修改安全设置。

不需要安装浏览器扩展。FolderBridge 也不会静默下载或执行来自网络的二进制文件。

## 连接其他本地 MCP 客户端

输出可直接使用的配置：

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format toml
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format json
```

生成只读命令：

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format tunnel --read-only
```

## 工具与编辑流程

默认服务器提供：

- `server_info`：报告当前工作区和安全边界；
- `workspace`：列出、读取、搜索文件，并查看有界的 Git status/diff；
- `edit_file`：创建 UTF-8 文件，或执行原子化、唯一精确替换。

典型安全编辑循环：

1. 列出或搜索文件；
2. 读取文件并保留返回的 SHA-256；
3. 携带该哈希提交精确替换；
4. 检查 Git diff。

MCP 不能删除或移动文件。绝对路径、`..`、符号链接、junction/reparse point、版本控制内部文件、常见依赖/构建目录和疑似凭据文件名都会被拒绝。

## 可选命名任务

MCP 永远不能传入任意命令文本。如果需要“改完运行测试”，先创建并检查本地任务策略：

```powershell
python .\folderbridge_launcher.py init --workspace C:\path\to\repo
# 人工检查 C:\path\to\repo\.folderbridge.json
python .\folderbridge_launcher.py approve --workspace C:\path\to\repo
```

随后在启动器中启用已批准任务，或在生成的服务器命令中加入 `--allow-tasks`。模型只能选择任务名称，不能提供命令、参数、环境变量或工作目录。

> [!WARNING]
> 测试、构建工具和包脚本会以当前操作系统用户权限执行仓库代码。来源不可信的仓库应放入一次性虚拟机或容器，或者保持任务执行关闭。

## 安全模型

FolderBridge 把 MCP 请求、仓库文本和工具输出都视为不可信数据，真正的强制控制位于工具实现中，而不是仅依赖 MCP annotations。主要控制包括：

- 规范路径限制与链接拒绝；
- 对 UTF-8 读取、搜索、Git 输出、子进程输出和协议消息设置上限；
- 带当前内容哈希前置条件的原子写入；
- 保护本地策略文件；
- 使用 `shell=False`、固定参数数组、干净的任务环境和超时；
- 不监听入站网络，不包含 FolderBridge 遥测。

详细内容见[安全模型](docs/security-model.md)和[上游设计研究](docs/upstream-research.md)。报告安全漏洞请阅读 [SECURITY.md](SECURITY.md)。

## 开发

运行时没有第三方依赖：

```powershell
python -m unittest discover -s tests -v
```

提交 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 构建 Windows EXE

构建使用固定版本、仅构建时需要的 PyInstaller。发布 EXE 使用 console bootloader 和 `hide-console=hide-early`：双击时只显示图形界面，Tunnel 调用同一 EXE 的 `serve` 子命令时则保留 stdio。

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\scripts\build_windows.ps1 -Python .\.build-venv\Scripts\python.exe
```

构建结果和 SHA-256 文件位于 `release\windows-x64`。

## 许可证

项目采用 [Apache License 2.0](LICENSE)。

FolderBridge MCP 是独立开源项目，与 OpenAI 不存在从属、背书或赞助关系。ChatGPT、OpenAI、MCP 及其他产品名称归各自权利人所有。
