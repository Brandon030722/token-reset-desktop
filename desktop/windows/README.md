# Windows 原生观察站

此版本用 .NET 8 WPF 窗口承载系统 WebView2，网页与窗口一起打开。不会启动 Tk、外部浏览器主界面或本机 HTTP 服务。只有用户点击 HTTPS 原帖、订阅链接时才交给系统浏览器。

**当前在 macOS 编写，尚未在 Windows 编译、运行或测量体积与内存。** 不把源码和构建脚本当作已经验证的 Windows 安装包。

## 构建

在 Windows 开发机预先安装 Node.js 24、匹配目标架构的 Python 3.12、.NET 8 SDK。脚本只在项目 build 目录创建 Python 虚拟环境，不安装系统级依赖。

~~~powershell
.\scripts\build_windows.ps1
# 已有前端 dist 时：
.\scripts\build_windows.ps1 -SkipFrontend
# 用户电脑没有 .NET Desktop Runtime 时可生成更大的独立版本：
.\scripts\build_windows.ps1 -SelfContained
~~~

默认产物 artifacts/TiboMonitor-win-x64.zip，解压整个目录并运行 TiboMonitor.exe。默认依赖用户已安装的 **.NET 8 Desktop Runtime** 和 **Evergreen WebView2 Runtime**，不随包重复分发 Chromium。缺少 WebView2 时显示微软官方下载入口；程序不自动安装运行时。Windows ARM64 需要匹配架构 Python，用 -Runtime win-arm64 构建。

图标从 desktop/assets/AppIcon.ico 编译进 EXE；图标未生成时允许开发构建。helper 用 PyInstaller onedir 打包，不带 Tk；不用每次检查都解压运行时的 onefile 形式。默认 .NET 发布不含 .NET 运行时，适合先考虑包体积；SelfContained 增大体积但减少一个安装前提。

## 本机状态与邮件

状态位于 %USERPROFILE%\.tibo-reset：

- monitor.config.json：来源和监控/邮件配置，只由 Python helper 读取。
- state.sqlite3：去重记录。
- data/snapshot.json、data/health.json：网页可读取的公开状态。
- web.config.json：网页只接收其中经过校验的 subscriptionUrl 字段。
- webview2：系统 WebView 的缓存和 localStorage。

启动后检查一次，以后约每 15 分钟运行 helper。helper 运行完退出；最长运行 90 秒。单实例和进程锁避免重复执行；关闭窗口会停止定时并结束当前 helper。休眠期间不能保证准时检查。最小化时尝试挂起 WebView，恢复时推送最新结果。保留 WebView 本身所需的系统进程，不承诺固定内存数值。

邮件默认关闭。需要管理员在本机 monitor.config.json 明确设置 sendEmail=true，并提供 API key、专用订阅组、已验证发件地址及 optInConfirmed=true，才可能由已有监控引擎提交。桌面壳不会继承 TIBO_SEND_EMAIL 环境开关，不会在 UI 自动启用发送。测试启动可使用：

~~~powershell
.\TiboMonitor.exe --dry-run
~~~

dry-run 仍采集与保存本机状态，但不发信。不要为测试清空正式 state.sqlite3。

## 前端桥接接口

应用在本机主文档加载前注入：

~~~javascript
window.__TIBO_DESKTOP__ = { platform: 'windows' };
window.chrome.webview.postMessage({ type: 'monitor.check' });
window.addEventListener('tibo:monitor', event => {
  // event.detail.status: running | ok | cooldown | source-unavailable | failed
  // 其余字段与 monitor CLI 的 JSON 结果相同。
});
~~~

资源来自 https://tibo.invalid/index.html 的本机响应；该域名不提供网络服务。沿用网页相对路径：

~~~text
/assets/...
/favicon.svg
/data/snapshot.json
/data/health.json
/config.json  -> 仅 {"subscriptionUrl":"https://..."}
~~~

WebResourceRequested 只提供前端资源白名单和两个公开 JSON 文件，拒绝其他资源与远程请求。桥接仅处理本机主文档发出的 monitor.check，没有文件读取、任意命令执行或密钥获取接口。远程主导航、子框架、权限申请和下载均被禁止；用户主动打开的外部 HTTPS 链接由系统处理。

## Windows 验证清单

1. 无 Python/Tk 开发环境时启动，主 UI 在应用窗口内显示；任务管理器没有常驻 Python helper。
2. 首次检查和 Ctrl+R 返回状态；15 分钟内重复点击不重复请求来源。
3. 网络失效时保留历史，显示来源不可用，不产生邮件。
4. 最小化/恢复后 UI 正常，关闭窗口结束子进程。
5. 原帖与订阅 HTTPS 在外部打开；file/http/自定义协议及远程嵌入页不能取得桥接。
6. 删除 WebView2 的测试环境显示运行时缺失说明；默认小包缺少 .NET 8 Desktop Runtime 时，系统会提示安装它。
7. 使用 --dry-run 测试采集，正式邮件需另行用授权测试邮箱验证。

API 依据：[本地内容与安全资源响应](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/working-with-local-content)、[WebView2 安全建议](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security)、[运行时分发](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。这些文档确认系统共享运行时、资源响应和来源校验的用法，不代替本项目的 Windows 实机测试。
