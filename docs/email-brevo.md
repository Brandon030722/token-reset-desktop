# Brevo 免费内测邮件配置

应用当前使用 Brevo campaign API，默认关闭发送。管理员只需配置一次服务；订阅用户只需邀请码与邮箱确认。默认内测邀请码为 `TOKENEMAIL`（区分大小写，忽略首尾空格）。

邮件采用轻量卡片设计，确认邮件和提醒邮件共用品牌样式，详见 [邮件视觉与验证](email-design.md)。实际收信效果受邮箱客户端影响。

## 1. 配置账号

1. 注册/登录 Brevo Free，不购买套餐。完成账号资料与发件人邮箱验证码验证。账号能否发信和域名要求以实际审核为准。
2. 建立专用列表，例如 `Token重置 · 已确认订阅`，不要混用其他联系人列表。
3. 创建订阅表单，启用 double opt-in，只把确认后的邮箱加入上述列表。发布表单并复制 HTTPS 分享地址。
4. 在应用「提醒 → 高级选项 → 配置邮件订阅入口」保存分享地址。这个动作只保存入口，不启用发信。
5. 创建 API Key，在本地配置文件保存；不要发送到聊天、前端、公开仓库或安装包。

## 2. 私有发信配置

本地配置文件：`~/.tibo-reset/monitor.config.json`。保留已有采集配置，填入以下字段：

```json
{
  "mailProvider": "brevo",
  "apiKey": "你的 Brevo API Key",
  "listId": "专用列表的数字 ID",
  "fromEmail": "已经验证的发件邮箱",
  "maxRecipients": 100,
  "optInConfirmed": false,
  "sendEmail": false
}
```

用自己的邮箱完成确认、投递、退订测试后，再将 `optInConfirmed` 和 `sendEmail` 设为 `true`。不要导入未确认联系人。测试邮件不要伪装成真实重置公告。

也可用环境变量 `BREVO_API_KEY`、`BREVO_LIST_ID`、`BREVO_FROM_EMAIL`、`BREVO_OPT_IN_CONFIRMED=true`、`TIBO_MAIL_PROVIDER=brevo`、`TIBO_SEND_EMAIL=true`。GitHub Actions 中密钥只放 Secrets，其余开关可用 Variables。Mac 从 Finder 启动通常不会继承终端设置的环境变量，推荐使用权限为 600 的本地文件。

## 3. 邀请码

「提醒」输入 `TOKENEMAIL` 验证后，才能从应用打开订阅表单。通过记录由原生应用保存，关闭面板不会丢失；并不等于已经订阅或邮件可以发送。前端不会收到 API Key。

可在 `~/.tibo-reset/web.config.json` 设置 `emailInviteHash` 为新邀请码的 SHA-256 十六进制摘要；修改后旧授权失效。不要把默认邀请码当作强鉴权：本地应用和静态网页可以被修改，公开表单链接也可被转发。这只是应用内的内测入口门槛；正式防绕过、限邀请人数需要服务端验证和表单接入，当前不具备。

## 4. 额度和故障处理

- 官方 Free 额度目前为每天 300 封，API 支持自动发送；确认邮件及其他发送同样消耗账号额度。
- 程序发送前核对 Free 计划返回额度与专用列表人数，默认列表超过 100 人就暂停新 campaign。这个限制不是订阅人数的强制上限。
- 名单由 Brevo 管理，程序只读取人数和列表 ID，不把邮箱名单写入公开状态库。
- 同一事件跨重启、跨 MailerLite/Brevo 切换共用去重记录，避免重复提交。超时后不自动重建或重发，需要在 Brevo 按 campaign ID 核查。
- 小范围补偿当前只发本机系统通知；邮件继续按广泛重置信号评分 ≥80 分（不是发生概率） 判断。
- API 受理不代表收件箱送达；只有实际验证后才能宣布接通。仅保存表单、通过邀请码或离线测试均不代表已接通。

参考：[免费计划](https://help.brevo.com/hc/en-us/articles/208589409-About-Brevo-s-pricing-plans)、[验证发件人](https://help.brevo.com/hc/en-us/articles/208836149-Create-a-new-sender-From-name-and-From-email)、[创建邮件](https://developers.brevo.com/reference/create-email-campaign)、[发送邮件](https://developers.brevo.com/reference/send-email-campaign-now)、[退订占位符](https://help.brevo.com/hc/en-us/articles/209553645-Insert-a-custom-unsubscribe-link-in-your-emails)。

配额检查不是原子预留，确认邮件或其他活动可能同时消耗额度；实际发送仍可能被服务拒绝或延期。只允许一个本地或云端发送器连接同一订阅名单。
