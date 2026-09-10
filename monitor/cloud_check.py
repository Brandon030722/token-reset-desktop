"""Verify cloud mail access; a manual dispatch may request one connection test."""
import json
import os
import re
from .brevo import Brevo, readiness
from .email_design import shell


def main():
    try:
        key = os.environ.get('BREVO_API_KEY', '')
        if not key:
            raise ValueError('Missing mail configuration')
        client = Brevo(key)
        config = {'listId': os.environ.get('BREVO_LIST_ID', ''), 'maxRecipients': 100}
        if not re.fullmatch(r'[1-9]\d*', config['listId']):
            raise ValueError('Missing list ID')
        status = readiness(client, config)
        if status not in ('ready', 'empty-list'):
            print(json.dumps({'status': status})); return 2
        if os.environ.get('SEND_CONNECTION_TEST') == 'true':
            if os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
                raise ValueError('Test requires manual dispatch')
            recipient, sender = os.environ.get('BREVO_TEST_RECIPIENT', ''), os.environ.get('BREVO_FROM_EMAIL', '')
            if not all(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', x) for x in (recipient, sender)):
                raise ValueError('Test address missing')
            client.request('POST', '/smtp/email', {
                'sender': {'name': 'Token重置', 'email': sender}, 'to': [{'email': recipient}],
                'subject': 'Token重置 · 云端连接测试（非重置公告）',
                'htmlContent': shell('云端已经接力', '这是一封连接测试，不代表发现额度重置。',
                    '<h1 style="font-size:28px;line-height:1.4">电脑休息时，<br>我们继续帮你留意。</h1>'
                    '<p>这封邮件从 GitHub Actions 云端提交，用于验证监控服务的发信连接。</p>'
                    '<p style="background:#edf5ff;padding:16px;border-radius:14px"><strong>连接测试 · 非重置公告</strong><br>'
                    '本次没有发现或宣告额度重置，也不会改变你的 Codex 额度。</p>'
                    '<p>之后云端每 15 分钟尝试检查公开动态，符合提醒条件才寄信；调度和投递可能延迟。</p>',
                    '你收到此邮件，是因为这个邮箱被指定用于连接测试。<br>普通订阅者不会收到本次测试。')})
            status = 'test-submitted'
        print(json.dumps({'status': status, 'provider': 'brevo'})); return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
