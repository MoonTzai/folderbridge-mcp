# ChatGPT Web LLM Test Adapter

通用、**仅用于测试**的 Browser-backed OpenAI-compatible Provider。**0.2.0 起以独立本地 Standalone Launcher 为主入口**，不要求 ChatGPT 对话再去控制 FolderBridge Extension；FolderBridge Extension 模式继续保留为可选入口。每个 completion 请求都会送到一个全新的 ChatGPT 网页对话中。

它不是《认知星图》专用插件，也不依赖 Debate-Judge。任何能够配置自定义 OpenAI-compatible Base URL 的本地 HTML / App 都可以复用，包括：

- 《认知星图 / Cognitive Landscape》schema4 低样本真实语义 probe；
- Debate-Judge HTML 的真实网页模型实测；
- 其它本地 OpenAI-compatible 客户端的人工账号授权语义验收。

## 为什么每个请求都必须新开对话

本适配器强制 **fresh chat per request**。它不复用上一请求的 ChatGPT 会话上下文。

这对认知星图尤其重要：不同独立采样不能互相读取前一份样本。对 Judge 也同样重要：不同 testcase / 实测不能因为浏览器历史对话而串上下文。

并发请求会各自创建独立 ChatGPT 标签页；`max_parallel` 只限制同时工作的网页会话数，超过上限的请求排队，不会把不同请求合并到同一聊天中。**0.3.2 默认 2 路，硬上限 4 路**；Standalone 状态页可用滑块在 1–4 路之间即时调整。但并发并不是唯一保护：0.3.2 新增 fresh-chat 历史安全节流，默认相邻新会话至少间隔 20 秒、5 分钟最多启动 8 个；若网页检测到“请求过于频繁 / 暂时限制访问对话记录”等历史限流提示，则进入 120/240/480/600 秒指数冷却。登录/验证操作优先复用已有 ChatGPT 页，不再无意义创建额外标签页。

## 接口

Standalone 0.3.2 固定 Base URL：

```text
http://127.0.0.1:8769/v1
```

Standalone 专用 DevTools 固定为 `127.0.0.1:8770`，因此可以与可选 FolderBridge Extension 模式的 `8767/8768` 并存。Extension 模式仍使用 `http://127.0.0.1:8767/v1`。

固定测试模型 ID：

```text
chatgpt-web-current
```

支持：

```text
GET  /v1/models
POST /v1/chat/completions
```

`stream:true` 返回 OpenAI 风格 SSE，但由于网页端必须等一轮回答稳定后才能可靠抽取，当前是“回答完成后一次性发送 content delta”，**不是 token-live streaming**。

`response_format={"type":"json_object"}` 会要求网页 ChatGPT 只返回一个 JSON object。0.3.2 仍保持严格结构校验，但会先从 assistant 消息副本中剔除网页按钮/控制件，再规范化**非语义显示包装**：例如 Markdown ` ```json ` 围栏，或网页代码块的 `Copy code / 复制代码` 标签；这些标签即使被网页布局拼在同一行也不会污染 JSON。规范化后仍必须恰好得到一个 JSON object。解释性文字、第二个对象、数组或损坏 JSON 仍返回协议错误，不改写模型的字段或语义内容。

## system role 的重要限制

网页 ChatGPT 没有公开的 API system-role 注入接口。因此适配器会把调用方的 system/user/assistant messages 封装成一个结构化的网页提示词，再发送到新对话。

因此：

```text
system_semantics = prompt_wrapped
```

它适合做“真实 ChatGPT 网页模型的语义质量”验收，但**不能**用来证明 OpenAI 官方 API 的 system role、token 参数、CORS、SSE、finish_reason 或其它协议行为与网页端等价。

服务响应会带：

```text
X-ChatGPT-Web-Test-Adapter: prompt-wrapped-system; fresh-chat-per-request
```

completion 的 `system_fingerprint` 也固定标记为：

```text
chatgpt-web-test-prompt-wrapped
```

## 推荐启动流程｜FolderBridge Launcher-owned Standalone 0.3.2

首次安装或源码更新后，只需做一次 Extension 更新与 exact-hash 审批；日常使用不再要求手工运行 CMD：

1. 在 **FolderBridge → Extensions & Skills** 点“安装/更新插件 ZIP…”，选择正式 `FolderBridge-Plugin-chatgpt-web-llm-adapter-v0.3.2.zip`。Launcher 会 staged 校验并安装/更新，但不会自动批准新代码；随后在同一侧栏核对当前版本、权限与 exact SHA-256，再明确批准并启用。
2. 在同一张 Adapter 卡片直接查看 `OFFLINE / STARTING / WAITING_LOGIN / READY / ERROR`，并看到 **installed version / live version / exact hash**。Launcher 只启动当前用户 Extension 目录中已按当前 hash 批准且启用的那一份代码。
3. 点“启动”。Launcher 只持有它自己创建的 Standalone `Popen` ownership；服务在 `127.0.0.1:8769`，专用 Chrome / Edge DevTools 在 `127.0.0.1:8770`。若任一端口被未知进程占用，启动会 fail-closed，**绝不会按端口发现 PID 后强杀陌生进程**。
4. 启动完成后本地 `status.html` 自动打开。首次使用时可在 sidebar 或 status 页点“登录 / 验证页”，使用 Standalone 自己的持久 profile 完成 ChatGPT 登录 / 安全检查。profile 保存在 `%LOCALAPPDATA%\ChatGPT-Web-LLM-Test-Adapter\standalone-v1`。
5. 状态进入 `READY` 后，直接点“验证调用”。Launcher 会使用本次临时 Key 发出固定的真实 `/v1/chat/completions` JSON probe；看到 `LIVE_PROBE_PASS` 才代表网页 transport 的真实 completion E2E 已通过。验证结果不携带临时 Key，也不把 Key 写进普通日志。
6. sidebar 直接显示并可复制 Base URL、Model Name 与**本次启动随机生成**的临时 API Key；Key 只用于本机人类控制面与客户端连接，Extension `status/connection` 不向 MCP 返回该 secret。
7. sidebar 与 status 页都可在 **1–4 路**之间调整网页并发，默认 2。降低并发只限制后续进入的请求，不强制终止已经运行中的 completion；超限请求继续排队。
8. 点“重启”时，Launcher 先优雅停止自己 owned 的旧 service/browser，确认退出与端口释放，再启动当前已批准版本；新进程会生成新的临时 API Key并重新打开状态页。若检测到的是 external instance，则拒绝终止它。
9. 点“停止”只清理当前 FolderBridge run 自己持有的 process tree。若停止后 8769/8770 仍被其它进程占用，只报告 warning，不会越权清理。

项目根目录的四个 CMD 仍保留作为 recovery / debug fallback，而不是日常主入口；日常真实验证已经由 sidebar 的“验证调用”覆盖，fallback 的“验证”CMD 保留同一固定 JSON probe 语义：

```text
启动-ChatGPT-Web-LLM-Standalone.cmd
验证-ChatGPT-Web-LLM-Standalone.cmd
状态-ChatGPT-Web-LLM-Standalone.cmd
停止-ChatGPT-Web-LLM-Standalone.cmd
```

Standalone 与可选 Extension `serve` 使用不同端口和不同 profile，可并存；日常 HTML 客户端应连接 Launcher-owned Standalone 的 `8769/v1`。

## 可选启动流程｜FolderBridge Extension

如宿主允许控制该 Extension，仍可运行 `install.ps1`，Rescan 后按 exact hash 批准并启用，再调用 `serve`。Extension 模式继续使用 `8767/8768` 和 FolderBridge extension state。某些 ChatGPT 产品控制面可能阻止对“网页 ChatGPT 适配器”Extension action 的调用；这正是 Standalone 成为主入口的原因，而不是本地服务或浏览器自动化故障。

## 安全边界

- Standalone 只监听 `127.0.0.1:8769`，其专用浏览器 DevTools 只监听 `127.0.0.1:8770`；可选 Extension 模式分别使用 `8767/8768`。所有端口都只绑定 loopback，不监听 LAN / WAN。
- `/v1/*` 必须提供本次启动生成的 Bearer token；DevTools 端口不作为客户端 API 暴露。
- 浏览器 CORS 只允许：
  - `Origin: null`（`file://` 单 HTML）；
  - `localhost`；
  - `127.0.0.1`。
- 不允许调用方传 URL、浏览器可执行文件、profile 路径、shell 命令或网页选择器。
- 浏览器只打开固定 `https://chatgpt.com/`。
- 每个 completion 请求最多 96 条 string message；总消息字符数有硬上限。
- 网页并发硬上限 4 路，默认 2 路；超过当前上限的 completion 在本机排队，不额外创建活跃 ChatGPT 会话。
- fresh chat 启动另有独立节流：默认最小 20 秒间隔，滚动 5 分钟最多 8 次。这个节流作用于真正的新 ChatGPT 页面创建，因此认知星图自己的 fallback、归并和解释映射也无法绕过。
- 如果页面出现已知的历史访问限流提示，Adapter 会记录命中并指数冷却 2/4/8/10 分钟；`/health` 暴露窗口计数、下一次可启动时间、剩余冷却和命中次数。
- “打开登录 / 验证页”会优先复用现有 ChatGPT 页面；只有完全没有页面时才创建新页，而且同样受 fresh-chat 节流。
- 当前 0.3.2 **没有宣称自动启用 Temporary Chat**。普通 fresh chat 仍可能进入聊天历史；Temporary Chat 若后续自动化，必须先可靠识别网页控件并验证已开启，不能静默假装成功。
- Adapter 不把消息正文、Bearer token 或 ChatGPT 登录凭据写进普通日志。
- Standalone profile 与连接记录保存在 `%LOCALAPPDATA%\ChatGPT-Web-LLM-Test-Adapter\standalone-v1`；可选 Extension 模式继续使用 FolderBridge `extension.state`。
- 0.3.2 日常由 FolderBridge Launcher 按当前 `Popen` handle 管理 Standalone 生命周期；ownership 不持久化，不从端口反查 PID。未知/外部实例只报告，不会被停止。项目 CMD 仅保留 recovery fallback；可选 Extension 的 `serve` 仍是 host-owned Job。

## 参数语义

为了兼容常见 OpenAI-compatible 客户端，以下字段可被接收：

```text
max_tokens / max_completion_tokens
reasoning_effort / thinking
temperature / top_p
presence_penalty / frequency_penalty
seed / stop
```

这些参数在网页 ChatGPT 上**不能保证获得 API 等价执行**；它们不会被伪装成已经生效。真实可保证的是：消息内容被包装发送、每请求 fresh chat、JSON mode 的最终结构校验、响应抽取和 OpenAI-compatible transport envelope。

因此需要验证正式供应商 API 参数、鉴权、截断、SSE/CORS 时，仍应继续使用对应供应商 API 或 mock/contract tests。

## 《认知星图》接入

使用“自定义 OpenAI 兼容端点”：

```text
API 地址: http://127.0.0.1:8769/v1
模型 ID: chatgpt-web-current
API Key: Standalone 状态页显示的本次临时 token
```

schema4 / `typed-ideas-v2` 的 S1 / S2 / S3 / S4 仍由认知星图自己拆成独立请求；Adapter 不合并这些请求，但所有采样、归并、价值关联和 fallback 都必须经过 Adapter 的统一 fresh-chat 节流。

## Debate-Judge HTML 接入

Judge 只要其 provider 层支持 OpenAI-compatible Base URL，就使用同一组三项连接参数。Adapter 不理解裁判流程，也不读取 Judge 项目文件；它只处理收到的 `messages`。

如果 Judge 某条 pipeline 依赖供应商特有参数或真正的 API system-role 行为，应把 Web Adapter 的结果标记为 `browser semantic acceptance`，不要混入正式 API compatibility verdict。
