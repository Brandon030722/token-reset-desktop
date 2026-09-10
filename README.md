# Token重置 · Desktop

**当前版本：0.1.0。** 本仓库是 Token重置本地应用的独立版本线，由原项目 0.8.1 拆出；从 v0.1.0 重新开始发布版本，不代表撤回已有功能。

[下载 macOS Apple Silicon 版](https://github.com/Brandon030722/token-reset-desktop/releases/tag/v0.1.0) · [云端监控与网站](https://github.com/Brandon030722/token-reset)

## 本地应用

- macOS 菜单栏黑底白字 T!，点击打开紧凑面板，关闭面板继续监控；WebView 按需创建和释放。
- 每 15 分钟尝试检查 Tibo 公开动态；系统通知区分广泛重置和小范围公告。
- 读取本机 Codex 返回的实际七天额度窗口，不假设存在五小时窗口。
- 可将已读取的未来恢复时间同步至自己的云端仓库，由云端到点向本人邮箱发送提醒，关机也可执行。
- 保留邀请码订阅入口、通知设置、预约开关和黑白静态图标。

macOS arm64 构建已经本机验证。Windows WPF/WebView2 源码及手动构建工作流保留，尚未完成 Windows 实机验证，也不具备与 macOS 相同的菜单栏、个人周提醒集成；本次发布不提供已验证的 Windows 安装包。

## 安装与升级

解压完整的 `Token重置.app`，放入用户的 `~/Applications` 后打开。点击屏幕顶部 T! 查看面板，也可使用 Spotlight 搜索“Token重置”。应用采用本机 ad-hoc 签名，未经过 Apple 公证。

从原版本升级时，先退出旧菜单栏进程，再替换应用；不要同时运行两份。保留原 bundle identifier 和 `~/.tibo-reset` 状态目录，已有配置、通知去重记录、邀请码状态与云端预约继续沿用。显示版本改为 0.1.0，内部构建号继续递增，避免重用旧构建号。

## 云端与本地的边界

本仓库只构建本地软件，**不包含定时采集、发信和 Pages 部署工作流**，也不自动创建或迁移 GitHub Secrets。现有云端仓库 `Brandon030722/token-reset` 继续运行。不要因为本地代码换仓库，就把私有配置里的 `weeklyCloud.repository` 改成本仓库。

个人预约通过本机已登录的 GitHub CLI 写入用户自己管理的云端仓库 Secret；安装包没有作者的 GitHub 身份、Brevo 密钥、个人邮箱或额度数据。详细行为见 [云端预约说明](docs/cloud-and-weekly.md)，公共订阅见 [Brevo 配置](docs/email-brevo.md)。

## 开发

需要 Node.js 24、Python 3。构建 macOS 安装包还需要 Python 3.12、Xcode Command Line Tools，构建机器为 Apple Silicon。

```sh
npm ci
npm test
npm run build
bash scripts/build_macos.sh
open artifacts/Token重置.app
```

`npm run dev` 只预览桌面前端；浏览器没有原生桥接和本机私有数据，不能用它验证系统通知或云端预约开关。原生应用内的所有主界面均随包加载，不依赖网站或本机 HTTP 服务。

版本信息：`package.json` / `package-lock.json` 为 0.1.0，macOS 构建读取 package.json；Windows 项目 Version 与本机 Codex clientInfo 需同步更新。

## 数据与提醒的限制

公开动态来自第三方镜像，可能不完整、失效或限流。80 分是规则提醒门槛，未经统计校准，不代表 80% 的发生概率。个人周邮件按最后同步的时间提醒，不确认额度已到账；云端调度和投递可能延迟，下一周期需本机重新读取。明确退出登录、切换账号、改期与取消需要成功同步才会影响云端。

本项目为个人学习与公益研究用途，非 OpenAI 或 X 官方服务。遵守相关服务条款，不高频或大规模抓取。仓库不包含登录凭据、用户状态或实时采集数据。

## 邮件邀请权限

看板无需邀请码。邮件由云端验证邀请码及邮箱后统一发送，用户无需配置发件服务。部署与迁移见 [邮件说明](docs/email-brevo.md)。服务端尚未配置时，应用明确显示邮件服务准备中。
