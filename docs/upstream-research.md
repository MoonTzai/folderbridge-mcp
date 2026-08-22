# coding-tools-mcp 上游研究与安全重写建议

> 审计快照：上游 `main` 的 `ed85e41999b0cf840d6e45f2bed11ac7f52eab3f`（2026-08-17）；最近正式版为 `v0.3.0` / `5d6e131afebd89f98438b1c1dca8d157c0713c8a`（2026-08-13）。主分支在该版本后还有 CLI 版本显示和客户端配置文档等提交，故本文以主分支快照分析实现，以发布页说明稳定版本。[主分支快照](https://github.com/xyTom/coding-tools-mcp/tree/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f)；[v0.3.0 发布页](https://github.com/xyTom/coding-tools-mcp/releases/tag/v0.3.0)

## 1. 结论先行

这个项目的核心价值不是“远程 MCP 服务”本身，而是把一组本地编码原语包装成 MCP 工具：受工作区限制的文件读写、补丁、命令进程管理和 Git 查询。当前实现已经做了不少认真防护，但同时承担了手写协议兼容、Streamable HTTP、OAuth、隧道、桌面进程管理、遥测以及 18 个工具，导致安装链、信任边界和用户心智都偏重。[工具注册表](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L570-L697)；[协议分发实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/protocol.py#L367-L502)

最简单且更安全的重写路线是：

1. 第一版只做本地 `stdio`，使用官方 Python MCP SDK/FastMCP，不自行解析两代 JSON-RPC，也不开放端口。官方 SDK 本身把“本地服务器用 stdio、部署服务用 HTTP”作为默认选择，并可由类型标注生成工具 schema。[官方 SDK：运行方式](https://github.com/modelcontextprotocol/python-sdk/blob/57394b0548d1e2dc2dce8d67d84985769df3b8bb/docs/run/index.md#L5-L17)；[官方 SDK：工具与 schema](https://github.com/modelcontextprotocol/python-sdk/blob/57394b0548d1e2dc2dce8d67d84985769df3b8bb/docs/servers/tools.md#L5-L21)
2. 第一里程碑只提供只读工具与一个经过预览/确认的补丁工具；先不要提供任意 shell。`exec_command` 即使在上游“safe”模式中也不是完整沙箱；项目自己的限制文档明确说分类可能漏掉解释器、包脚本、静态二进制或生成文件中的行为，网络禁止也只是策略级。[上游限制声明](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/docs/limitations.md#L1-L8)
3. 如后续确需执行，提供固定任务 `run_task(test|lint|format|build)`，使用“可执行文件 + 参数数组、`shell=false`、最小环境、默认断网、外部 OS 沙箱”，而不是接受任意命令字符串。MCP 官方安全建议也要求本地代理限制文件系统、记录操作，并把危险命令放进沙箱/容器或增加授权。[MCP 官方安全最佳实践](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/docs/2026-07-28/tutorials/security/security_best_practices.mdx#L783-L794)
4. 不自动把仓库内 `AGENTS.md`/`CLAUDE.md` 提升为服务器 instructions；只把它们作为带“仓库不可信内容”标签、由用户主动读取的普通资源。上游会自动读取根文件并放入初始化/发现响应的 instructions，这构成提示注入通道，而非传统代码执行漏洞。[上游项目上下文加载](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/project_context.py#L49-L106)；[README 对行为的说明](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/README.md#L148-L153)
5. 第一版不要 HTTP/OAuth/隧道/桌面守护进程/云端控制面/遥测，也不要 `curl | bash`。这几项并非本地工作流的必要条件，却分别引入网络暴露、凭据、供应链和隐私风险。

建议把目标产品定义为：“一个由 MCP 客户端按需启动、每次绑定一个明确工作区、默认只读、变更前给出精确预览并请求一次性确认的本地工具进程。”这比复制上游全部能力更符合“体验便捷但风险更小”的目标。

## 2. 范围、方法与限制

- 本报告只使用上游源码、README/安全文档、提交/发布记录，以及 MCP 官方规范和官方 Python SDK；没有使用第三方解读。
- 这是静态审计：阅读了入口、协议、传输、工作区路径、补丁、命令策略、环境变量、HTTP/OAuth、遥测、安装脚本、npm 启动器、桌面客户端和 Cloudflare 控制面。没有执行上游安装脚本、隧道或未知项目命令。
- “源码事实”均链接到固定 commit；“风险”是从这些事实推导出的攻击面，不代表已经发现可远程利用的公开漏洞。

## 3. 项目是怎样组成的

### 3.1 包与入口

上游要求 Python 3.11+，核心运行依赖只有 PyJWT；PySide6/psutil、Pillow 等通过可选 extras 提供桌面和图片能力。Python 命令入口为 `coding_tools_mcp.server:main`，桌面入口另行注册。[`pyproject.toml`](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/pyproject.toml#L5-L47)

npm 包不是服务的 JavaScript 实现，而是一个薄启动器：优先 spawn `uvx coding-tools-mcp`，失败时回退 `pipx run`；只有设置 `CODING_TOOLS_MCP_VERSION` 时才固定 Python 包版本。npm 包没有 `postinstall`，但默认启动仍会在运行时解析并执行未固定版本的 Python 包。[npm 启动器](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/npm/coding-tools-mcp/bin/coding-tools-mcp.js#L1-L56)；[npm package.json](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/npm/coding-tools-mcp/package.json#L1-L36)

### 3.2 MCP 与运行时

生产服务器没有依赖官方 MCP SDK，而是自行实现 `server/discover`、旧式 `initialize`、`tools/list`、`tools/call`、错误映射及现代/旧协议兼容；stdio 层按行读写 JSON-RPC，HTTP 层也在项目内实现。协议版本常量包含 2026-07-28、2025-11-25 和 2025-06-18。[协议常量与能力](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/protocol.py#L11-L57)；[协议分发](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/protocol.py#L367-L502)；[stdio 传输](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/transport_stdio.py#L45-L88)

服务器运行时将一个 canonical workspace、一个共享命令管理器、补丁器、项目上下文和遥测会话组装在一起。因此一个服务器进程中的工作区就是主要信任域，命令 ID、活动进程上限、输出留存和补丁锁也在该运行时内共享。[运行时构造](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L1266-L1360)

### 3.3 工具目录

固定快照中共有 18 个工具。[完整注册表](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L570-L697)

| 类别 | 工具 |
|---|---|
| 运行状态 | `server_info`、`check_exec_environment` |
| 文件与补丁 | `read_file`、`list_dir`、`list_files`、`search_text`、`apply_patch`、`view_image` |
| 命令进程 | `exec_command`、`write_stdin`、`read_output`、`kill_command`、`request_permissions` |
| Git 只读 | `git_status`、`git_diff`、`git_log`、`git_show`、`git_blame` |

### 3.4 桌面和远程层

桌面程序不是聊天型 MCP 客户端；它保存每工作区 profile，管理 MCP 服务器/隧道进程，展示健康状态和日志，并生成可复制的客户端配置。默认 profile 使用 OAuth、`trusted` 权限模式和 28766 端口。[桌面 profile 模型](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/models.py#L34-L49)；[桌面运行命令构造](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/runtime.py#L518-L577)

仓库还包含 Cloudflare Worker 控制面，可在持有 `CONTROL_TOKEN` 时用 `GITHUB_TOKEN` dispatch GitHub Actions；这与纯本地编码工具并非同一最小边界。[Cloudflare 控制面](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/cloudflare/sandbox-control/src/index.mjs#L12-L27)；[工作流 dispatch](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/cloudflare/sandbox-control/src/index.mjs#L149-L185)

### 3.5 版本演进

`v0.3.0` 是一次边界收敛版本：发布记录称 HTTP 改为 stateless，命令 ID 归属工作区共享运行时，移除 session cwd 一类工具，工具目录固定为 18 个，并加入 2026-07-28 协议与旧协议兼容；补丁锁也移动到共享运行时。[v0.3.0 发布页](https://github.com/xyTom/coding-tools-mcp/releases/tag/v0.3.0)；[固定快照 changelog](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/CHANGELOG.md#L3-L135)

该标签之后到本报告快照的提交主要是扩展客户端配置文档和暴露 CLI 版本；因此安全分析以 `ed85e41` 的实际源码为准，而不是只读 `v0.3.0` 宣传摘要。[v0.3.0 后提交比较](https://github.com/xyTom/coding-tools-mcp/compare/v0.3.0...ed85e41999b0cf840d6e45f2bed11ac7f52eab3f)

## 4. 安装与端到端工作流还原

### 4.1 本地 stdio

README 的本地路径是让 MCP 客户端启动 `uvx coding-tools-mcp --stdio --workspace /path/to/repo`，也提供 `npx coding-tools-mcp` 包装方式和多个客户端配置片段。[README 快速开始](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/README.md#L35-L68)

端到端时序是：

1. 用户在客户端配置一次可执行命令和 workspace。
2. 客户端拉起子进程并通过 stdin/stdout 传递逐行 JSON-RPC；stdout 只承载协议，日志走 stderr。[上游 stdio 实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/transport_stdio.py#L45-L88)；[MCP 官方 stdio 要求](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/basic/transports/stdio.mdx#L7-L16)
3. 新协议客户端可先调用 `server/discover`，随后 `tools/list`；调用能力时发 `tools/call`。旧客户端走 `initialize`，兼容分支由上游协议层处理。[上游协议分发](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/protocol.py#L367-L502)；[MCP 官方工具列举](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#listing-tools)
4. `exec_command` 短任务直接返回；长任务返回 opaque `command_id`，客户端可用 `read_output` 分页读取、`write_stdin` 交互、`kill_command` 终止。[命令工具定义](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L626-L690)
5. `apply_patch` 先解析并暂存所有变化、校验基线，再使用同目录临时文件/备份提交；失败时进行 best-effort 多文件回滚。[补丁事务实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/patching.py#L87-L170)

### 4.2 HTTP/远程

不加 `--stdio` 时服务器启动 Streamable HTTP；默认绑定 127.0.0.1，可配置静态 bearer 或 OAuth。源码拒绝在没有 bearer/OAuth 的情况下直接绑定非 loopback，除非使用显式无认证选项。[HTTP 启动与非 loopback 检查](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L5603-L5671)

MCP 官方也要求 Streamable HTTP 校验 `Origin`、本地运行时优先绑定 localhost，并为远程连接实现认证，因此这层复杂性是真实安全需求，而不是可省略的小装饰。[MCP 官方 Streamable HTTP 安全要求](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/basic/transports/streamable-http.mdx#L58-L65)

### 4.3 一个直接的体验问题：端口文档不一致

README/quickstart 的直接 HTTP 示例展示 8765，但 CLI parser 的默认端口是 8000；安装脚本又显式默认 8765。用户若直接执行 Python 入口而未加 `--port`，照抄文档 endpoint 会连错端口。[README endpoint](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/README.md#L61-L72)；[quickstart endpoint](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/docs/quickstart.md#L43-L56)；[CLI 默认 8000](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L5707-L5719)；[安装脚本默认 8765](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/scripts/install.sh#L10-L15)

## 5. 哪些地方可以显著简化

### 5.1 用官方 SDK 取代协议和 schema 手写层

上游生产代码自行维护两代发现/初始化流程、工具 schema、错误语义、stdio 和 HTTP。独立重写可把协议兼容交给官方 SDK，只保留领域逻辑；FastMCP 可以用 Python 类型生成 schema，`Annotated`/`Field` 约束还能直接进入验证。[官方 FastMCP 工具定义](https://github.com/modelcontextprotocol/python-sdk/blob/57394b0548d1e2dc2dce8d67d84985769df3b8bb/docs/servers/tools.md#L5-L21)；[官方参数约束](https://github.com/modelcontextprotocol/python-sdk/blob/57394b0548d1e2dc2dce8d67d84985769df3b8bb/docs/servers/tools.md#L109-L116)

这会删掉三类重复工作：协议版本分支、schema/handler 双份登记、HTTP/stdio 生命周期细节。兼容性升级也跟随官方 SDK，而不是本地继续扩张 `protocol.py` 和 `server.py`。

### 5.2 stdio-only 消除本地无关能力

本地客户端已经负责启动/停止 stdio 子进程；第一版无需常驻 daemon、端口、健康探测、bearer、OAuth、PKCE、动态注册、隧道或 Cloudflare Worker。官方传输模型也明确规定客户端启动 stdio 子进程，并通过关闭 stdin 和终止进程完成生命周期。[MCP 官方 stdio](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/basic/transports/stdio.mdx#L7-L16)

### 5.3 合并重叠工具

- `list_dir` 与 `list_files` 合并为 `find_files`，用 `recursive`、`max_depth`、`limit` 参数表达差异。
- `read_file` 保留为 `read_text`，默认 UTF-8，要求 range 与字节上限。
- `git_status` 与 `git_diff` 可在第二阶段合并为 `review_changes`；`log/show/blame` 不是让工作流跑通的必要条件。
- `write_stdin/read_output/kill_command` 只在引入持续进程后再增加；第一版固定任务应尽量同步、超时可取消。
- `server_info` 和 `check_exec_environment` 可合并为 `doctor`，同时输出实际启用的能力、沙箱是否可用和拒绝原因。

### 5.4 让安装变成一次生成配置，而不是搭服务

建议 CLI 只有三个用户入口：

1. `tool init <workspace> --client codex|claude|cursor`：canonicalize workspace，展示将写入/复制的精确客户端配置，经用户确认后再修改配置。
2. `tool doctor <workspace>`：检查包版本/哈希、工作区范围、客户端命令、有效能力和 OS 沙箱状态。
3. `tool serve --stdio --workspace <canonical-path>`：通常由 MCP 客户端启动，用户无需手动运行。

安装应是带版本和哈希的本地包安装，不从 `main` 执行脚本，不在首次调用时静默更新运行时。

## 6. 静态安全审计

风险等级表示在“客户端或工作区内容不完全可信”的目标模型下，对独立重写优先级的判断。

### 6.1 高风险：任意命令执行的边界不是沙箱

`exec_command` 接受命令字符串，再通过正则、路径参数分析、危险命令分类和权限模式决定是否运行；网络检测也依赖命令文本匹配。[命令策略核心](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L2349-L2538)

Linux 可用时，代码用 Landlock 限制文件系统访问；非 Linux 或没有 Landlock 的主机仍会在策略检查后执行，只返回警告。Landlock 代码本身没有提供网络隔离。[Landlock 实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L4053-L4201)；[上游安全说明](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/SECURITY.md#L26-L30)

关键问题不是正则是否“够多”，而是测试、构建和包管理器本来就会执行仓库代码。恶意行为可以藏在 package script、测试 fixture、编译器插件、静态二进制或生成文件里；上游也明确承认 shell/test runner 会执行任意项目代码，并要求外部沙箱。[上游 residual risks](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/SECURITY.md#L96-L101)

**重写边界：** 不提供任意 shell。若要运行测试，固定任务模板、`shell=false`、参数 allowlist、最小 env、无凭据、默认实际断网，并把整个 helper 放进外部 OS 沙箱；若当前平台无法实现，明确显示“未沙箱”，拒绝对不可信仓库执行，而不是把字符串过滤标成 safe。

### 6.2 高风险：仓库文本可成为提示注入

上游会读取根目录 `AGENTS.md`/`CLAUDE.md` 的文本并放入服务器初始化/发现 instructions，嵌套上下文文件名也会被枚举。[上下文文件识别与加载](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/project_context.py#L9-L106)

这意味着仅“连接一个仓库”就可能把仓库作者控制的自然语言提升到比普通工具输出更靠前的指令通道。它不直接绕过 Python 权限检查，但可诱导代理调用变更/执行工具、索取放宽权限或泄露上下文。

**重写边界：** 默认不自动读取此类文件；用户主动读取时返回 `source=workspace, trust=untrusted` 元数据，客户端 UI 清楚显示来源。仓库内容永远不能改变服务器策略、审批或工具注解。

### 6.3 高风险：`request_permissions` 不是逐次用户授权

危险模式下 `request_permissions` 直接返回自动 grant；其他模式返回“不支持运行时提权”，建议重启到更宽模式，而不是进行一次真正的用户 elicitation/确认。[`request_permissions` 实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L3397-L3425)

MCP 官方要求界面清楚展示工具、调用时提供可视提示，且人应始终能拒绝；工具 annotations 在服务器不可信时也不能被客户端当作可信安全事实。[MCP 官方 human-in-the-loop](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/server/tools.mdx#L34-L40)；[annotations 信任要求](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/server/tools.mdx#L300-L306)

**重写边界：** 每次变更/执行先返回精确计划：canonical 路径、diff 或 executable/argv、cwd、会传递的环境变量名、是否联网。确认凭据只绑定“工作区 ID + 操作内容哈希 + 短过期时间 + 单次 nonce”，不能成为全局 trusted 开关；客户端不能确认时默认拒绝。

### 6.4 中高风险：权限模式会把多个防线一起打开

`safe` 默认禁网络样式命令、shell 展开和 inline script；`trusted` 打开这些能力；`dangerous` 关闭命令权限门和 Landlock。README 和安全文档都要求 dangerous 只在隔离容器/VM 中使用。[权限模式定义](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L149-L176)；[模式说明](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/SECURITY.md#L62-L66)

桌面 profile 默认却是 `trusted`；Dockerfile 也默认 trusted。对用户而言，“桌面默认值”很容易被理解成安全推荐，而不是兼容性选择。[桌面默认值](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/models.py#L34-L49)；[Docker 默认参数](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/Dockerfile#L34-L58)

**重写边界：** 不设 trusted/dangerous 全局模式，改为细粒度 capability；高风险 capability 只能为精确操作临时授予。

### 6.5 中风险：文件系统边界总体不错，但有平台和竞态余量

正面措施：路径层拒绝 NUL、绝对路径、`..` 和 symlink escape；写入时验证最近已存在父目录，且补丁拒绝直接写 symlink。[Workspace 路径实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L1083-L1152)

静态推断：unsafe root 集合只显式加入字符串 `/` 与用户 home；在 Windows 上 `C:\` 不等于 `/`，因此若操作者误把盘符根设为 workspace，这一“过宽根目录”检查可能不会拒绝。直接文件工具仍会被该 workspace 限制，但范围会变成整盘。[unsafe root 检查](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L1083-L1095)

另外，路径先 resolve/检查再使用仍存在 TOCTOU/symlink 竞态窗口；上游把对 anchored/no-follow 平台支持的依赖列为残余风险。[上游 residual risks](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/SECURITY.md#L96-L101)

**重写边界：** 明确拒绝 `Path.anchor`/所有 drive root、home、临时目录根和其他过宽根；危险范围必须 UI 二次确认。敏感文件操作使用目录句柄/`openat` 风格与 no-follow，Windows 还要测试 junction/reparse point。

### 6.6 中风险：遥测默认对外联网

运行时始终创建 `SessionTelemetry`。遥测模块默认向 PostHog batch endpoint 发送安装 ID、会话/产品/工具使用类事件，并提供环境变量 opt-out；这是服务器自身网络请求，不受 `exec_command` 的“网络命令”字符串策略控制。[运行时创建遥测](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L1338-L1360)；[遥测默认值与 endpoint](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/telemetry.py#L1-L92)；[事件发送](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/telemetry.py#L209-L234)

没有证据表明它发送文件内容，但“本地、safe、禁止命令联网”仍可能产生产品遥测，是隐私预期差异。

**重写边界：** 本地第一版完全不联网、不遥测。若未来确需统计，只做明确 opt-in、显示完整事件 schema、允许本地查看队列。

### 6.7 中高风险：安装与运行时供应链

quickstart 推荐从 `main` 直接 `curl .../install.sh | bash`；脚本还可直接下载 cloudflared、执行 Dev Tunnel 的 `curl | bash`、全局安装 ngrok。下载逻辑未见 checksum/signature 校验。[quickstart 安装命令](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/docs/quickstart.md#L1-L22)；[通用下载与 cloudflared 安装](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/scripts/install.sh#L200-L259)；[Dev Tunnel 安装](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/scripts/install.sh#L260-L272)

另一方面，正式发布工作流使用 PyPI trusted publishing 和 npm provenance，这是正面供应链措施。[发布工作流](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/.github/workflows/release.yml#L158-L245)

**重写边界：** 只从版本化发行物安装，固定版本、依赖 lock 和哈希；不执行分支头脚本，不在 npm wrapper 第一次运行时隐式解析 latest。发布物签名/attestation 要可由 `doctor` 校验。

### 6.8 中高风险：凭据在 argv、JSON 和剪贴板中流动

- 安装脚本接受 `--auth-token TOKEN`，把 token 放进服务器 argv，并在生成配置时把 bearer/OAuth 凭据输出到终端。[安装脚本认证参数](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/scripts/install.sh#L341-L425)
- 桌面 profile 以 JSON 保存；代码尝试 chmod 0600，但明确在 Windows 依赖用户目录 ACL。[桌面存储](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/storage.py#L12-L100)
- Cloudflare named tunnel token通过 `--token` 进入进程命令行，运行时还读取进程 cmdline 比对 token。[隧道进程构造与检测](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/runtime.py#L332-L364)；[cmdline 匹配](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/runtime.py#L766-L800)
- GUI 可把包含凭据的客户端配置复制到系统剪贴板。[桌面复制操作](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/apps/desktop-client/mcp_desktop_client/app.py#L697-L706)

**重写边界：** stdio-only 第一版根本不需要服务器秘密。未来若增加远程模式，使用 OS credential vault/受限句柄或 stdin，禁止 secrets 出现在 argv、普通 JSON、日志和默认剪贴板中，并落实 audience、issuer、HTTPS、PKCE 等 MCP 授权要求。

### 6.9 正面控制：值得借鉴但应重新实现

- 文件路径 canonicalization、相对路径限制和 symlink escape 检查是正确方向。[路径实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L1083-L1152)
- 补丁先 staging、基线冲突检测、同目录临时文件/备份与 best-effort rollback，能降低部分写入和覆盖并发变更的风险。[补丁实现](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/patching.py#L87-L170)
- 默认环境继承不是全量复制；代码过滤敏感名称/值、动态加载器和启动脚本变量，并把 HOME/TMP/cache 指向服务器运行目录。[环境构造](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L2624-L2719)
- HTTP 模式包含 loopback 默认、非 loopback 认证检查和 Origin 等防线；如果未来确实需要远程模式，应采用官方 SDK 的认证组件保留这些原则，而非从头复制。[HTTP 启动检查](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/coding_tools_mcp/server.py#L5603-L5671)

## 7. 推荐的独立重写安全边界

### 7.1 信任模型

分别建模四个主体，不要把“本机”当成同一信任级：

- **用户/UI：** 唯一能扩大权限与确认副作用的主体。
- **MCP 客户端/模型：** 可提出工具调用，但不能自行授予能力。
- **工作区内容：** 默认不可信，包括源码、测试、构建脚本、instructions 文档和二进制。
- **本地工具进程：** 只拥有一个 canonical workspace 的最小句柄；不得继承主机 secrets 或任意网络。

每个服务器进程只绑定一个工作区。配置中保存 canonical workspace ID 和包版本，不保存“允许一切”的永久位。

### 7.2 最小工具集

**里程碑 A：默认可安全启用**

| 工具 | 能力 | 必要约束 |
|---|---|---|
| `doctor` | 返回版本、工作区、有效能力、沙箱状态 | 不返回 secrets；绝对路径可按 UI 需要脱敏 |
| `read_text` | 按行/字节范围读取文本 | 相对路径、no-follow、单次/总量上限、二进制拒绝 |
| `find_files` | 列目录、glob、有限递归 | 最大深度/数量/耗时；不跟随 symlink/junction |
| `search_text` | literal 默认，regex 可选 | 文件/匹配/输出上限，regex 超时 |
| `preview_patch` | 解析补丁并返回结构化 diff/目标 hash | 不写盘；报告新增/修改/删除和冲突 |
| `commit_patch` | 提交已确认的 preview | 单次 grant、expected hashes、原子替换、失败回滚 |

**里程碑 B：验证外部沙箱后再启用**

| 工具 | 能力 | 必要约束 |
|---|---|---|
| `run_task` | test/lint/format/build 中的已配置任务 | 无 shell；固定 executable + argv；最小 env；默认实际断网；超时、输出 cap、进程树回收 |
| `command_control` | poll/input/cancel 持续任务 | opaque ID 绑定工作区/会话；TTL、并发限额；input 另行确认 |
| `review_changes` | Git status/diff | 只读、禁 external diff/textconv、输出上限 |

第一版不应包含任意 `exec_command`、Git 历史遍历、图片预览、HTTP、OAuth、隧道、云端 sandbox control、桌面常驻器或遥测。

### 7.3 一次变更的审批协议

1. 工具返回 canonical targets、expected content hashes、结构化 diff、删除/重命名标记和风险标签。
2. UI 以普通用户能理解的方式显示变更；MCP 官方要求人可见且可拒绝工具调用。[MCP 官方安全交互](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/server/tools.mdx#L34-L40)
3. 用户确认后，host 签发短时、单次、绑定 plan hash 和 workspace ID 的 grant。
4. commit 时重新校验路径、文件身份和 expected hashes；发生竞态即失败，不能自动扩大范围。
5. 记录本地审计条目：时间、操作摘要、目标、结果和 grant ID；不记录内容/secrets，不上传。

如果现有 MCP 客户端无法提供这种绑定式确认，最保守实现是把写入拆成用户显式运行的本地 companion 命令；不要让 MCP tool 自称“已获用户授权”。

### 7.4 执行隔离基线

- **Linux：** 外部 container/bubblewrap/nsjail 一类隔离，read-only toolchain、workspace 最小挂载、独立 tmp/home、seccomp/权限收缩和 network namespace。
- **Windows：** Job Object 只能处理生命周期，不能单独充当安全边界；需要低权限 helper 搭配 AppContainer/Windows Sandbox/容器/WSL 隔离，并测试 junction、reparse point 和进程树。
- **macOS：** 独立低权限账户、容器或 VM；若无法证明网络/文件隔离，就拒绝对不可信项目执行。

跨平台共同规则：不继承代理/云凭据、SSH agent、Git credential helper、包管理 token、动态 loader/startup env；所有子进程有 wall-clock timeout、输出上限和进程树终止。

## 8. 更便捷的用户流程

推荐把用户看到的完整流程压缩为：

1. 用户选择一个文件夹。
2. `init` 显示：“只读文件；补丁需确认；命令执行关闭；无网络；无遥测。”
3. 用户选择 MCP 客户端，工具生成一段固定版本、绝对 workspace 的 stdio 配置；经确认后写入或复制。
4. 客户端下次启动时自动拉起工具；`doctor` 在首个响应里显示绿色/黄色状态，而不是让用户自己排查端口、OAuth 和隧道。
5. 第一次写入弹出 diff 预览并单次确认；此后仍按操作确认，而不是要求用户重启到 trusted。
6. 只有用户主动开启“沙箱任务执行”且 `doctor` 实测隔离通过后，才出现 test/lint/format/build。

这个流程没有 daemon、端口和密码，失败点主要剩下“包不存在、workspace 不存在、客户端配置无效”三类，错误也更容易给出一键修复建议。

## 9. 实现与验收清单

### 协议与包装

- 使用固定版本官方 MCP SDK；做 stdio conformance 测试。
- stdout 永远只有协议帧，日志只写 stderr；关闭 stdin 后完成清理退出。[MCP 官方 stdio](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/basic/transports/stdio.mdx#L7-L16)
- schema 从类型生成，但仍做服务端长度、枚举、范围和互斥条件验证。
- 工具注解真实反映 read-only/destructive/idempotent；客户端不得仅凭注解跳过用户确认。[MCP annotations 信任要求](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/b0f60ba5409db7a6582440a7b473cc0398890f15/docs/specification/2026-07-28/server/tools.mdx#L300-L306)

### 文件系统

- 测试 absolute、`..`、NUL、UNC、drive-relative、drive root、home root、大小写折叠、8.3 路径、symlink、junction/reparse point。
- 测试 check/use 之间替换文件、父目录和链接；敏感操作必须 anchored/no-follow。
- 补丁测试新增/修改/删除/rename、基线冲突、跨目录失败、磁盘满和 rollback。
- 所有查询有最大字节、条目、深度、匹配数和耗时。

### 命令（若启用）

- 测试 argv 注入、任务配置注入、package scripts、编译器插件、Git hooks、external diff/textconv、env/credential 泄漏。
- “断网”必须用真实网络隔离测试 DNS/TCP/UDP/IPv4/IPv6/loopback，而不是检查字符串。
- 测试 timeout 后子孙进程全部回收、输出不会无限增长、command ID 不可跨工作区/会话复用。
- 无可用外部沙箱时，`doctor` 必须失败关闭执行能力。

### 提示与授权

- 仓库 instructions、工具输出、编译错误和 patch 内容全部视为不可信数据。
- 测试伪造“用户已同意”、诱导启用网络/读取外部文件、工具注解欺骗。
- 测试 grant 与 plan/workspace 绑定、过期、重放、内容变更和范围扩大。

### 供应链与隐私

- 发布版本固定依赖和哈希，生成 SBOM/attestation；`doctor` 可验证。
- 无 `curl | bash`、无首次运行静默更新、无默认遥测。
- 第一版没有 secrets；未来 secrets 不进 argv、普通 JSON、日志或默认剪贴板。

## 10. 可参考与不可照搬的分界

可以参考的是设计思想：workspace canonicalization、补丁 staging/基线校验/回滚、输出与进程配额、最小环境，以及把残余风险写进用户可见的安全文档。复制具体代码前还需遵守上游 Apache-2.0 许可和 NOTICE/署名义务；“参考原理、独立实现”也应保留自己的设计记录与测试证据。[上游许可证](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/LICENSE)；[上游 NOTICE](https://github.com/xyTom/coding-tools-mcp/blob/ed85e41999b0cf840d6e45f2bed11ac7f52eab3f/NOTICE)

不应照搬的是把 arbitrary shell 暴露为基础工具、用正则策略命名“安全执行”、自动注入仓库 instructions、全局 trusted/dangerous 开关、默认联网遥测、分支头 `curl | bash`、凭据经过 argv/JSON/剪贴板，以及为本地流程同时承担 HTTP/OAuth/隧道/云控制面。

最终建议的交付顺序是：

1. stdio + 官方 SDK + `doctor/read_text/find_files/search_text`；
2. `preview_patch/commit_patch` + 绑定式一次性确认；
3. 客户端配置生成器与打包/校验；
4. 只有在目标平台的外部沙箱测试通过后，才加入 `run_task`；
5. 远程 HTTP 若有真实需求，作为独立产品边界另行威胁建模，而不是默认能力。
