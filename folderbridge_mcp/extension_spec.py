from __future__ import annotations


EXTENSION_FORMAT_SUMMARY = """FolderBridge Extension ABI v1

目录结构：
<extension-id>/
  folderbridge-extension.json
  plugin.py
  （可选）其它由 plugin.py 导入的同目录文件

核心规则：
1. manifest 的 schema_version 固定为 1；id 使用小写字母/数字/._-。
2. entrypoint 必须是插件目录内的 .py 文件，并实现 handle(action, params, context) -> dict。
3. 插件不会作为新的 MCP tool 暴露；统一通过 FolderBridge 的 extension(list/info/run) 调用，所以安装新插件不需要改变 Connector 工具目录。
4. 外部插件必须按“完整插件目录 hash + permissions”本机批准；任一文件或权限变化都会使批准失效。
5. v1 插件在独立子进程运行，使用清理环境、固定超时、有界 stdin/stdout/stderr。它隔离崩溃和协议污染，但不是完整 OS 沙箱；不可信插件请放 VM/容器。
6. 不允许声明任意 shell / 任意公网网络。权限必须是 FolderBridge 认识的精确权限，例如 workspace.read、workspace.write、workspace.adapter、extension.state、network.loopback:127.0.0.1:8188、process.execute:ffmpeg、git.commit-selected-files、git.push-current-branch、github.web-auth。
7. 不要靠安装时修改每个工作区的 .folderbridge.json。项目适配必须优先使用 workspace_adapter.mode=dynamic 和 detect.any_of/all_of；FolderBridge 会在每次调用时重新检测，因此项目后来新增脚本也能自动适配。
8. 插件持久状态优先使用 context.state_dir（FolderBridge 用户配置目录），不要污染仓库。确实需要写工作区时必须声明 workspace.write，并由 action 参数明确触发。
9. action 的 input_schema 使用受限 JSON Schema 子集：type/properties/required/additionalProperties/items/enum/minimum/maximum/minLength/maxLength/minItems/maxItems/description/default。
10. action.authorization=global 表示侧栏一次批准并启用后全局可用；authorization=none 只允许 bundled 插件的只读状态/发现动作。
11. ABI v1 的 plugin.py 应只依赖 FolderBridge 已打包模块与 Python 标准库；不要假设用户能在单文件 EXE 内 pip 安装第三方包。额外软件能力优先通过精确的本地 HTTP API 或 process.execute:<程序名> 调用。

manifest 示例：
{
  "schema_version": 1,
  "id": "example-tool",
  "name": "Example Tool",
  "version": "1.0.0",
  "description": "Example FolderBridge extension",
  "entrypoint": "plugin.py",
  "permissions": ["workspace.read", "extension.state"],
  "execution": {"mode": "isolated-process", "timeout_seconds": 180},
  "workspace_adapter": {
    "mode": "dynamic",
    "state": "profile",
    "detect": {"any_of": ["pyproject.toml", "package.json"], "all_of": []}
  },
  "actions": {
    "status": {
      "read_only": true,
      "requires_workspace": false,
      "authorization": "global",
      "input_schema": {"type": "object", "properties": {}, "additionalProperties": false}
    }
  }
}

plugin.py 最小接口：
def handle(action, params, context):
    # context: extension_id, extension_version, permissions,
    # workspace_root, workspace_read_only, state_dir, workspace_adapter
    if action == "status":
        return {"ready": True}
    raise RuntimeError("unsupported action")
"""


EXTENSION_LLM_PROMPT = """你正在为 FolderBridge MCP 编写 Extension。请严格遵守 FolderBridge Extension ABI v1，并把最终结果做成一个可直接放入 extensions 目录的完整插件文件夹。

工作流程要求：
1. 先判断我当前提供的信息是否足够。不要猜测目标软件的私有 API、CLI 参数、工作流格式、文件布局或认证方式。
2. 如果资料不足，先明确列出你真正需要我提供的最少资料，并主动要求我上传/提供。例如：
   - 目标软件的 API/CLI 文档或 --help 输出；
   - 我现有的脚本、配置、workflow JSON、示例请求/响应；
   - 一个能代表真实结构的项目文件树或最小样例项目；
   - 若需解析专有文件，要求上传一个非敏感样例文件；
   - 若需调用本地服务，询问并确认固定 loopback host/port 和 API 路径。
   优先让用户上传文件；不要要求用户手工粘贴大型二进制或超长文件内容。
3. 如果资料已经足够，直接生成插件，不要反复确认显而易见的信息。
4. 输出至少包含：folderbridge-extension.json、plugin.py、针对 manifest/动作/错误边界的单元测试，以及简短 README。
5. manifest.schema_version 必须是 1。entrypoint 必须位于插件目录内并定义 handle(action, params, context) -> dict。
6. 只能使用 FolderBridge 已允许的精确权限。禁止任意 shell、任意公网网络、通配权限或隐藏副作用。需要本地 HTTP 时声明具体 network.loopback:127.0.0.1:<port>；需要外部程序时声明具体 process.execute:<basename>。
7. 不要在安装插件时修改或生成每个工作区的 .folderbridge.json task。若插件需要识别项目能力，使用 workspace_adapter.mode=dynamic + detect.any_of/all_of；FolderBridge 会在调用时动态重新检测。若需要持久状态，优先使用 context.state_dir，而不是往仓库塞内部状态。
8. 插件代码在独立子进程运行，但这不是完整 OS 沙箱。不要声称权限声明能阻止恶意 Python 绕过；对不可信代码应建议 VM/容器。
9. action.input_schema 要尽量精确，拒绝未知参数；对路径使用 workspace-relative 参数语义，不接受任意绝对路径，除非 FolderBridge ABI 未来明确提供这种权限。
10. action.authorization 默认使用 global。只有随 FolderBridge 打包的 bundled 插件，且动作真正只读/无危险副作用时，才可以建议 authorization=none。
11. 若某能力会执行工作区代码（构建、打包、脚本），在 README 和 manifest 权限说明里明确写出风险，并使用动态 workspace adapter，而不是静默预置任务。
12. ABI v1 不要新增 Python 第三方依赖。若能力依赖 FFmpeg、Blender、ADB 等外部程序，声明精确 process.execute:<basename> 并在 README 说明用户需要安装的软件；若依赖本地服务，使用固定 loopback 权限。
13. 完成后做一次自检：manifest 可解析、权限无未知值、入口文件存在、所有动作 schema 与 handle 分支一致、无路径穿越、无任意 URL/任意 command 参数、超时和输出有上限。

FolderBridge Extension ABI v1 速查：

""" + EXTENSION_FORMAT_SUMMARY
