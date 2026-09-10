import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { demoSnapshot } from '../src/demo';
import { parseSnapshot, canAlert, isSourceUrl } from '../src/domain';

const now = Date.parse(demoSnapshot.checkedAt);
test('demo is valid but can never generate mail', () => {
  assert.equal(parseSnapshot(demoSnapshot, now).mode, 'demo');
  assert.equal(canAlert(demoSnapshot, now), false);
});
test('invalid probabilities, sources and event links fail validation', () => {
  for (const mutate of [
    (s: typeof demoSnapshot) => { s.forecast!.probability48h = 101; },
    (s: typeof demoSnapshot) => { s.evidence[0].url = 'https://evil.example/123'; },
    (s: typeof demoSnapshot) => { s.forecast!.eventId = 'missing'; },
  ]) {
    const s = structuredClone(demoSnapshot);
    mutate(s);
    assert.throws(() => parseSnapshot(s, now));
  }
});
test('only canonical public post links are accepted', () => {
  assert.equal(isSourceUrl('https://x.com/thsottiaux/status/123'), true);
  assert.equal(isSourceUrl('https://x.com.evil.example/thsottiaux/status/123'), false);
  assert.equal(isSourceUrl('https://someone@x.com/thsottiaux/status/123'), false);
});
test('Python engine output passes frontend schema and expires', () => {
  const code = [
    'import json,tempfile',
    'from pathlib import Path',
    'from datetime import datetime,timezone,timedelta',
    'from monitor.store import Store',
    'from monitor.engine import update',
    'from monitor.feeds import stamp',
    'with tempfile.TemporaryDirectory() as d:',
    ' s=Store(Path(d)/"state.sqlite3")',
    ' now=datetime.now(timezone.utc)',
    ' update(s,[],now-timedelta(hours=1))',
    ' p={"id":"123","text":"We will reset Codex limits for all users tomorrow.","postedAt":stamp(now),"url":"https://x.com/thsottiaux/status/123"}',
    ' print(json.dumps(update(s,[p],now)))',
    ' s.close()',
  ].join('\n');
  const raw = execFileSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', code], { encoding: 'utf8' });
  const snapshot = parseSnapshot(JSON.parse(raw));
  assert.equal(canAlert(snapshot), true);
  assert.equal(canAlert(snapshot, Date.now() + 3600_001), false);
});

test('limited announcement is valid and visible without any global forecast', () => {
  const code = [
    'import json,tempfile',
    'from pathlib import Path',
    'from datetime import datetime,timezone',
    'from monitor.store import Store',
    'from monitor.engine import update',
    'from monitor.feeds import stamp',
    'with tempfile.TemporaryDirectory() as d:',
    ' s=Store(Path(d)/"state.sqlite3")',
    ' now=datetime.now(timezone.utc)',
    ' p={"id":"321","text":"Impacted accounts will receive another reset credit for Codex tomorrow.","postedAt":stamp(now),"url":"https://x.com/thsottiaux/status/321"}',
    ' s.mark_seen(["321"])',
    ' print(json.dumps(update(s,[p],now)))',
    ' s.close()',
  ].join('\n');
  const raw = execFileSync(process.platform === 'win32' ? 'python' : 'python3', ['-c', code], { encoding: 'utf8' });
  const snapshot = parseSnapshot(JSON.parse(raw));
  assert.equal(snapshot.forecast, null);
  assert.equal(snapshot.events[0].type, 'limited-reset');
  assert.equal(snapshot.evidence.length, 1);
  assert.equal(canAlert(snapshot), false);
});
