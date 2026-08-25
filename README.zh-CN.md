# FolderBridge MCP

简体中文 | [English](README.md)

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![源码 Python 3.11+](https://img.shields.io/badge/Source%20Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

> [!TIP]
> **FolderBridge Windows 版就是单文件应用：[直接下载 `FolderBridge.exe`](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe) 后双击即可；无需另外安装 Python 或 Node.js。**
> 文件位于 GitHub **Releases → 最新版本 → Assets**，不会出现在仓库的源码文件列表中。若连接 ChatGPT 网页版，仍需按向导另外选择 OpenAI 官方独立发布的 `tunnel-client.exe`。也可打开[完整发布页面](https://github.com/MoonTzai/folderbridge-mcp/releases/latest)下载 EXE 和可选的 SHA-256 校验文件。

**在 AI 客户端与一组由你明确选择的本地文件夹之间，建立更安全的本地优先桥梁。**

FolderBridge MCP 是一个零第三方依赖的 Python MCP 服务器和桌面启动器。它让 ChatGPT 网页端或其他支持本地 stdio MCP 的客户端，在明确边界内查看并谨慎修改本地工作区。项目主动舍弃了公网 HTTP 服务器、任意 Shell、遥测以及静默常驻服务。Windows Launcher 现在可通过“连接设置向导”左侧固定的 `中文 / EN` 按钮切换完整用户界面；语言选择会持久化，但不会改变 Tunnel 配置指纹，也不会触发重新连接。

> [!IMPORTANT]
> 项目目前处于早期公开测试阶段。它可以缩小攻击面，但不是操作系统级沙箱。只应开放你信任的文件夹和代码仓库。

## 0.7.0 重点更新

- **新增本地 Skill Engine，且不扩张 MCP tool 目录：** 可信的方法类 Skill 可按需发现、匹配和加载；统一通过 bundled `skill-engine` Extension 走现有稳定 `extension` 网关，新增 Skill Pack 不会新增 MCP tool 名称。
- **模型路由，不虚假宣称强制调用：** MCP 初始化指令只注入有界 routing index，在架构、调试、TDD、代码审查和实现任务中提示模型先 `match`、再 `get` 合适的方法；`server_info` 明确标记为 model-routed，而不是保证每次必调。
- **外部 Skill Pack 使用 exact-hash 信任：** 未批准或已 stale 的 Pack 对模型侧不可见；Launcher 批准时必须携带刚显示的精确 hash，Pack 任一文件变化都会使批准失效；`get` 还会重新核验最终返回正文的确切字节，避免 Skill 在 `match` 后悄悄变化。
- **内置工程方法 Pack：** `folderbridge-engineering` 提供代码设计、架构优化、故障诊断、测试驱动开发、代码审查和按方案实现六类方法。该 Pack 是 FolderBridge 针对自身 Skill Engine 做的精简／改编版本，来源于 Matt Pocock 的开源 [`mattpocock/skills`](https://github.com/mattpocock/skills) 中选定的工程 Skills，并按上游 MIT License 保留归属与许可说明；它不是 Matt Pocock 官方插件。Skill 只是方法文本，不会作为本地代码执行。
- **Launcher 统一为 Extensions & Skills：** 右侧栏分别管理 Extension 与 Skill Pack，可打开用户 Skill 目录、启停 Pack、核对外部 Pack hash、撤销批准和查看详情，并明确提示“不会执行本地代码，但会影响模型的方法选择和行为”。
- **打包链真实验证 Skill：** `skills --json` 在 Windows 管道中固定输出 UTF-8；`extensions --self-test` 会实际启动 ComfyUI 与 Skill Engine worker；Windows build 会把 `skill_packs` 打进单文件 EXE，并在生成 checksum 前验证 bundled Skill 与 worker 链路。
- **PPTX XML 实际占用可观测：** `pptx_inspect` 在保留 256 MiB XML/relationship 解析前上限的同时，正常结果新增实际未压缩字节数、限制值与占用比例，便于判断真实课件离上限还有多远。
- **统一用户状态目录：** Launcher、MCP host 与清理环境后的 Extension worker 现在共用一套 FolderBridge 用户配置根目录解析规则，避免 Extension/Skill 状态因环境变量视图不同而分裂。
- **Extension 改为校验后私有快照执行：** exact-hash 批准现在真正绑定到最终执行字节。worker 会先把 hash 覆盖的 Extension 文件复制到私有临时快照，对快照重新核验宿主钉住的 SHA-256，再只从该快照 import/运行；插件相对路径读取也统一落在快照中，不再回到可变原目录。
- **补齐并发与协议边界：** Extension trust store 的读改写在单进程内串行化；manifest、worker 请求和响应改为 strict JSON，拒绝非标准 `NaN`/`Infinity`；重叠密钥按长值优先脱敏；JSON Schema 中 boolean 不再因为 Python 的相等规则被当作数值 enum。
- **第一方外部进程统一纳入 owned-process 语义：** 核心 Git 检查、bundled Git Publisher 的 Git/GCM、Office PowerShell 渲染都具备进程树超时清理；Git 安全检查一旦输出被截断会直接失败，不把不完整结果当作完整检查。
- **补充资源与生命周期保护：** ComfyUI 启动流程持有稳定的本地 process handle，避免与 shutdown 竞态；核心 PPTX 检查在 XML 解析前增加 256 MiB 的 XML/relationship 总未压缩体积上限。
- **修复跨屏 DPI 字体缩放：** Windows Per-Monitor V2 环境下，FolderBridge 在窗口移动到不同缩放比例的显示器时，会根据新的 `GetDpiForWindow` 结果明确重算 named font 的像素尺寸。ttk 标签/按钮/输入框/Treeview、运行日志、主启动/停止按钮以及连接向导正文都会随当前屏幕重新缩放，不再依赖 Tk 对已有控件运行时 `tk scaling` 更新的未定义行为。
- **新增长任务 Extension Job：** action 可声明 `run_mode=job`，`extension(action="run")` 会立即返回 FolderBridge 托管的 `job_id`，随后继续用同一个 `extension` 网关查询状态或取消；不新增 MCP tool 名称。超时可配置到 24 小时，`0` 表示关闭自动超时终止。每个 FolderBridge 进程最多同时保有 16 个活跃 Job（包括 `termination_pending`），并只保留最近 128 条已结束 Job 记录；前台 Extension worker 另有独立的 16-worker 生命周期预算。
- **明确环境变量/密钥边界：** Extension 仍从清理后的环境启动，只有 manifest 精确声明的 `environment.inherit:变量名` 才会被复制进去；`CONTROL_PLANE_API_KEY` 与 FolderBridge/control-plane 内部变量永远禁止继承。通过 key/token/secret/password/auth 类变量名继承的值，会在插件返回结果、日志和对外错误中递归脱敏，避免把 API Key 带回模型侧。
- **新增外部 HTTPS 权限声明：** 需要调用公网 API 的插件可声明 `network.outbound:https`，让用户在精确 hash 批准时明确看到公网依赖。该权限与其它 Extension permission 一样属于授权契约，不是内核级网络沙箱。
- **宿主校验工作区产物：** 插件可返回 `workspace_artifacts`；FolderBridge 会重新按工作区路径策略解析，并附加 size/SHA-256 后才把结果暴露给模型。
- **统一的深层进程所有权模块：** Extension worker/Job、Tunnel 命令、FolderBridge 托管的 ComfyUI、获批自定义任务以及受限 Git 检查现在共用同一套 Windows/POSIX 进程组实现。前台超时、Job 超时、显式取消和 FolderBridge 退出都会终止完整的 FolderBridge-owned 进程树，避免不同模块各自维护 `taskkill` / process-group 细节。
- **窗口按真实内容自适应并支持折叠：** Tk 完成布局后，启动器会按页面实际请求尺寸扩大窗口，最多占显示器 94%；“本地工作区与权限”“OpenAI Secure MCP Tunnel”“运行日志”三个区域都可单独展开/收起，顶部还提供“全部展开 / 全部折叠”。内容全部放得下时主滚动条自动隐藏，只有可见内容超出可用高度才显示；DPI 改变、区域折叠或打开/关闭 Extensions 侧栏都会重新计算。
- **托管服务状态实时刷新：** Extensions 侧栏打开时，每 2 秒重新检测一次 ComfyUI 托管状态；在线显示绿色，离线/未配置显示红色，检测中/启动中使用中性色。状态未变化时只更新原有状态标签，不再每 2 秒重建整个侧栏。
- **Judge 类 API 配置文件默认隐藏：** `.api-config.json` / `api-config.json` 已归入凭据类路径，普通 MCP 文件工具不会把它们当项目文本暴露出来。
- **新增浏览器授权的 GitHub 发布链：** bundled `git-publisher` Extension 可通过 Git Credential Manager 打开 GitHub 官方网页授权，OAuth 凭据保留在 Windows Credential Manager；插件只提交显式文件白名单、只把当前分支推到既有 GitHub HTTPS origin，并可把显式工作区文件按受限 tag/title/文件名发布为 GitHub Release assets。通用 Release 的长时间上传使用宿主托管 Job。模型侧不暴露 token/PAT/password 输入字段。
- **Git 发布仍保持强边界：** 拒绝已有 staged 内容、密钥/凭据类文件、会变换内容的 Git attributes 和危险的仓库本地 Git 设置；绝不执行 `git add .`，不接受任意 remote/ref，受控 commit 禁用 hooks/签名，push 永不 force。
- **补齐 Microsoft Office 原生视觉链：** bundled `office` Extension 可对 `.pptx`、`.docx`、`.xlsx` 调用本机已安装的 Microsoft Office 做原生渲染。PowerPoint 直接逐页导出 PNG；Word/Excel 先走各自原生固定版式引擎，再由 Windows 原生 PDF renderer 转成逐页 PNG。
- **Word 不启动 Office 也能做结构读取：** `inspect_docx` 可读取段落、样式/编号、表格、分节与页设置、页眉页脚、媒体、超链接、脚注、尾注、批注等 OOXML 结构。
- **Excel 不启动 Office 也能读公式与结构：** `inspect_xlsx` 可读取工作表、限定单元格区域、公式及缓存值、共享/内联字符串、合并单元格、隐藏行列、定义名称、计算设置和外部链接部件。
- **面向审计的可核验产物：** 原生渲染可同时生成 PNG 目录和同级 ZIP，并返回 Office 原件及每个输出的 bytes/SHA-256；现有 `image_open` 可随后逐页或直接从 ZIP 成员进行视觉检查。
- **Office 自动化仍保持强边界：** 只接受工作区内相对路径和非链接文件；暂不接受带宏 Office 格式；打开前强制禁用 Automation 宏；原件只读打开；Excel 禁止链接更新；调用的是固定 bundled PowerShell 脚本，不接受任意命令/脚本/URL。
- **0.4.x 的安全与界面改进全部保留：** 高 DPI 可滚动界面、ComfyUI 托管诊断、稳定 Extension 网关、有界全局 capability、单文件 Windows EXE 交付方式均不变。

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

运行要求取决于使用方式：

- **Windows 单文件 EXE：** 无需另外安装 Python 或 Node.js；Windows 是已经测试过的双击 Launcher 环境。
- **从源码运行/开发：** 需要 Python 3.11 或更高版本；Windows 构建推荐以 Python 3.11 x64 作为可复现基线。
- **项目能力依赖：** 只安装目标工作区自己需要的工具链，例如 Node/npm 项目的 test/build 脚本需要 Node.js LTS。勾选 capability 不会自动安装这些工具。
- Git 为可选依赖。有界 `status`/`diff` 查看在 Git 可用时工作；若要使用 Git Publisher 的浏览器授权 commit/push，则需要安装带 Git Credential Manager 的 Git for Windows。

### Windows EXE（推荐）

从 [GitHub 最新版本的 Assets](https://github.com/MoonTzai/folderbridge-mcp/releases/latest)下载单个 `FolderBridge.exe`，或[直接下载 EXE](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe)。它不会出现在仓库的源码文件列表中。`FolderBridge.exe` 本身就是完整的 FolderBridge 应用，并已经包含 Python runtime，因此普通 EXE 用户不需要安装 Python 或 Node.js。旁边的 `FolderBridge.exe.sha256` 只是用于完整性校验的可选文件，不影响运行。

单文件 EXE 有意**不会**重新捆绑 OpenAI 独立发布的 `tunnel-client`。只有连接 ChatGPT 网页版时，才需要从 OpenAI 官方 Release 单独下载并在 Launcher 中选择准确的 `tunnel-client.exe`。

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

### 可选开发环境与项目工具链

以下内容**不是**普通 `FolderBridge.exe` 用户的运行前置条件：

- 从源码开发 FolderBridge 或重新封装 Windows EXE 时，推荐从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)安装 **Python 3.11 x64**，并确认 `python --version` 可用。打包流程使用独立的 `.build-venv` 与 `requirements-build.txt`。
- 只有当目标工作区本身是 Node/npm 项目，并且它自己的 test/build 命令需要 `node`/`npm` 时，才从 [Node.js 官方下载页](https://nodejs.org/en/download)安装 **Node.js LTS**，再用 `node --version` 与 `npm --version` 验证。
- 其他项目可能需要自己的 runtime 或编译器。FolderBridge capability 提供的是授权、发现与有边界的执行入口，不负责安装依赖。

## 连接 ChatGPT 网页端

ChatGPT 不能直接连接本地 stdio 进程。FolderBridge 通过 OpenAI 官方 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 完成这条链路。你需要：

> [!IMPORTANT]
> Secure MCP Tunnel 只需要本机向 OpenAI 发起**出站 HTTPS（默认 443）**，不需要开放任何互联网入站端口。不要为 FolderBridge 开启 Windows 远程桌面、路由器端口转发、DMZ、UPnP 映射或 `3389`；这些都不属于 Tunnel 配置。如果防火墙询问是否允许入站访问，不要为了 Tunnel 创建面向公网的宽泛入站规则。

- 从 [OpenAI Platform Tunnel 设置](https://platform.openai.com/settings/organization/tunnels)取得 `tunnel_id`；
- 从同一组织的 [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys)创建供 `tunnel-client` 使用的 Key；
- 官方 [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest)；
- 与账号或工作区相匹配的 ChatGPT 开发者模式权限。

点击启动器右上角“连接设置向导”，可以直接查看以下步骤和快捷按钮，并在附录中查看可选 Python/Node 工具链说明。

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
- `workspace`：在指定 `workspace_id` 内列出和读取文件，对最大 512 MiB 的 UTF-8 文件进行流式 literal search，并以可分页、可按相对路径收窄的方式查看 Git status/diff；
- `file_info`：读取普通文件的有界元数据与整文件 SHA-256；编辑超过 1 MiB 的文本前，用它取得当前 SHA；
- `pptx_inspect`：安全解析 PPTX 文本、OOXML 图关系和 SmartArt 数据，不执行 Office 内容；
- `image_open`：把工作区中的 PNG/JPEG/GIF/WebP（包括 ZIP 内精确成员）作为 MCP 图像内容返回；
- `extension`：固定的插件网关；`list` 返回紧凑且可分页的目录，`info` 返回单个插件的完整 action schema，`run` 执行选定 action；以后安装更多插件不会继续增加 MCP tool 名称；
- `edit_file`：读写模式下创建小型内联 UTF-8 文件，或精确编辑不超过 128 MiB 的已有 UTF-8 文本；编辑已有文件必须携带当前整文件 SHA-256，大文件整文件新建使用 `write_file`；
- `write_file`：读写模式下提供 `begin`、`append`、`status`、`commit`、`abort` 五个固定事务动作，用于最大 512 MiB 的整文件 UTF-8 新建或替换，同时保持 MCP 单消息上限仍为 1 MiB。

只有一个工作区时，旧客户端可以继续省略 `workspace_id`。存在多个工作区时，所有工作区作用域工具都必须携带 `server_info` 返回的 `workspace_id`；缺失或未知 ID 会被拒绝。重复目录、父子重叠目录和超过 8 项的列表也会在启动前被拒绝。

做局部精确替换时：先列出/搜索并读取所需片段，保留当前整文件 SHA-256，再调用 `edit_file`，最后检查 Git diff。`workspace(read)` 会为不超过 1 MiB 的文件返回整文件 SHA；更大的文件改用 `file_info`。新建文件采用 no-clobber 发布；已有文件会在原子发布前再次复核预期 SHA。如果底层文件系统不能提供安全的原子 no-clobber 发布，新建会安全失败，而不会退化成覆盖已有文件。

做大文件整文件新建或替换时使用 `write_file`：先 `begin`（`replace` 还要携带 `file_info` 得到的旧 SHA），再按精确的 UTF-8 字节 offset 连续 `append`，可用 `status` 查看当前 offset，最后带完整新文件字节数和 SHA-256 执行 `commit`，或用 `abort` 放弃。单块最多 128 KiB，确保最坏 JSON 转义后仍低于不变的 1 MiB MCP 单消息上限。事务只在当前服务器进程内存在，暂存文件位于工作区之外，整文件上限 512 MiB；超过 24 小时的陈旧暂存文件会在后续启动时清理。提交前会再次校验 UTF-8、大小、新 SHA、工作区/链接策略和替换目标旧 SHA，再从同目录完整临时文件原子发布；新建模式不会覆盖 `begin` 后突然出现的目标。

大文件检查链路不再退回“小文件搜索”上限。literal search 会以流式方式扫描单个最大 512 MiB 的 UTF-8 文件，并分别报告 binary、非 UTF-8、超限和 I/O 跳过；list/search 使用结果 offset 分页，Git status/diff 使用字节 offset 分页，并可按一个工作区相对路径收窄。这里扩展的是可继续取下一页的能力，而不是放大单次响应；MCP 1 MiB envelope 保持不变。

Skill 路由也采用同样的规模化策略。初始化只携带 64 KiB 的紧凑 round-robin 路由索引，不嵌入 Skill 正文；如果启用的 Skill 放不下，会明确报告遗漏数量，而 `skill-engine match` 始终保留为完整的任务级发现入口。Extension `list` 同样保持紧凑可分页，完整 schema 延后到 `extension(info)` 获取。

外部 Skill Pack 与 Extension 属于用户安装内容，不属于 Release payload。它们的精确 hash 批准记录保存在用户级 FolderBridge 配置目录中；只要外部文件内容／声明权限没有变化，升级 EXE 不会要求重新批准。源码仓库里的 `skill_packs` 与 `extensions` 是 Release 自身的源目录；Windows 构建现在只封装显式 bundled allowlist，因此其中临时放置的 untracked／第三方目录不会再被误打进 EXE。若未来某个外部组件被正式收编为 bundled，且与旧外部安装使用同一个 ID，则 bundled 版本安全优先，旧外部副本被忽略而不是持续报 duplicate-ID；外部代码始终不能覆盖 bundled ID。

### 有界 MCP 并发

FolderBridge 0.8.0 将请求拆成两个有界 lane，让长时间的数据面任务不会拖死控制/状态请求。当前默认是 2 个 control worker、最多 8 个 control in-flight，以及 6 个 data worker、最多 12 个 data in-flight。达到上限时不会继续无界排队，而是直接返回 JSON-RPC `-32001` / `Server busy`。并发响应允许按 request id 乱序完成（JSON-RPC 本身允许），但 FolderBridge 会串行写出完整 JSONL，因此不同响应的字节不会交叉。

控制面包括初始化/ping/工具目录、`server_info`、Extension list/info/job status/cancel，以及事务写入的 status/abort。读取与互不冲突的数据任务可以在其他数据任务运行时继续执行。同一目标文件的核心写入会串行，不同目标文件可以并行；task、build/package/capability 以及非只读 Extension action 被视为无法预知写入范围的 workspace mutation，在同一工作区内不会与核心文件写入并发。无论是前台非只读 Extension action 还是非只读 Extension Job，都会一直持有工作区 mutation lease，直到宿主确认 worker 进程已经退出；如果终止暂时无法确认，就进入宿主管理的 `termination_pending`，继续持锁，并由有界 daemon reaper 在进程后续退出时自动收口。16 个 Job 与 16 个前台 worker 的生命周期预算彼此独立，也与 MCP request worker 预算分离。stdio 关闭时会先关闭 mutation admission 并唤醒排队中的 mutation waiter，再重试终止宿主拥有的 Extension worker，然后 drain 有界 request worker，最后清理事务暂存。

MCP 不能删除或移动文件。绝对路径、`..`、符号链接、junction/reparse point、版本控制内部文件、常见依赖/构建目录和疑似凭据文件名都会被拒绝。

## 全局预授权能力

启动器可以把常见能力一次授权给当前和未来所有工作区：`test`、`build`、`package-windows`、`package-android` 和 `git-push`。这些权限保存在启动器设置中，不写进每个工作区的 `.folderbridge.json`。主界面同时提供“全选”和“清空”按钮。

对已经全局预授权的 `test` 与 `build`，FolderBridge 现在保证每个已选择工作区都有可用 provider。若项目本身存在 `npm run test`、`npm run build`、Python unittest/pytest 等受支持入口，继续优先执行项目入口；若没有，`test` 自动使用 FolderBridge 自有的有界 workspace smoke：复用核心的凭据/VCS/依赖目录/link 拒绝策略，检查常见 UTF-8 文本、JSON/HTML 结构，并在可信系统 Node 可用时用 `--check` 做有界 JavaScript 语法解析而不执行工作区 JavaScript。`build` 则使用不修改工作区的安全 fallback：静态 HTML、文档/内容型和本来无需构建的目录明确返回 `identity` 模式；有源码但没有构建入口的目录明确返回 `validation-only` 模式。两者都不会伪造编译产物，`server_info` 会报告实际选中的 provider。

`git-push` 继续作为底层“只推送”能力：限制为 GitHub HTTPS `origin`、当前分支、禁止 force push，并拒绝仓库本地的 credential helper、pushurl/receivepack 和 URL 重写配置。若需要浏览器授权＋显式文件 commit＋push，请改用 bundled **Git Publisher** Extension。显式项目构建/封装能力可能执行本地项目代码，所以只勾选你愿意全局预授权的能力。

直接 stdio 客户端可在 `serve` 或 `client-config` 命令中重复加入 `--capability <名称>`；Windows 启动器提供同样的持久化复选框。

## Extensions 插件

FolderBridge Extension ABI v1 用于以后增加 ComfyUI、FFmpeg、Blender、Ollama、ADB 等全局集成，而不继续修改 MCP 工具目录。Windows 主界面右侧有默认折叠的 **Extensions** 侧栏：可以热扫描用户插件目录，显示已安装/已批准/已加载状态，并且在 Tunnel 保持连接时直接批准、加载或停用插件。

每个插件至少包含 `folderbridge-extension.json` 和 `plugin.py`。外部插件的批准绑定“完整插件目录 SHA-256 + permissions”；任一文件或权限变化都会让旧批准自动失效。插件在独立子进程中运行，使用清理环境、固定超时和有界协议 I/O。这能隔离崩溃与协议污染，但**不是完整的操作系统沙箱**；不可信插件仍应放进 VM/容器。

需要适配具体项目时，应使用 `workspace_adapter.mode=dynamic` 与 `detect.any_of` / `detect.all_of`。FolderBridge 每次调用都会重新检测，因此插件安装时不需要向每个工作区注入 `.folderbridge.json` task，项目以后才出现相关脚本也能自动变为可用。插件持久状态优先使用 FolderBridge 提供的 profile state 目录，不要污染仓库。

公开 Extension action 应保持**小而固定、语义有界**。不要暴露 `run-all`、`verification-suite`、`do-everything` 这类把几十个互相独立的测试/检查或多阶段流水线塞进一次前台 MCP 调用的聚合总入口。应拆成固定白名单、语义明确、超时和输出可预测的小 action，由客户端按顺序逐项调用；如需告诉客户端标准顺序，可额外提供只返回计划、不执行子进程的只读 `verification-plan`。真正属于一个原子语义单元的长任务，在当前客户端能可靠查询/取消 Job 时可使用 host-owned Job；但不要用 Job 把本应拆分的聚合入口藏起来。

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

### 发布 GitHub Release

仓库 push 与 GitHub Release 明确分离。Git Publisher 1.3.0 保留零参数 `release` 动作作为 FolderBridge 自身的兼容锁定发布路径：版本只允许从 `pyproject.toml` 读取稳定的 `x.y.z`，只允许在 `main`、tracked 工作树干净且 `origin/main` 已与当前 HEAD 一致时发布；标签固定为 `v<version>`，资产固定为 `release/windows-x64/FolderBridge.exe` 与 `FolderBridge.exe.sha256`。独立的通用 `release-assets` 动作只作用于当前选中的工作区仓库，接受受限 tag/title 与显式普通文件白名单；tracked 内容必须干净、当前分支必须已与 origin 对齐，禁止移动既有 tag 与 force push，同时允许显式未跟踪构建产物使用安全的 Release 文件名上传。资产在任何远端修改前都会做 SHA-256 校验并复制为临时快照，长时间上传以宿主托管 Job 运行。Release 认证直接复用已经通过浏览器授权的 Git Credential Manager 账号：隔离 worker 从 GCM 取得凭据，只在本次操作期间通过子进程环境交给 `gh.exe`，因此不再要求额外执行 `gh auth login`。模型不能传入 token/PAT/password 或任意 Git/`gh` 命令参数，凭据也不会由 FolderBridge 持久化或通过 MCP 返回。

仓库端 `.github/workflows/release-windows.yml` 仍保留为第二条发布路径：当最终 commit 标题严格为 `Release FolderBridge <version>` 时，它会独立重新读取版本、执行完整 Windows 测试、构建并验证 EXE，然后创建或修复对应 Release。已有 tag 只有在确实指向同一个 release commit 时才会被接受；已有 Release 可以重新上传两个固定资产并重新标记为 Latest。

## 许可证

FolderBridge 自行创作的项目代码与文档采用 [Apache License 2.0](LICENSE) 授权。

内置的 `folderbridge-engineering` Skill Pack 含有基于 Matt Pocock 的 [`mattpocock/skills`](https://github.com/mattpocock/skills) 中选定工程 Skills 精简／改编的方法文本。上游版权归 Matt Pocock 所有，上游内容采用 MIT License。FolderBridge 在 [`skill_packs/matt-pocock-engineering/NOTICE.md`](skill_packs/matt-pocock-engineering/NOTICE.md) 与 [`LICENSE.upstream-MIT.txt`](skill_packs/matt-pocock-engineering/LICENSE.upstream-MIT.txt) 中保留来源、归属与完整 MIT 许可文本。该 Pack 是 FolderBridge 的适配版本，不是 Matt Pocock 官方插件，也不暗示 Matt Pocock 或 AI Hero 对 FolderBridge 的从属、背书或赞助关系。

FolderBridge MCP 是独立开源项目，与 OpenAI 不存在从属、背书或赞助关系。ChatGPT、OpenAI、MCP 及其他产品名称归各自权利人所有。
