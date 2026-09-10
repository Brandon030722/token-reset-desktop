"""Personal deadlines in a repository Secret; no login tokens or quota uploaded.

This is an owner-operated integration: the local gh login must administer the
configured repository. Distributed apps never contain that login or mail keys.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .brevo import Brevo
from .email_design import shell, escape, local_time, button
from .feeds import date, stamp
from .store import atomic_json

SECRET = 'PERSONAL_WEEKLY_SCHEDULE'
EMAIL = r'[^@\s<>\r\n]{1,128}@[^@\s<>\r\n]+\.[^@\s<>\r\n]+'


def validate(payload):
    if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('jobs'), list) or len(payload['jobs']) > 40:
        raise ValueError('Invalid personal schedule')
    seen = set()
    for job in payload['jobs']:
        if not isinstance(job, dict) or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')) or job['id'] in seen:
            raise ValueError('Invalid schedule ID')
        seen.add(job['id'])
        if not isinstance(job.get('email'), str) or len(job['email']) > 254 or not re.fullmatch(EMAIL, job['email']):
            raise ValueError('Invalid personal recipient')
        if not isinstance(job.get('label'), str) or not 1 <= len(job['label']) <= 100:
            raise ValueError('Invalid label')
        observed, due = date(job['observedAt']), date(job['dueAt'])
        if not timedelta(0) < due-observed <= timedelta(days=8):
            raise ValueError('Invalid observed deadline')
    return payload


def upload(config, payload):
    repo = config.get('repository', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository')
    exe = config.get('ghExecutable') or shutil.which('gh')
    if not exe or not Path(exe).is_absolute() or not os.access(exe, os.X_OK):
        raise ValueError('GitHub CLI unavailable')
    # Secret travels over stdin, never shell arguments or log output.
    result = subprocess.run([exe, 'secret', 'set', SECRET, '--repo', repo],
        input=json.dumps(validate(payload)).encode(), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=15)
    if result.returncode: raise RuntimeError('Schedule upload failed')


def sync(config, store, snapshot, now):
    settings = config.get('weeklyCloud') or {}
    previous = store.get('weeklyCloudPrivate') or {}
    prepared = store.get('weeklyCloudPrepared') or {}
    enabled = settings.get('enabled') is True
    if not enabled and not previous and not prepared:
        return {'status': 'disabled', 'pending': 0, 'enabled': False, 'configured': bool(settings.get('repository') and settings.get('recipient'))}
    try:
        if enabled and snapshot.get('status') != 'ok':
            # Network failures preserve already registered deadlines. Explicit
            # logout cancels them when the cloud can be reached.
            if snapshot.get('reason') != 'chatgpt-login-required':
                return {**store.get('weeklyCloudStatus', {}), 'status': 'read-unavailable', 'enabled': enabled, 'configured': True}
        elif enabled and not timedelta(0) <= now-date(snapshot['checkedAt']) <= timedelta(minutes=30):
            return {**store.get('weeklyCloudStatus', {}), 'status': 'read-unavailable', 'enabled': enabled, 'configured': True}
        email = settings.get('recipient', '')
        if enabled and (not isinstance(email, str) or not re.fullmatch(EMAIL, email)):
            raise ValueError('Personal recipient missing')
        owner = hashlib.sha256((snapshot.get('accountKey', '')+'|'+email.lower()).encode()).hexdigest()
        if previous.get('entries') and previous.get('repository') != settings.get('repository'):
            raise ValueError('Cancel the old repository schedule before moving')
        same = previous.get('owner') == owner and previous.get('repository') == settings.get('repository')
        old = dict(previous.get('entries', {})) if same else {}
        if prepared.get('owner') == owner and prepared.get('repository') == settings.get('repository'):
            old.update(prepared.get('entries', {}))
        entries = {}
        valid = enabled and snapshot.get('status') == 'ok'
        if valid:
            # Keep a just-due entry if a later local read already advanced to the
            # next week before the cloud got a turn. Future replaced entries cancel.
            entries.update({k:j for k,j in old.items() if timedelta(0) <= now-date(j['dueAt']) <= timedelta(hours=24)})
            for window in snapshot['windows']:
                end = date(window['resetsAt'])
                # Completely unused buckets can report a moving placeholder end.
                if window['usedPercent'] <= 0 or not now < end: continue
                key = window['id']+'|'+stamp(end)
                entries[key] = old.get(key) or {'id': uuid.uuid4().hex, 'email': email,
                    'label': window['label'], 'dueAt': stamp(end), 'observedAt': snapshot['checkedAt']}
        payload = validate({'version': 1, 'jobs': list(entries.values())})
        changed = not same or entries != previous.get('entries')
        if changed:
            # Don't discard the old cloud schedule on a failed upload.
            # Persist IDs before the request: an upload timeout can still mean
            # the server accepted it. Retrying must reuse those same IDs.
            store.put('weeklyCloudPrepared', {'owner': owner, 'repository': settings['repository'], 'entries': entries})
            upload(settings, payload)
            previous = {'owner': owner, 'repository': settings['repository'], 'entries': entries, 'syncedAt': stamp(now)}
            store.put('weeklyCloudPrivate', previous)
        pending = sum(date(j['dueAt']) > now for j in entries.values())
        status = {'status': 'synced' if valid else 'disabled', 'pending': pending,
            'syncedAt': previous.get('syncedAt'), 'nextAt': min((j['dueAt'] for j in entries.values() if date(j['dueAt']) > now), default=None)}
    except Exception:
        status = {**store.get('weeklyCloudStatus', {}), 'status': 'sync-failed'}
    status.update({'enabled': enabled, 'configured': bool(settings.get('repository') and settings.get('recipient'))})
    store.put('weeklyCloudStatus', status)
    return status


def content(job):
    return shell('每周恢复时间到了', '电脑休息时，也有人帮你记着这个时间。',
        '<p style="color:#326bbe;font-weight:bold">你的个人每周提醒</p>'
        '<h1 style="font-size:28px;line-height:1.4">新的额度周期，<br>可以回来看看啦。</h1>'
        '<p>还记得上次为你记下的时间吗？现在已经到了。</p>'
        f'<div style="background:#edf5ff;padding:18px;border-radius:14px"><strong>{escape(job["label"])}</strong><br>'
        f'记录的恢复时间：{local_time(job["dueAt"])}<br><small>读取于 {local_time(job["observedAt"])}</small></div>'
        '<p>这是云端按本机先前记录发出的到点提醒，不代表已经确认额度到账，也不是额外重置公告。'
        '如果之后发生过提前重置或账号变更，请以 Codex 当前显示为准。</p>'
        + button('查看 Codex', 'https://chatgpt.com/codex') +
        '<p style="font-size:12px;color:#64748b">每次开机读取到新的有效时间后，会自动更新下一次提醒。不会自行无限增加七天。</p>',
        '这是你在 Token重置 中启用的个人提醒，不向公共订阅名单群发。<br>在「我的额度」关闭周邮件并同步后，可取消后续预约。')


def free_credit(client):
    plans = client.request('GET', '/account').get('plan', [])
    credits = [p.get('credits') for p in plans if p.get('type') == 'free' and p.get('creditsType') == 'sendLimit']
    return len(credits) == 1 and type(credits[0]) in (float, int) and math.isfinite(credits[0]) and credits[0] >= 1


def deliver(payload, ledger, client, sender, now, checkpoint):
    jobs = validate(payload)['jobs']
    if not re.fullmatch(EMAIL, sender): raise ValueError('Sender missing')
    submitted = 0
    for job in jobs:
        if date(job['observedAt']) > now or not timedelta(0) <= now-date(job['dueAt']) <= timedelta(hours=24): continue
        if job['id'] in ledger: continue
        if not free_credit(client): raise RuntimeError('Free mail quota unavailable')
        # Only opaque random IDs and outcomes are ever committed publicly.
        ledger[job['id']] = 'claimed'
        checkpoint(ledger)  # MUST be durable remotely before the external effect.
        try:
            response = client.request('POST', '/smtp/email', {
                'sender': {'name': 'Token重置', 'email': sender}, 'to': [{'email': job['email']}],
                'subject': 'Token重置 · '+job['label']+' 每周恢复时间到了', 'htmlContent': content(job)})
            if not isinstance(response.get('messageId'), str) or not response['messageId']:
                raise RuntimeError('Unconfirmed mail acceptance')
            ledger[job['id']] = 'submitted'; submitted += 1
        except Exception:
            ledger[job['id']] = 'needs-review'; checkpoint(ledger); raise
        checkpoint(ledger)
    uncertain = sum(ledger.get(j['id']) in ('claimed', 'needs-review') for j in jobs)
    return {'status': 'needs-review' if uncertain else 'ok', 'submitted': submitted,
            'pending': sum(date(j['dueAt']) > now for j in jobs), 'needsReview': uncertain}


def main():
    raw = os.environ.get(SECRET, '')
    if not raw:
        print(json.dumps({'status': 'not-configured'})); return 0
    try:
        if os.environ.get('GITHUB_ACTIONS') != 'true': raise ValueError('Cloud execution only')
        payload = validate(json.loads(raw))
        path = Path('.monitor/weekly-mail.json')
        ledger = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(ledger, dict): raise ValueError('Invalid mail ledger')
        def checkpoint(value):
            atomic_json(path, value)
            subprocess.run(['git', 'add', '--', str(path)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'commit', '--only', '-m', 'chore: checkpoint personal mail delivery', '--', str(path)], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(['git', 'push', 'origin', 'HEAD'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key = os.environ.get('BREVO_API_KEY', '')
        if not key: raise ValueError('Mail key missing')
        result = deliver(payload, ledger, Brevo(key), os.environ.get('BREVO_FROM_EMAIL', ''), datetime.now(timezone.utc), checkpoint)
        print(json.dumps(result)); return 1 if result['needsReview'] else 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
