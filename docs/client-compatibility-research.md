# FolderBridge 本地 MCP 客户端兼容性研究

> 调研日期：2026-08-22。资料范围仅限 MCP 官方规范、OpenAI 官方文档、Codex 官方文档和 VS Code 官方文档；客户端版本与配置格式仍可能变化。

## 结论

FolderBridge 已预留通用本地 MCP 接口，不依赖 ChatGPT 网页端。一个客户端只要能在**工作区所在机器**上启动前台子进程、维持双向 `stdin`/`stdout`，并实现 MCP 的工具发现与调用，就可以直接接入，不需要 HTTP、OAuth 或 Tunnel。MCP 官方把 stdio 定义为由客户端启动服务器子进程，以换行分隔的 UTF-8 JSON-RPC 消息通信；`stdout` 只能承载协议消息，日志走 `stderr`。[MCP 2026 stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)

当前 FolderBridge 同时支持两条协议路径：

- 2026-07-28：请求在 `_meta` 中携带协议版本和客户端能力，可先发 `server/discover`，再发 `tools/list`、`tools/call`；
- 2025-11-25、2025-06-18、2025-03-26、2024-11-05：先完成 `initialize` → `notifications/initialized`，再发 `tools/list`、`tools/call`。

这些版本和入口可见当前[协议实现](../folderbridge_mcp/mcp.py#L10)。2026 版规范规定服务器必须实现 `server/discover`，但客户端可以跳过探测并直接调用其他 RPC；旧版规范则要求 `initialize` 是首个交互，并在成功后发送 `notifications/initialized`。[MCP 2026 Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)；[MCP 2025-11-25 Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)

## FolderBridge 当前兼容性评估

- **协议与传输：线协议上可直接兼容。** 实现覆盖最新无状态路径和四个常见初始化版本，且本地工作流测试分别执行了 `initialize` 与 `server/discover` 后的工具枚举/调用，见[测试](../tests/test_mcp_workflow.py#L57)；这不代表已经逐一实测所有第三方客户端及其版本。
- **客户端配置：核心字段兼容，外层格式需适配。** CLI/GUI 生成绝对 `command` 和参数数组，并提供常见 `mcp_servers` TOML、`mcpServers` JSON 以及 Tunnel 命令；目标客户端若使用 `servers` 等其他外壳，应只移植 `command`/`args`。
- **安全 UX：服务器信息充分，但逐次审批属于客户端。** FolderBridge 提供读写 annotations，并能通过 `--read-only` 或不启用 `--allow-tasks` 从工具目录硬性移除相应能力；它不能要求任意第三方客户端一定弹出确认框。
- **远程接入：有意不直接兼容 HTTP-only Host。** FolderBridge 没有 Streamable HTTP listener；这减少了本地攻击面，但网页/云端或只接受 URL 的客户端必须增加受信任桥接层。
- **范围限制：不是完整 MCP 功能服务器。** 它只实现当前文件工作流所需的发现/初始化、ping、取消通知和 Tools 子集，不提供 Prompts、Resources、Roots、Sampling、Elicitation 或协议 Tasks；客户端不能把这些可选能力视为接入前提。

## 客户端最低兼容条件

| 层面 | 直接接入需具备或建议 | FolderBridge 不要求 |
| --- | --- | --- |
| 进程 | 能以 `command` 和独立的 `args` 数组启动本地前台进程；保持管道直到会话结束 | 常驻服务、管理员权限、浏览器扩展 |
| 传输 | UTF-8、每行一个 JSON-RPC 2.0 消息；读取 `stdout`，可单独收集 `stderr` | HTTP、SSE、OAuth |
| 协议 | 支持上列任一协议版本；能完成对应发现/初始化流程 | Roots、Sampling、Prompts、Resources、Elicitation、MCP Tasks |
| 工具 | 能处理 `tools/list` 的 JSON Schema，并用对象参数发 `tools/call`；至少消费返回的 `content`/`isError` | 必须消费 `structuredContent`（FolderBridge 同时返回文本 `content`） |
| 生命周期 | 断开时先关闭子进程 `stdin`，等待退出，必要时终止进程；设置启动和调用超时 | MCP 专用 shutdown RPC |
| 安全 UX | 安全接入时应让用户看到服务器与工具，敏感调用前显示参数并允许拒绝 | 仅凭 annotations 自动获得安全保证 |

MCP 对 stdio 的标准关闭顺序也是“关闭输入流 → 等待退出 → 必要时强制终止”，并建议对请求设置超时；FolderBridge 在读到 EOF 时退出。[MCP 2026 stdio shutdown](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio#shutdown)；[MCP 2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#shutdown)

## 协议调用顺序

传统客户端最小流程如下：

```text
spawn FolderBridge
  → initialize(protocolVersion, capabilities, clientInfo)
  ← protocolVersion, capabilities.tools, serverInfo
  → notifications/initialized
  → tools/list
  ← server_info, workspace[, edit_file][, run_task]
  → tools/call(name, arguments)
  ← content + structuredContent + isError
close stdin → wait/terminate
```

2026-07-28 客户端改为在每个请求的 `_meta` 中携带 `io.modelcontextprotocol/protocolVersion` 和对象形式的 `io.modelcontextprotocol/clientCapabilities`，不再依赖 `initialize`；可选的首个探测是 `server/discover`。[MCP 2026 stdio request metadata](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio#request-metadata)；[MCP 2026 tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

FolderBridge 只声明 `tools` 能力且工具列表不动态变化，所以客户端无需实现服务器反向请求、资源订阅或工具列表变更通知。单条入站消息上限为 1 MiB，见[实现](../folderbridge_mcp/mcp.py#L91)。

## `command`、`args`、`cwd` 与 `env`

MCP 只统一线协议，并未规定所有产品必须使用同一种配置文件外壳。这一点可以从第一方客户端文档直接看出：Codex 使用 `[mcp_servers.<name>]` TOML 表，支持 `command`、`args`、`env`、`env_vars`、`cwd`；VS Code 使用 JSON 的 `servers` 对象和 `type: "stdio"`，支持 `command`、`args`、`cwd`、`env`、`envFile`。[Codex MCP 配置](https://developers.openai.com/codex/mcp#stdio-servers)；[VS Code MCP 配置参考](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#_standard-inputoutput-stdio-servers)

因此，FolderBridge 的 `client-config --format toml|json` 是两种常见模板，不是“任意客户端可原样粘贴”的协议标准。接入其他客户端时应按该客户端第一方文档放置配置，但复用生成结果中的这两个字段：

```text
command = Python/FolderBridge EXE 的绝对路径
args    = [launcher, serve, --workspace, 工作区绝对路径, 可重复 --workspace, 可选 --read-only/--allow-tasks]
```

建议：

- 保持 `command` 与每个参数分离，不把整条命令交给 shell 重新解析；路径含空格时仍作为一个数组元素传递。
- FolderBridge 通过每个 `--workspace` 固定独立边界，故 `cwd` 不是必需项，也不能代替工作区边界；多工作区时工具调用还必须携带 `server_info` 返回的 `workspace_id`。若客户端要求 `cwd`，可设为其中一个仓库根目录，但它不会改变允许范围。
- FolderBridge 本地 stdio 本身不需要 secret 环境变量。客户端若支持 `env`，也应只显式传必要变量，避免把宿主密钥无差别继承给子进程；不要在 stdin/stdout 上插入交互式登录提示，因为两条流只能承载 MCP 消息。
- 进程必须以前台方式运行。VS Code 官方文档也明确提醒，Docker stdio 服务不能使用 detached 模式，因为客户端需要持续使用标准流。[VS Code MCP 配置参考](https://code.visualstudio.com/docs/agents/reference/mcp-configuration#_standard-inputoutput-stdio-servers)

## 读写标记与审批边界

MCP **不强制某一种审批界面**，但建议始终保留人类拒绝工具调用的能力，并建议对敏感操作确认、调用前展示工具输入、校验结果及记录审计信息。[MCP Tools：User Interaction Model 与 Security Considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

FolderBridge 当前标记为：

| 工具 | annotations | 客户端建议 |
| --- | --- | --- |
| `server_info`、`workspace` | `readOnlyHint: true`、`destructiveHint: false`、`openWorldHint: false` | 可按客户端策略减少确认，但仍应让用户知道工作区已暴露给模型 |
| `edit_file` | `readOnlyHint: false`、`destructiveHint: true`、`idempotentHint: false` | 每次显示文件路径与参数并确认；不支持审批的客户端用 `--read-only` |
| `run_task` | `readOnlyHint: false`、`destructiveHint: true`、`idempotentHint: false`、`openWorldHint: true` | 仅在本机已批准任务配置且客户端有清晰确认 UX 时启用 `--allow-tasks` |

具体工具标记和按启动模式隐藏写入/任务工具的逻辑见[工具实现](../folderbridge_mcp/tools.py#L43)。规范明确规定 Tool Annotations 全是**提示**，默认也偏保守；不可信服务器提供的 annotations 不能成为自动执行的安全依据。[MCP 2025-11-25 ToolAnnotations](https://modelcontextprotocol.io/specification/2025-11-25/schema#toolannotations)

Annotations 从 2025-03-26 才进入规范，2024-11-05 客户端可能完全忽略；`tools/call` 也没有标准的 `approved: true` 字段或可验证的“用户已审批”票据。[MCP 2025-03-26 changelog](https://modelcontextprotocol.io/specification/2025-03-26/changelog)；[MCP 2025-11-25 tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

因此应区分两种“兼容”：不理解 annotations 的客户端仍可能在协议上正常调用工具，但不等于具备 FolderBridge 推荐的安全体验。没有逐次审批或工具 allowlist 的客户端，建议只生成 `--read-only` 配置；服务器端路径、哈希和任务策略检查仍是最终强制边界。

## 何时需要 HTTP 或 Tunnel 桥接

| 场景 | 是否需要桥接 |
| --- | --- |
| 本机桌面/IDE/CLI 客户端能直接启动 stdio 子进程 | 不需要，直接配置 `command + args` |
| 客户端运行在容器、WSL、SSH 远端或远程开发主机，但工作区也在那里，且客户端能在那里启动进程 | 通常不需要；应把 FolderBridge 与工作区部署在同一执行环境，并改用该环境中的绝对路径 |
| 纯网页或云端 Host 无法启动用户机器上的进程 | 需要由本机 companion、Tunnel 或受控网关桥接 |
| 客户端只接受 Streamable HTTP URL | 需要 stdio↔HTTP 代理/桥；FolderBridge 自身不监听网络 |
| 要做公开插件分发或供任意远程客户端长期调用 | 需要稳定、可公开访问且带认证的 HTTPS MCP 服务；这已超出 FolderBridge 当前本地优先边界 |

OpenAI Secure MCP Tunnel 是上述第三种场景的一个产品专用方案：`tunnel-client` 在能访问私有 MCP 服务的机器上发起出站 HTTPS，把 OpenAI 侧 JSON-RPC 请求转发到本地 stdio/HTTP 服务，不要求 FolderBridge 开放公网端口。官方也说明 Tunnel 用于私有连接与开发测试，不替代公开插件所需的稳定公网 HTTPS endpoint。[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

对其他厂商客户端，不应默认复用 OpenAI Tunnel；应先看该客户端是否有本地 stdio host、官方远程代理或 companion。若没有，才评估独立 HTTP 网关，并重新处理 Origin 校验、localhost 绑定、认证、凭据和网络暴露。MCP 官方对 Streamable HTTP 明确要求校验 `Origin`，本地服务优先只绑定 `127.0.0.1`，并为连接实现认证。[MCP 2025-11-25 Streamable HTTP security](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#security-warning)

## 对外文档可压缩成的兼容声明

> FolderBridge 可直接连接任何能在工作区所在机器上启动本地 stdio MCP 进程、支持 `tools/list`/`tools/call`，并兼容 MCP 2024-11-05 至 2025-11-25 初始化流程或 2026-07-28 无状态流程的客户端。把生成配置中的 `command` 和 `args` 按目标客户端的配置格式填入；不支持写操作确认时请使用 `--read-only`。只有网页/云端 Host 无法启动本地进程，或客户端只接受 HTTP URL 时，才需要其官方 Tunnel、companion 或受控 stdio↔HTTP 网关。
