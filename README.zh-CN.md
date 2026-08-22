# FolderBridge MCP

简体中文 | [English](README.md)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

> [!TIP]
> **Windows 用户：[直接下载 FolderBridge.exe](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe)**
> 安装包位于 GitHub **Releases → 最新版本 → Assets**，不会出现在仓库的源码文件列表中。也可打开[完整发布页面](https://github.com/MoonTzai/folderbridge-mcp/releases/latest)下载 EXE 和 SHA-256 校验文件。

**在 AI 客户端与一组由你明确选择的本地文件夹之间，建立更安全的本地优先桥梁。**

FolderBridge MCP 是一个零第三方依赖的 Python MCP 服务器和桌面启动器。它让 ChatGPT 网页端或其他支持本地 stdio MCP 的客户端，在明确边界内查看并谨慎修改本地工作区。项目主动舍弃了公网 HTTP 服务器、任意 Shell、遥测以及静默常驻服务。

> [!IMPORTANT]
> 项目目前处于早期公开测试阶段。它可以缩小攻击面，但不是操作系统级沙箱。只应开放你信任的文件夹和代码仓库。

## 0.4.1 重点更新

- **`--allow-tasks` 支持混合工作区：** 没有 `.folderbridge.json` 的工作区、已有批准 named task 的工作区、只使用 Extension 的工作区，以及 task config 尚未批准的工作区，可以同时存在于一个连接中。只有真正调用该工作区的 named task 时才检查批准状态。
- **Launcher 托管 ComfyUI：** Windows 启动器可以记住已经验证过的 ComfyUI Portable / `.venv` / `venv` 安装路径，并在 bundled Local ComfyUI Extension 加载时自动启动；如果 `127.0.0.1:8188` 已有服务在线，则识别为外部服务并直接复用，不会重复启动。
- **严格的进程 ownership：** FolderBridge 不持久化 ComfyUI PID，也不保存任意启动命令，更不会因为某个未知进程占用了 8188 就按端口终止它。只有当前 Launcher 本次运行亲自创建并保留在内存中的 `Popen` handle 才允许停止。
- **更安全的退出顺序：** 先停止 FolderBridge-owned managed services，再停止 Tunnel/MCP；外部服务保持运行。可能阻塞的进程等待放到后台线程，不冻结 Tk 主线程。
- **跨显示器 DPI 加固：** 保留 Per-Monitor V2，并加入轻量 DPI fallback poll；固定 UI metric 每次都从原始逻辑尺寸按 `dpi / 96` 重新计算，避免跨不同 Scale 显示器来回拖动时产生累计缩放漂移。

完整版本记录见 [CHANGELOG.md](CHANGELOG.md)；Managed Service 与插件边界见 [Extension ABI v1](docs/extensions.md)。

## 为什么选择 FolderBridge？

- **独立文件夹边界：** 一个连接最多添加 8 个规范化工作区；每次工具调用明确选择 `workspace_id`，目录之间不会合并。
- **默认只读：** 必须在启动器中明确切换后才开启写入。
- **防冲突编辑：** 修改已有文件时必须携带最近一次读取返回的 SHA-256；文件已变化就拒绝覆盖。
- **没有任意 Shell：** 可选任务必须按名称定义、在本机人工检查，并以配置文件精确哈希批准。
- **不监听公网端口：** MCP 服务器只使用 stdio。
- **没有遥测：** FolderBridge 自身不发起网络请求。
- **密钥隔离：** OpenAI Runtime API Key 仅驻留启动器内存，并在启动本地 MCP 进程前清除。
- **傻瓜式桌面界面：** 可添加/移除的文件夹列表、全局权限、Tunnel 配置、诊断、启停、进程状态和脱敏日志集中在一个窗口。
- **适配 Windows 缩放：** 字体和窗口自动跟随当前显示器 DPI，跨不同 Scale 的显示器移动时自动刷新。

## 快速开始

运行要求：

- Python 3.11 或更高版本；
- Windows 可获得已经测试过的双击启动体验，stdio 服务器本身可跨平台运行；
- Git 为可选依赖，只用于有界的 `status` 和 `diff` 查看。

### Windows EXE（推荐）

从 [GitHub 最新版本的 Assets](https://github.com/MoonTzai/folderbridge-mcp/releases/latest)下载 `FolderBridge.exe` 和 `FolderBridge.exe.sha256`，或[直接下载 EXE](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe)。安装包不会出现在仓库的源码文件列表中。无需安装 Python；校验哈希后直接双击 `FolderBridge.exe`。

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

逐个添加需要的工作区，首次使用建议保持“只读”，然后按照状态面板完成设置。启动器偏好保存在仓库之外，Runtime API Key 永远不会落盘。旧版的单工作区配置会自动迁移为列表中的第一项。

`folderbridge_gui.pyw` 仅作为源码环境的便利入口保留，要求 Windows 的 `.pyw` 文件关联指向带 Tkinter 的 Python。普通用户应双击独立的 `FolderBridge.exe`。

## 连接 ChatGPT 网页端

ChatGPT 不能直接连接本地 stdio 进程。FolderBridge 通过 OpenAI 官方 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 完成这条链路。你需要：

> [!IMPORTANT]
> Secure MCP Tunnel 只需要本机向 OpenAI 发起**出站 HTTPS（默认 443）**，不需要开放任何互联网入站端口。不要为 FolderBridge 开启 Windows 远程桌面、路由器端口转发、DMZ、UPnP 映射或 `3389`；这些都不属于 Tunnel 配置。如果防火墙询问是否允许入站访问，不要为了 Tunnel 创建面向公网的宽泛入站规则。

- 从 [OpenAI Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)取得 `tunnel_id`；
- 从同一组织的 [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys)创建供 `tunnel-client` 使用的 Key；
- 官方 [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest)；
- 与账号或工作区相匹配的 ChatGPT 开发者模式权限。

点击启动器右上角“网页端一键引导”，可以直接查看以下四步和快捷按钮。

### 1. 下载官方 Windows x64 客户端

> [!WARNING]
> **只下载完整包 `tunnel-client-v<版本>-windows-amd64.zip`。不要下载或选择任何 `tunnel-client-runtime-*` 文件。** Runtime 是内部组件，不支持配置所需的 `init` 命令；即使文件名看起来相似，也会导致“配置失败，请查看日志”。解压完整包后，只选择准确名为 `tunnel-client.exe` 的主程序。

1. 打开 [`openai/tunnel-client` 最新 Release](https://github.com/openai/tunnel-client/releases/latest)，展开 **Assets**。
2. 下载名为 `tunnel-client-v<版本>-windows-amd64.zip` 的完整包。Release 中的 `amd64` 就是 Windows x64，适用于绝大多数 Intel/AMD 电脑。
3. 可同时下载 `SHA256SUMS.txt` 校验文件。不要选择 `tunnel-client-runtime-*`、`windows-arm64`、`all.zip`、Source code 或许可证文件；Runtime 包不能代替完整客户端。
4. 将 ZIP 全部解压到固定目录；无需安装，也不要直接在压缩包内运行。向导可创建并打开推荐目录 `%LOCALAPPDATA%\FolderBridge\bin`。
5. 回到 FolderBridge，点击“选择已解压的 EXE”，只选择准确名为 `tunnel-client.exe` 的文件。

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

1. 在文件夹列表中逐个添加明确的工作区（最多 8 个），首次使用保持“只读（推荐）”；全局权限作用于列表内全部目录。
2. `tunnel-client` 只选择完整包中的 `tunnel-client.exe`，不要选择 `tunnel-client-runtime-*`；Profile 保持 `folderbridge` 即可。
3. 填写相同的 Tunnel ID 和 Runtime API Key；可按需一次勾选要全局预授权的常用能力，逐工作区“高级：自定义任务”没有特殊需求时保持关闭。
4. 点击“启动连接”；启动器会执行官方 `init`、`doctor`、`run` 流程。顶部变成“运行中”后再进行下一步。以后更换文件夹或权限时，可直接再次点击“应用配置”；启动器会更新自己管理的同名 Profile。

### 4. 创建 ChatGPT 开发态 App

打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，点击 **+** 创建 App，关键选项如下：

| 选项 | 必须选择 |
| --- | --- |
| Connection / 连接方式 | **Tunnel / 隧道** |
| Available Tunnel | 选择相同 Tunnel，或粘贴 `tunnel_...` ID |
| Authentication / 身份验证 | **No authentication / 无身份验证** |

> [!WARNING]
> 不要保留表单默认的 **OAuth**，也不要把 `https://tunnel-service...` 地址填入 Server URL。FolderBridge 不实现用户 OAuth；这样配置会出现 `does not implement OAuth` 错误。

#### 在 Chat 界面调用

1. 保持 FolderBridge 顶部为“运行中”，然后在 ChatGPT 新建一个对话。
2. 点击输入框旁的 **+**，进入“更多 / More”，选择刚创建的 FolderBridge App。
3. 发送任务请求，例如：“请先使用 FolderBridge 的 `server_info` 列出可用工作区，再读取我指定的工作区根目录。”如果 ChatGPT 显示工具确认，核对 `workspace_id` 和目标文件后再确认。

这个入口与测试流程以 OpenAI 的[连接与测试官方说明](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt)为准。启动器内的“连接设置向导”也提供相同步骤和一键复制调用示例。

ChatGPT 使用工作区期间需要保持启动器运行；关闭启动器会停止 Tunnel。开发者模式和 App 安装属于账号级安全操作，最终确认仍在 ChatGPT 网页中完成；FolderBridge 不会接管浏览器登录态或修改安全设置。

不需要安装浏览器扩展。FolderBridge 也不会静默下载或执行来自网络的二进制文件。

## 连接其他本地 MCP 客户端

有。FolderBridge 的预留兼容接口就是标准 MCP `stdio` 服务器；ChatGPT Tunnel 只是网页端不能直接启动本地进程时使用的桥。MCP 官方 `stdio` 传输同样规定由客户端启动服务器子进程，并通过标准输入/输出交换 JSON-RPC 消息。[MCP stdio 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)

### 客户端需要满足的条件

- 能配置本地 **command + args**，并启动一个长期运行的子进程；
- 使用 MCP `stdio`，保持 stdout 只传协议消息，并读取服务器的工具列表和工具调用结果；
- 支持 MCP 工具发现/调用（`initialize`、`tools/list`、`tools/call`，或 2026-07-28 的现代发现流程）；
- 在会话结束时关闭 stdin 或终止子进程；
- 最好具有工具授权/确认界面，但不能把客户端提示框当成唯一安全边界。

FolderBridge 当前兼容 `2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25` 和 `2026-07-28` 协议路线。它提供标准工具 schema、文本结果与 `structuredContent`；是否展示确认弹窗由具体客户端决定。[MCP Tools 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

| 客户端类型 | 能否直接接入 | 接入方式 |
| --- | --- | --- |
| 本地桌面应用或 IDE，支持启动 stdio MCP | 可以 | 配置 `FolderBridge.exe` 为 command，`serve ...` 为 args |
| 本地应用只提供一个“命令”输入框 | 通常可以 | 复制完整 stdio 命令；注意路径引号 |
| 只接受远程 HTTP/SSE URL | 不能直接接入 | 使用该客户端官方的 stdio 网关/代理，或另行部署受保护的 MCP 桥 |
| 纯网页、移动端或云端沙箱 | 通常不能直接接入本机 | 使用厂商官方 Tunnel/远程连接机制，并让桥在能访问本机工作区的受信主机上运行 |

### 最简单的本地接入方式

1. 在 FolderBridge 主界面添加一个或多个工作区，并保持“只读（推荐）”。
2. 打开“连接设置向导”第 5 页，根据客户端复制 JSON、TOML 或完整 stdio 命令。
3. 将配置粘贴到客户端的 MCP Servers 设置；字段名以该客户端自己的文档为准。
4. 重启或刷新客户端，确认能看到 `server_info` 和 `workspace` 工具。

直接 stdio 接入时不需要 `tunnel-client`、Tunnel ID、Runtime API Key，也不需要保持 FolderBridge GUI 打开。客户端会在需要时自动运行：

```powershell
FolderBridge.exe serve --workspace C:\path\to\repo --read-only
```

添加多个目录时重复 `--workspace`，所有目录仍保持独立根边界：

```powershell
FolderBridge.exe serve --workspace C:\work\frontend --workspace C:\work\backend --read-only
```

从源码生成可直接粘贴的配置：

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format json --read-only
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format toml --read-only
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format tunnel --read-only
```

JSON 示例采用许多桌面客户端使用的 `mcpServers` 约定：

```json
{
  "mcpServers": {
    "folderbridge": {
      "command": "C:\\Tools\\FolderBridge.exe",
      "args": ["serve", "--workspace", "C:\\work\\frontend", "--workspace", "C:\\work\\backend", "--read-only"]
    }
  }
}
```

> [!IMPORTANT]
> JSON/TOML 顶层键只是常见约定，不是所有客户端都相同。真正的兼容条件是客户端能把 command/args 原样作为 MCP stdio 子进程启动。若客户端只接受 URL，当前版本没有内置 HTTP/SSE 监听器，不要把本机端口直接暴露到公网。

更完整的协议版本、进程生命周期、`cwd`/`env`、审批边界和远程桥接说明见[客户端兼容性研究](docs/client-compatibility-research.md)。

## 工具与编辑流程

这些内建工具不依赖每个工作区自己的 task 配置：

- `server_info`：报告可用工作区的名称、稳定 `workspace_id`、内建/全局能力和安全边界；
- `workspace`：在指定 `workspace_id` 内列出、读取、搜索文件，并查看有界的 Git status/diff；
- `file_info`：读取二进制文件的有界元数据与 SHA-256；
- `pptx_inspect`：安全解析 PPTX 文本、OOXML 图关系和 SmartArt 数据，不执行 Office 内容；
- `image_open`：把工作区中的 PNG/JPEG/GIF/WebP（包括 ZIP 内精确成员）作为 MCP 图像内容返回；
- `extension`：固定的 `list` / `info` / `run` 插件网关；以后安装更多插件不会继续增加 MCP tool 名称；
- `edit_file`：读写模式下，在指定工作区创建 UTF-8 文件，或执行原子化、唯一精确替换。

只有一个工作区时，旧客户端可以继续省略 `workspace_id`。存在多个工作区时，所有工作区作用域工具都必须携带 `server_info` 返回的 `workspace_id`；缺失或未知 ID 会被拒绝。重复目录、父子重叠目录和超过 8 项的列表也会在启动前被拒绝。

典型安全编辑循环：

1. 列出或搜索文件；
2. 读取文件并保留返回的 SHA-256；
3. 携带该哈希提交精确替换；
4. 检查 Git diff。

MCP 不能删除或移动文件。绝对路径、`..`、符号链接、junction/reparse point、版本控制内部文件、常见依赖/构建目录和疑似凭据文件名都会被拒绝。

## 全局预授权能力

启动器可以把常见能力一次授权给当前和未来所有工作区：`test`、`build`、`package-windows`、`package-android` 和 `git-push`。这些权限保存在启动器设置中，不写进每个工作区的 `.folderbridge.json`。因此某个工作区加入 FolderBridge 时即使还没有 EXE/APK 构建脚本，几个月后新增受支持的入口，也会在调用时自动发现，无需重新添加工作区或手改 JSON。主界面同时提供“全选”和“清空”按钮。

`git-push` 被限制为 GitHub HTTPS `origin`、当前分支、禁止 force push，并拒绝仓库本地的 credential helper、pushurl/receivepack 和 URL 重写配置。构建/封装能力可能执行本地项目代码，所以只勾选你愿意全局预授权的能力。

直接 stdio 客户端可在 `serve` 或 `client-config` 命令中重复加入 `--capability <名称>`；Windows 启动器提供同样的持久化复选框。

## Extensions 插件

FolderBridge Extension ABI v1 用于以后增加 ComfyUI、FFmpeg、Blender、Ollama、ADB 等全局集成，而不继续修改 MCP 工具目录。Windows 主界面右侧有默认折叠的 **Extensions** 侧栏：可以热扫描用户插件目录，显示已安装/已批准/已加载状态，并且在 Tunnel 保持连接时直接批准、加载或停用插件。

每个插件至少包含 `folderbridge-extension.json` 和 `plugin.py`。外部插件的批准绑定“完整插件目录 SHA-256 + permissions”；任一文件或权限变化都会让旧批准自动失效。插件在独立子进程中运行，使用清理环境、固定超时和有界协议 I/O。这能隔离崩溃与协议污染，但**不是完整的操作系统沙箱**；不可信插件仍应放进 VM/容器。

需要适配具体项目时，应使用 `workspace_adapter.mode=dynamic` 与 `detect.any_of` / `detect.all_of`。FolderBridge 每次调用都会重新检测，因此插件安装时不需要向每个工作区注入 `.folderbridge.json` task，项目以后才出现相关脚本也能自动变为可用。插件持久状态优先使用 FolderBridge 提供的 profile state 目录，不要污染仓库。

ComfyUI 是第一个 bundled extension。状态检查固定访问 `127.0.0.1:8188`；执行 workflow 需要在 Extensions 侧栏一次批准。从 FolderBridge 0.4.1 开始，Windows Launcher 还可以自己托管本地 ComfyUI 进程：只需选择一次受支持的 Portable 根目录（`python_embeded\\python.exe` + `ComfyUI\\main.py`），或源码安装根目录（`main.py` + `.venv\\Scripts\\python.exe` / `venv\\Scripts\\python.exe`），之后可用明确的 Python/main.py 参数、`shell=False` 和固定 `127.0.0.1:8188` 自动启动。如果 8188 在 FolderBridge 启动前已经在线，则标记为外部服务并直接复用，不会再启动第二份。

Managed Service 的 ownership 比 Extension 权限更严格：持久配置只保存 `install_root` 和 `auto_start`，不保存 PID 或任意启动命令；只有当前 Launcher 本次运行亲自创建并保留在内存中的 `Popen` handle 才算 owned、才允许停止。FolderBridge 不会执行用户选择的 BAT/CMD，也不会因为未知程序占用了 8188 就按端口查 PID 后终止。退出时先停止 owned managed services，再停止 Tunnel/MCP；外部 ComfyUI 保持运行。

Windows 下 Launcher 请求 Per-Monitor DPI V2，并用轻量 DPI 轮询作为 fallback。固定 GUI metric 只在 DPI 真变化时，从原始逻辑尺寸按 `dpi / 96` 重新计算，因此 96 → 144 → 96 不会产生累计缩放漂移。

ABI v1 的 `plugin.py` 应只依赖 FolderBridge 已打包模块与 Python 标准库，不假设单文件 EXE 可以任意 pip 安装第三方包。额外软件优先通过精确 loopback API 或声明 `process.execute:<程序名>` 调用。可运行 `FolderBridge.exe extensions --json` 检查 EXE 实际发现的插件。连接设置向导的“附录 插件标准”提供完整格式，以及一键复制的 **LLM 插件开发指令**；该指令要求 LLM 在资料不足时主动要求用户上传/提供 API 文档、脚本、workflow、样例文件或项目结构，而不是自行猜测。

详细规范见 `docs/extensions.md`。

## 可选的逐工作区命名任务

MCP 永远不能传入任意命令文本。对于全局能力没有覆盖的特殊仓库命令，再创建并检查本地任务策略：

```powershell
python .\folderbridge_launcher.py init --workspace C:\path\to\repo
# 人工检查 C:\path\to\repo\.folderbridge.json
python .\folderbridge_launcher.py approve --workspace C:\path\to\repo
```

随后在启动器中启用已批准任务，或在生成的服务器命令中加入 `--allow-tasks`。打开这个开关**不要求所有工作区都存在 `.folderbridge.json`**：无 config 工作区、已有批准 task 的工作区、Extension-only 工作区，以及 config 尚未批准的工作区可以同时存在于一个连接中；只有真正调用该工作区的 named task 时才检查批准状态。模型只能选择任务名称，不能提供命令、参数、环境变量或工作目录。

> [!WARNING]
> 测试、构建工具和包脚本会以当前操作系统用户权限执行仓库代码。来源不可信的仓库应放入一次性虚拟机或容器，或者保持任务执行关闭。

## 安全模型

FolderBridge 把 MCP 请求、仓库文本和工具输出都视为不可信数据，真正的强制控制位于工具实现中，而不是仅依赖 MCP annotations。主要控制包括：

- 每个工作区分别执行规范路径限制与链接拒绝，并拒绝重叠根目录；
- 对 UTF-8 读取、搜索、Git 输出、子进程输出和协议消息设置上限；
- 带当前内容哈希前置条件的原子写入；
- 保护本地策略文件；
- 使用 `shell=False`、固定参数数组、干净的任务环境和超时；
- Extension manifest 拒绝未知/过宽权限，批准绑定完整插件代码 hash 与 permissions，插件代码移出 MCP 主进程执行；
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
