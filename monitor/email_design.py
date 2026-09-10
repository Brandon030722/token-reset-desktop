"""Small, self-contained HTML emails: inline styles and no remote assets."""
import html
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit


def escape(value):
    return html.escape(str(value), quote=True)


def source_url(value):
    try:
        url = urlsplit(value)
        if url.scheme == 'https' and url.hostname in ('x.com', 'twitter.com') and not url.username and not url.password:
            return escape(value)
    except (TypeError, ValueError):
        pass
    return ''


def local_time(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return escape(value)
        return date.astimezone(timezone(timedelta(hours=8))).strftime('%m 月 %d 日 %H:%M') + '（北京时间）'
    except (TypeError, ValueError):
        return escape(value)


def button(label, url):
    return f'<table role="presentation" cellspacing="0" cellpadding="0"><tr><td bgcolor="#ffcf51" style="border:2px solid #223249;border-radius:28px;padding:13px 23px"><a href="{url}" style="font-size:16px;font-weight:bold;color:#223249;text-decoration:none;display:inline-block">{escape(label)} &nbsp;→</a></td></tr></table>'


def shell(title, preview, content, footer):
    return f'''<!doctype html><html lang="zh-CN" data-ark-theme="popucom" data-ark-depth="moderate"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title><style>a:focus-visible{{outline:3px solid #326bbe;outline-offset:4px}}@media(max-width:400px){{.mail-content{{padding:24px 20px!important}}}}</style></head>
<body style="margin:0;padding:0;background:#f4f7fc;color:#223249;font-family:Arial,'PingFang SC','Microsoft YaHei',sans-serif;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{escape(preview)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#f4f7fc"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:100%;max-width:560px;background:#fff;border:1px solid #e2e8f2;border-radius:22px;overflow:hidden">
<tr><td bgcolor="#223249" style="padding:23px 26px;border-radius:21px 21px 0 0"><table role="presentation" cellpadding="0" cellspacing="0"><tr><td bgcolor="#ffcf51" style="width:42px;height:42px;text-align:center;border-radius:14px;font-size:27px;font-weight:900;color:#223249">T!</td><td style="padding-left:12px;color:white;font-size:18px;font-weight:bold">Token重置<br><span style="font-size:11px;font-weight:normal;color:#c7d8ee;letter-spacing:1px">把值得关注的消息，轻轻送到</span></td></tr></table></td></tr>
<tr><td class="mail-content" style="padding:30px 26px 26px;font-size:15px;line-height:1.8">{content}</td></tr>
<tr><td style="padding:20px 26px;background:#f8faff;border-top:1px solid #e7edf5;border-radius:0 0 21px 21px;font-size:12px;color:#64748b;line-height:1.8">{footer}</td></tr></table>
<p style="font-size:11px;color:#64748b;margin:16px 0 0">独立开源观察项目 · 与 OpenAI 无关联</p>
</td></tr></table></body></html>'''


def confirmation():
    return shell('请确认订阅 Token重置', '还差最后一步，让值得关注的重置消息找到你。',
        '<p style="margin:0 0 8px;color:#326bbe;font-size:12px;font-weight:bold">欢迎来到观察站</p>'
        '<h1 style="margin:0 0 14px;font-size:29px;line-height:1.35;letter-spacing:-1px">消息来了，<br>我们帮你留意。</h1>'
        '<p style="margin:0 0 22px;color:#53647a">还差最后一步：确认这是你的邮箱。</p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="background:#edf5ff;border-radius:14px;padding:18px 20px">'
        '<strong style="font-size:15px">只在值得关注时，寄来一封</strong><br><span style="font-size:13px;color:#53647a">广泛重置信号评分达到 80 分时提醒。<br>同一事件一次，不发送日常汇报。</span></td></tr></table>'
        '<p style="margin:22px 0 15px">点击确认，即同意接收上述邮件提醒。</p>' + button('确认我的订阅', '{{ doubleoptin }}') +
        '<p style="font-size:12px;color:#64748b;margin:19px 0 0">评分不是发生概率；尚无历史命中率，不代表官方承诺。</p>',
        '不是你申请的？忽略这封邮件即可。<br>订阅后可随时通过提醒邮件底部的链接退订。')


def forecast_email(snapshot, preview=False):
    f = snapshot['forecast']
    evidence = [e for e in snapshot['evidence'] if e['id'] in f['evidenceIds']]
    event = next((e for e in snapshot.get('events', []) if e['id'] == f['eventId']), {})
    scope = event.get('scope') or '全体重置线索；具体套餐以原帖为准'
    if isinstance(scope, dict):
        scope = scope.get('label') or '具体套餐以原帖为准'
    cards = []
    for e in evidence[:3]:
        url = source_url(e.get('url', ''))
        link = f'<a href="{url}" style="color:#326bbe;text-decoration:underline">查看原帖 →</a>' if url else ''
        cards.append('<tr><td style="padding:15px 0;border-bottom:1px solid #e7edf5;font-size:14px;line-height:1.8">' +
                     escape(e['summary']) + f'<br><span style="font-size:11px;color:#64748b">{local_time(e["postedAt"])} &nbsp; {link}</span></td></tr>')
    sample = '<p style="background:#fff1c4;padding:10px 14px;border-radius:10px;font-size:12px;margin:0 0 18px"><strong>样式预览 · 测试邮件</strong><br>下方分数与线索仅用于展示，不是实时重置公告。</p>' if preview else ''
    content = sample + '<p style="margin:0 0 8px;color:#326bbe;font-size:12px;font-weight:bold">有一条消息，值得你看一眼</p>'
    content += '<h1 style="font-size:28px;line-height:1.4;margin:0 0 12px">重置线索，<br>有新进展。</h1><p style="margin:0 0 22px;color:#53647a">我们把关键信息整理好了，你可以直接查看原帖。</p>'
    content += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td bgcolor="#edf5ff" style="padding:20px;border-radius:15px"><span style="font-size:12px;color:#53647a">重置信号评分 · 非概率</span><br><strong style="font-size:52px;color:#326bbe;line-height:1.4">{escape(f["probability48h"])}<span style="font-size:23px">/100</span></strong><br><span style="font-size:12px;color:#53647a">实验性评分，不代表重置已发生。</span></td></tr></table>'
    content += f'<p style="font-size:13px;line-height:1.9;margin:18px 0 8px"><strong>适用范围</strong> &nbsp; {escape(scope)}<br><strong>观察截止</strong> &nbsp; {local_time(f["windowEndsAt"])}</p>'
    content += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">' + ''.join(cards) + '</table>'
    url = next((source_url(e.get('url', '')) for e in evidence if source_url(e.get('url', ''))), '')
    if url:
        content += '<div style="margin-top:24px">' + button('去看看最新线索', url) + '</div>'
    return shell('Token重置 · 新的重置线索', '重置线索达到提醒线，查看评分、适用范围与原帖。', content,
                 '你收到这封邮件，是因为已确认订阅 Token重置。<br>同一事件只提醒一次。想安静一阵？<a href="{{ unsubscribe }}" style="color:#326bbe;text-decoration:underline">退订提醒</a>。')
