# 0.1.0 · 本地与云端预约

本地代码仓库是 [token-reset-desktop](https://github.com/Brandon030722/token-reset-desktop)，现有云端服务位于 [token-reset](https://github.com/Brandon030722/token-reset)。后者继续保存 Secrets、运行定时任务和发信，独立仓库不会自动成为新的发信端。

macOS 应用每 15 分钟读取本机 Codex 官方 app-server 的七天窗口（windowDurationMins=10080），展示实际剩余额度和 resetsAt。只读访问不运行模型任务，也不兑换额度。

私有 `~/.tibo-reset/monitor.config.json` 的 `weeklyCloud` 包含 enabled、recipient、repository、ghExecutable（已登录 GitHub CLI 的完整路径）。启用后将邮箱、额度名称、读取时间、恢复时间和随机事件 ID 写入目标仓库 Secret `PERSONAL_WEEKLY_SCHEDULE`；用量、账号标识和登录凭据不上传。需要该仓库的 Secrets 写权限，不是任何订阅用户都可共用作者凭据。

只有已用额度大于 0、且真实读取到未来七天恢复时间的窗口才预约。未用窗口的时间可能随查询移动，不自动预约。下一周期至少需一次新的有效读取；不会永远自动加七天。账号切换或明确退出登录后同步取消旧预约，网络故障保留已同步预约并提示。改期替换未来旧预约，已到期旧预约保留最多 24 小时，防止新的读取抢先覆盖。

云端每 15 分钟尝试检查已同步时间；关机不影响已登记的预约。调度和投递可能延迟，到点邮件不代表已确认额度到账。取消需联网同步成功，无法撤回已提交的邮件或正在执行的任务。本机系统通知在关机期间暂停，恢复运行后最多补提醒 24 小时内到期的窗口。

个人邮件只发给本人；公共订阅名单仅接收符合公开信号规则的提醒。80 分是规则门槛，不是 80% 概率，个人时间预约不走该门槛。公共群发只允许一个发布端，现有配置由云端负责，本机 sendEmail 保持 false。

云端先持久化随机事件 ID 的领取状态再发信，重跑不会重复；发送结果不明时标记待核查，不盲目重发。Secret 上传超时重试保持相同事件 ID。云端公开账本只存随机 ID 与状态，不写邮箱或个人恢复时间。

本机 `codex-usage.sqlite3`、`codex-usage.json` 仅位于私有状态目录。安装包与仓库均不含个人数据。原协议参考：[OpenAI App Server](https://learn.chatgpt.com/docs/app-server)。
