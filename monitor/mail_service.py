"""Subscriber-only schedule client. Never uploads Codex credentials or quota use."""
import hashlib
import json
import re
import uuid
import os
import tempfile
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from .brevo import NoRedirect
from .feeds import date, stamp


def read_session(directory):
    path = Path(directory)/'mail-session.json'
    try:
        if path.stat().st_size > 16384: return None
        data = json.loads(path.read_text())
        url = urlsplit(data.get('serviceUrl', ''))
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            return None
        if not re.fullmatch(r'[a-f0-9]{64}', data.get('token', '')): return None
        return data
    except (OSError, ValueError, TypeError): return None


def request(session, payload):
    req = urllib.request.Request(session['serviceUrl'].rstrip('/')+'/v1/schedule',
        data=json.dumps(payload).encode(), method='POST', headers={
            'Content-Type':'application/json', 'User-Agent':'TokenResetDesktop/0.1.1', 'Authorization':'Bearer '+session['token']})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
        raw = response.read(16385)
    if len(raw) > 16384: raise ValueError('Invalid service response')
    result = json.loads(raw)
    if result.get('status') not in ('synced', 'disabled'): raise ValueError('Unconfirmed synchronization')
    return result


def sync(directory, store, snapshot, now):
    session = read_session(directory)
    if not session: return None  # Owner-operated GitHub bridge remains separate.
    configured = session.get('subscriptionStatus') == 'active'
    enabled = session.get('weeklyEnabled') is True
    previous = store.get('subscriberMailStatus') or {}
    state = {'enabled':enabled, 'configured':configured, 'status':'disabled', 'pending':0}
    if not configured: return state
    try:
        valid = snapshot.get('status') == 'ok' and timedelta(0) <= now-date(snapshot['checkedAt']) <= timedelta(minutes=30)
        logout = snapshot.get('reason') == 'chatgpt-login-required'
        if enabled and not valid and not logout:
            return {**previous, **{'enabled':enabled,'configured':configured,'status':'read-unavailable'}}
        # Use an opaque random scope on account change, never an uploaded account ID.
        owner = hashlib.sha256((snapshot.get('accountKey','')+'|'+session['serviceUrl']+'|'+session['token']).encode()).hexdigest()
        private = store.get('subscriberMailPrivate') or {}
        if private.get('owner') != owner:
            private = {'owner':owner, 'scope':uuid.uuid4().hex}
            store.put('subscriberMailPrivate', private)
        active = enabled and valid
        payload = {'scope':private['scope'], 'enabled':active, 'observedAt':snapshot['checkedAt'] if active else stamp(now), 'windows':[]}
        if active:
            for w in snapshot['windows']:
                if w['usedPercent'] <= 0 or date(w['resetsAt']) <= now: continue
                payload['windows'].append({'id':w['id'],'label':w['label'],'windowDurationMins':10080,'dueAt':w['resetsAt']})
        result = request(session, payload)
        state.update({k:result[k] for k in ('status','pending','nextAt','syncedAt') if k in result})
    except Exception:
        state = {**previous, **state, 'status':'sync-failed'}
    store.put('subscriberMailStatus', state)
    return state


def set_enabled(directory, enabled):
    session = read_session(directory)
    if not session: return False
    if session.get('subscriptionStatus') != 'active': raise ValueError('Confirm email first')
    session['weeklyEnabled'] = enabled
    path = Path(directory)/'mail-session.json'
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.mail-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(session, handle); handle.flush(); os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return True
