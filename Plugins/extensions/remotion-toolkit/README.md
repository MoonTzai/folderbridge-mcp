# Remotion Toolkit v0.1.2 — FolderBridge 外源插件

## 定位

本插件为已注册的 FolderBridge 工作区提供经过边界约束的本地 Remotion SSR/渲染调用。它不安装依赖，不修改业务源，也不自动启动 Studio、开放公网端口或发布作品。

插件源码位于仓库相对目录 Plugins/extensions/remotion-toolkit。安装时从该目录运行 install.ps1；更新后在 FolderBridge Extensions & Skills 中 Rescan，核对新 SHA 并手动 Approve/Enable。已批准的旧版本不自动继承新目录权限。

## 支持的操作

- status：插件与 Node 状态
- verification-plan：阶段性验证与 QA 建议
- project-inspect：项目入口、清单与资源 SHA
- preflight：Node、Remotion、React、浏览器的真实可用性检查
- compositions：读取组合、Input Props 与 calculateMetadata
- bundle：在项目固定入口执行 Webpack 打包
- render-still / render-stills：单张或批量静帧
- render-frames：指定帧或闭合区间帧序列
- render-video：支持用户声明且插件明确允许的视频／音频编码选项
- media-probe / silence-scan：读取源媒体信息和静音区间
- studio-guide：官方 Remotion Studio WebMCP 和 Agent Skills 指南

实际动作清单、参数、限制和生命周期以 folderbridge-extension.json 的准确 schema 为准，不要从这份说明推断不存在的接口。

## 工作区与依赖

project_path、entry、public_dir 和 shared_public_dir 均相对于用户已经注册且选择的 FolderBridge 工作区，不能填写另一工作区或任意机器的绝对路径。项目由 remotion-toolkit.project.json 明确声明依赖版本、源码与媒体的 SHA-256、允许操作集合。插件只使用这些经过审核的相对路径；不读取或上传个人会话、私人素材库、系统凭据或项目之外的路径。

0.1.2 支持共享依赖提供者 shared_dependency_provider.project_path，例如 Remotion-Shared/remotion-4.0.539。共享提供者与调用项目必须位于同一个已注册工作区，并通过精确 Remotion/React 版本、package/lock SHA、项目允许范围的校验。allow_engine_inherited_dependencies 和 expected_react_version 是显式兼容选项，不能绕过版本一致性。

如果多项目复用只读媒体目录，可使用 shared_public_dir 配合 shared_public_sha256_map，而不是拷贝、移动或重新命名受保护的业务媒体。旧项目的本地安装依赖只有在全部已授权入口真正通过打包/渲染验证后才可以单独安排清理；插件本身不会擅自卸载。

## 安全与验收

写操作走 FolderBridge 受管 Job，输出放在调用者声明的工作区范围；使用 job_status / job_cancel 处理运行状态。插件不接受任意 shell、进程号、跨工作区绝对路径或直接 API 密钥。Node 和业务项目代码以当前操作系统账户权限运行，FolderBridge 不构成 OS 沙箱。

部署前先执行插件测试和 Node 语法检查，安装后验证 extension(status)、project-inspect、preflight、compositions、真实静帧及视频输出。真实视频内容、字幕节奏、音轨、色彩与语义验收应独立于技术构建收据，不得将单次历史测试视为新工程 PASS。

Remotion 官方文档：https://www.remotion.dev/docs/ai/skills 及 https://www.remotion.dev/docs/ai/webmcp
