"""Read-only local Codex quota integration. Never reads tokens or starts turns.

Private state stays alongside the desktop configuration, outside public/data.
Only 10080-minute windows are weekly; primary/secondary placement is irrelevant.
"""
import hashlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .feeds import date, stamp
from .store import Store, atomic_json, process_lock
from .weekly_cloud import sync as sync_weekly_cloud

WEEK_MINUTES = 7 * 24 * 60
POLL_SECONDS = 15 * 60
MAX_CATCHUP = timedelta(hours=24)


class CodexUnavailable(Exception):
    pass


def executable(config):
    candidates = [config.get('codexExecutable'), shutil.which('codex')]
    if sys.platform == 'darwin':
        candidates += ['/Applications/ChatGPT.app/Contents/Resources/codex', '/Applications/Codex.app/Contents/Resources/codex']
    for candidate in candidates:
        if candidate and Path(candidate).is_absolute() and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise CodexUnavailable('codex-not-found')


def read_local(config):
    """Short-lived official app-server protocol connection using local login.

    Account identity is hashed for deduplication; no email/token is exported.
    A private process group is always reaped, including helper SIGTERM on macOS.
    """
    process = subprocess.Popen([executable(config), 'app-server', '--stdio'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=os.name != 'nt')
    lines = queue.Queue(maxsize=64)
    stop = threading.Event()
    def reader():
        try:
            while not stop.is_set():
                line = process.stdout.readline(1_000_001)
                if not line or len(line) > 1_000_000:
                    break
                try: lines.put(line, timeout=0.1)
                except queue.Full: break
        finally:
            try: lines.put_nowait(None)
            except queue.Full: pass
    thread = threading.Thread(target=reader, daemon=True); thread.start()
    deadline = time.monotonic() + 20
    previous_handler = None
    if os.name != 'nt' and threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGTERM)
        def terminate(_signum, _frame): raise SystemExit(143)
        signal.signal(signal.SIGTERM, terminate)
    def send(msg):
        process.stdin.write((json.dumps(msg)+'\n').encode()); process.stdin.flush()
    def request(rid, method, params=None):
        msg = {'id': rid, 'method': method}
        if params is not None: msg['params'] = params
        send(msg)
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0: raise CodexUnavailable('codex-timeout')
            try: line = lines.get(timeout=remaining)
            except queue.Empty: raise CodexUnavailable('codex-timeout')
            if line is None: raise CodexUnavailable('codex-exited')
            try: response = json.loads(line)
            except (ValueError, TypeError): continue
            if response.get('id') != rid: continue
            if 'error' in response: raise CodexUnavailable('codex-request-failed')
            return response.get('result', {})
    try:
        request(1, 'initialize', {'clientInfo': {'name': 'token_reset_monitor', 'version': '0.1.0'}})
        send({'method': 'initialized'})
        before = request(2, 'account/read', {'refreshToken': False}).get('account') or {}
        if before.get('type') != 'chatgpt': raise CodexUnavailable('chatgpt-login-required')
        result = request(3, 'account/rateLimits/read')
        after = request(4, 'account/read', {'refreshToken': False}).get('account') or {}
        if before != after: raise CodexUnavailable('account-changed')
        # accountId identifies the quota owner/workspace when supported.
        identity = result.get('accountId') or before.get('email')
        if not isinstance(identity, str) or not identity.strip(): raise CodexUnavailable('account-identity-unavailable')
        return result, hashlib.sha256(identity.strip().lower().encode()).hexdigest()
    finally:
        stop.set()
        if os.name != 'nt':
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        elif process.poll() is None:
            process.terminate()
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != 'nt':
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            else: process.kill()
            process.wait(timeout=2)
        process.stdin.close(); process.stdout.close(); thread.join(timeout=0.2)
        if previous_handler is not None: signal.signal(signal.SIGTERM, previous_handler)


def weekly_windows(result, now):
    buckets = result.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        legacy = result.get('rateLimits')
        buckets = {legacy.get('limitId') or 'codex': legacy} if isinstance(legacy, dict) else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(key, str) or len(key) > 100 or not isinstance(bucket, dict): continue
        seen = set()
        for slot in ('primary', 'secondary'):
            w = bucket.get(slot)
            if not isinstance(w, dict) or type(w.get('windowDurationMins')) is not int or w['windowDurationMins'] != WEEK_MINUTES: continue
            used, end = w.get('usedPercent'), w.get('resetsAt')
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100: continue
            if type(end) not in (int, float) or not math.isfinite(end): continue
            try: reset = datetime.fromtimestamp(end, timezone.utc)
            except (ValueError, OverflowError, OSError): continue
            # Reject impossible far-future timestamps instead of inventing cycles.
            if not -MAX_CATCHUP <= reset-now <= timedelta(days=8): continue
            if key in seen: raise CodexUnavailable('ambiguous-weekly-window')
            seen.add(key)
            label = 'Codex' if key == 'codex' else bucket.get('limitName') or key
            if not isinstance(label, str): label = key
            windows.append({'id': key, 'label': label[:100], 'windowDurationMins': WEEK_MINUTES,
                'usedPercent': used, 'remainingPercent': 100-used, 'resetsAt': stamp(reset)})
    return sorted(windows, key=lambda w: (w["id"] != "codex", w["label"]))


def record_usage(store, result, account_key, now):
    windows = weekly_windows(result, now)
    previous = store.get('weeklySnapshot') or {}
    same = previous.get('accountKey') == account_key
    due = store.get('weeklyDue', []) if same else []
    new_by_id = {w['id']: w for w in windows}
    if same:
        for old in previous.get('windows', []):
            end = date(old['resetsAt'])
            current = new_by_id.get(old['id'])
            # Require an observation made before this deadline and a fresh
            # same-account quota read that still exposes this weekly bucket.
            if current and date(previous['checkedAt']) < end <= now and now-end <= MAX_CATCHUP:
                event_id = 'weekly-' + hashlib.sha256((account_key+'|'+old['id']+'|'+old['resetsAt']).encode()).hexdigest()[:40]
                if not any(x['eventId'] == event_id for x in due):
                    due.append({'eventId': event_id, 'title': old['label']+' · 每周恢复时间已到',
                        'body': '已到之前记录的每周额度恢复时间。请打开 Codex 核对当前额度；这不是额外重置公告。',
                        'dueAt': old['resetsAt'], 'accountKey': account_key, 'bucketId': old['id']})
    due = [x for x in due if x['bucketId'] in new_by_id and timedelta(0) <= now-date(x['dueAt']) <= MAX_CATCHUP][-20:]
    snapshot = {'version': 1, 'status': 'ok', 'checkedAt': stamp(now), 'attemptedAt': stamp(now),
        'accountKey': account_key, 'source': 'codex-app-server', 'windows': windows}
    with store.transaction():
        store.put('weeklySnapshot', snapshot); store.put('weeklyDue', due)
    return snapshot


def current_candidate(store, now):
    snapshot = store.get('weeklySnapshot') or {}
    if snapshot.get('status') != 'ok' or not timedelta(0) <= now-date(snapshot['checkedAt']) <= timedelta(hours=1):
        return None, 'weekly-unavailable'
    for item in store.get('weeklyDue', []):
        if item['accountKey'] != snapshot.get('accountKey') or not timedelta(0) <= now-date(item['dueAt']) <= MAX_CATCHUP: continue
        row = store.db.execute('SELECT status FROM local_notifications WHERE event_id=?', (item['eventId'],)).fetchone()
        if not row or row[0] == 'pending':
            return {k: item[k] for k in ('eventId', 'title', 'body')}, None
    return None, 'no-weekly-reset-due'


def refresh(config, directory, now=None):
    now = now or datetime.now(timezone.utc)
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    with process_lock(directory/'codex-usage.lock'):
        store = Store(directory/'codex-usage.sqlite3')
        try:
            previous = store.get('weeklySnapshot') or {}
            attempted = previous.get('attemptedAt')
            if attempted and timedelta(0) <= now-date(attempted) < timedelta(seconds=POLL_SECONDS):
                cloud = sync_weekly_cloud(config, store, previous, now)
                atomic_json(directory/'codex-usage.json', {**{k:v for k,v in previous.items() if k != 'accountKey'}, 'cloudMail': cloud})
                (directory/'codex-usage.json').chmod(0o600)
                return {'status': 'cooldown', 'weeklyStatus': previous.get('status'), 'nextCheckAt': stamp(date(attempted)+timedelta(seconds=POLL_SECONDS))}
            try:
                result, account_key = read_local(config)
                snapshot = record_usage(store, result, account_key, now)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, CodexUnavailable) else type(exc).__name__
                snapshot = {**previous, 'version': 1, 'status': 'unavailable', 'attemptedAt': stamp(now), 'reason': reason, 'windows': previous.get('windows', [])}
                store.put('weeklySnapshot', snapshot)
            # Identity and outbox are private. The WebView sees quota fields only.
            public = {k:v for k,v in snapshot.items() if k != 'accountKey'}
            public['cloudMail'] = sync_weekly_cloud(config, store, snapshot, now)
            atomic_json(directory/'codex-usage.json', public)
            for p in (directory/'codex-usage.json', directory/'codex-usage.sqlite3'):
                p.chmod(0o600)
            return {'status': snapshot['status'], 'weeklyWindows': len(snapshot['windows']), 'reason': snapshot.get('reason')}
        finally:
            store.close()
