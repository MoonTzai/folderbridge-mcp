# TestOnline 0.1.2 — FolderBridge 外源局域网发布插件

**状态：纯源码外源插件 + Launcher 托管服务，需使用包含 TestOnlineServiceController 的 FolderBridge 版本。**

本插件负责全部 HTTP/HTTPS 托管逻辑。FolderBridge 只检查插件 SHA 批准状态，使用 **自身可执行文件的独立子进程** 加载已批准的外源 `server.py`，退出时仅清理自己拥有的子进程。**插件不包含 `TestOnline-Host.exe`，也无需用户额外启动其他程序。**更新插件只需安装新版插件并重新批准哈希，不必重建 FolderBridge。

## 目录与地址

发布根目录（自动创建）：`<FolderBridge安装目录>\TestOnline`

创建子文件夹：`<端口号>-<发布模式>`，支持：
- `8765-http` → `http://<LAN_IP>:8765/`
- `8767-https` → `https://<LAN_IP>:8767/`

文件夹里的 `index.html` 是首页；若没有 `index.html`，访问首页会显示只包含该目录 HTML 文件的链接列表。多份 HTML 可通过 `/文件名.html` 分别打开，CSS、JS、图片等静态资源按原相对路径提供。隐藏文件、目录穿越、符号链接/Junction 和不支持的文件类型不对外开放。

**站点目录只在 TestOnline 服务启动时扫描一次，或用户在 TestOnline 的局域网主页点击「刷新站点」时手动重扫。**主页由外源插件提供，在 FolderBridge 插件栏点击已有的「打开状态页」即可打开，无需新增 FB 专用按钮。仅在发布电脑本机访问主页时显示刷新按钮，其他局域网设备只能访问站点和证书。新增、删除或重命名 `<port>-http|https` 文件夹后需点击该按钮，才会启动或停止相应监听端口；不再每秒轮询目录。**替换或覆盖 HTML 文件不需要扫描或重启**：服务器每次请求都从磁盘重新读取，统一设置 `Cache-Control: no-store`，刷新浏览器即可看到最新版本。

端口必须是 1024–65535 且避开 18888/18889 服务保留端口；同一个端口不允许重复。端口被其他服务占用时显示错误，不会终止陌生进程。IP 由 Launcher 在启动时从本机 RFC1918 私有网卡自动选择；如有多个可用网卡，可以在本机环境变量 FOLDERBRIDGE_TESTONLINE_BIND_IP 中指定私有 IPv4。若没有可用私网地址则仅监听 127.0.0.1，不声称手机可访问。

局域网门户：`http://<LAN_IP>:18888/`，列出在线站点和不可用原因。

## HTTPS 与 iOS

第一个 HTTPS 站点启动时，插件使用 **Git for Windows 自带的 OpenSSL** 创建只用于本机测试的 CA 和包含 `<LAN_IP>` IP-SAN 的服务器证书。服务私钥保存在 FolderBridge 插件受管状态目录的 `certificates/`，**不放入 TestOnline 文件夹，不向局域网客户端发送**。

可以从局域网门户或下面的短地址下载**公开根证书**（URL 为 `/CA`，实际下载文件名仍为 `testonline-root.cer`，不包含私钥）：

`http://<LAN_IP>:18888/CA`

用户需要在自己的 iPhone/iPad 安装公开证书，在「设置 → 通用 → 关于本机 → 证书信任设置」中显式开启完全信任，之后 Safari 才会把 `https://<LAN_IP>:<端口>/` 视作可信安全上下文。证书信任相当于授权本地测试 CA 签发的证书，应仅安装在自己控制的设备上，测试结束后可以撤销信任并删除证书。**公开根证书不包含私钥。**

HTTPS 源于用户对 iOS Web Share API 的测试需求。即使处于可信 HTTPS，Safari 也不能自动替用户选择分享面板中的「存储到‘文件’」，只能由用户主动选择。若浏览器不支持 Web Share，应按站点自身逻辑降级到下载。

## 安装

1. 使用 FolderBridge 的独立测试版（主程序里已登记 TestOnline 的托管服务控制器）。
2. 在此目录打包或使用完整插件 ZIP，只包含源码、manifest、文档和安装脚本；无独立 EXE。
3. 可以用 FolderBridge 图形界面的「安装/更新外源 Extension ZIP」，或者运行 `install.ps1` 安装到 `%LOCALAPPDATA%\folderbridge-mcp\extensions\testonline-host`。
4. 在 FolderBridge「Extensions & Skills」中重新扫描，**审核并批准当前 SHA+权限后启用插件**。这是一次必要的用户授权操作；不会绕过 FolderBridge 外源插件信任机制。
5. Launcher 自动启动 TestOnline，默认「自动启动」为开启。之后每次启动 FolderBridge 都会自动托管 TestOnline，无需单独操作。FolderBridge 侧栏只管理通用的启动、停止、重启及「打开状态页」；「刷新站点」位于插件自己提供的站点主页。

本次改造需要由用户在验证通过后升级一次 FolderBridge 主程序以获得新的托管子进程入口；外源插件后续可独立升级。不修改每个工作区的 `.folderbridge.json`，也不向公网发布。原有 ComfyUI/其他插件和 Coach/录音笔工程均不受影响。

## 插件动作

- `status`：读取 `127.0.0.1:18889/status` 上的服务状态；**不会启动服务**。
- `sites`：只列出最近一次扫描识别出的站点、URL 和有界错误信息；同样不会启动服务。
- `刷新站点`：外源插件主页在本机显示按钮，按钮向同源地址发送一次 `POST /rescan`；仅允许发布电脑本机请求并核对 Origin 与专用请求头，局域网其他设备及跨站网页不能触发。loopback 控制端点保持兼容。插件的只读 MCP 动作不变。

FolderBridge Launcher 是唯一受管进程启动/停止入口；插件 `serve(root, bind_ip, state_dir)` 是稳定服务入口。外源插件 manifest 的 `status`/`sites` 动作仅声明固定 loopback 权限，局域网监听由用户批准的 Launcher 托管控制器启动，不提供任意 shell、端口控制、远端写文件或任意工作区访问。

## 维护边界

发布服务、TLS 证书和外源查询接口全部保留在 `Plugins/extensions/testonline-host/`；FolderBridge 核心仅有 `testonline_service.py`、`managed_services.py` 的注册和一个私有 CLI 子进程入口。配置仅记录 `auto_start`，不记录任意 shell 命令。插件升级后需重新批准插件 SHA 并重启服务；旧运行中的 FolderBridge.exe 不能动态获得新的托管子进程入口。
