import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from monitor.weekly_cloud import sync, deliver, validate, content, upload
from monitor.store import Store
from monitor.feeds import stamp

NOW = datetime(2026, 9, 10, 6, tzinfo=timezone.utc)
END = NOW + timedelta(days=6)
CONFIG = {'weeklyCloud': {'enabled': True, 'recipient': 'owner@example.com', 'repository': 'owner/repo', 'ghExecutable': '/usr/bin/gh'}}


def snapshot(end=END, used=30, owner='private-owner'):
    return {'status': 'ok', 'checkedAt': stamp(NOW), 'accountKey': owner, 'windows': [
        {'id': 'codex', 'label': 'Codex', 'usedPercent': used, 'resetsAt': stamp(end)}]}


class Client:
    def __init__(self): self.sent = []; self.credits = 300; self.fail = False
    def request(self, method, path, payload=None):
        if path == '/account': return {'plan': [{'type':'free','creditsType':'sendLimit','credits':self.credits}]}
        self.sent.append(payload)
        if self.fail: raise TimeoutError()
        return {'messageId': 'accepted'}


class WeeklyCloudTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(Path(self.tmp.name)/'private.sqlite3')
        self.patch = patch('monitor.weekly_cloud.upload'); self.upload = self.patch.start()
    def tearDown(self): self.patch.stop(); self.store.close(); self.tmp.cleanup()
    def register(self, s=None):
        result = sync(CONFIG, self.store, s or snapshot(), NOW)
        self.assertEqual(result['status'], 'synced')
        return copy.deepcopy(self.upload.call_args.args[1])

    def test_upload_contains_only_schedule_not_identity_usage_or_auth(self):
        payload = self.register()
        self.assertEqual(set(payload['jobs'][0]), {'id','email','label','dueAt','observedAt'})
        self.assertNotIn('private-owner', json.dumps(payload))
        self.assertNotIn('usedPercent', json.dumps(payload))

    def test_unchanged_deadline_does_not_reupload_or_change_id(self):
        payload = self.register()
        changed = snapshot(used=45)
        changed['checkedAt'] = stamp(NOW+timedelta(minutes=15))
        sync(CONFIG,self.store,changed,NOW+timedelta(minutes=15))
        self.assertEqual(self.upload.call_count,1)
        self.assertEqual(list(self.store.get('weeklyCloudPrivate')['entries'].values()),payload['jobs'])

    def test_replacement_cancels_future_deadline_and_account_switch_replaces_all(self):
        first = self.register()['jobs'][0]['id']
        sync(CONFIG,self.store,snapshot(END+timedelta(days=1)),NOW)
        self.assertEqual(len(self.upload.call_args.args[1]['jobs']),1)
        self.assertNotEqual(self.upload.call_args.args[1]['jobs'][0]['id'],first)
        sync(CONFIG,self.store,snapshot(owner='different-account'),NOW)
        self.assertEqual(len(self.upload.call_args.args[1]['jobs']),1)

    def test_after_due_refresh_keeps_due_job_alongside_next_week(self):
        old = self.register()['jobs'][0]['id']
        s = snapshot(END+timedelta(days=7)); s['checkedAt'] = stamp(END+timedelta(minutes=1))
        sync(CONFIG,self.store,s,END+timedelta(minutes=1))
        jobs = self.upload.call_args.args[1]['jobs']
        self.assertEqual(len(jobs),2); self.assertIn(old,[j['id'] for j in jobs])

    def test_failure_preserves_ack_and_retries_same_deadline(self):
        first = self.register()
        self.upload.side_effect = TimeoutError()
        result = sync(CONFIG,self.store,snapshot(END+timedelta(days=1)),NOW)
        self.assertEqual(result['status'],'sync-failed')
        self.assertEqual(list(self.store.get('weeklyCloudPrivate')['entries'].values()),first['jobs'])
        self.upload.side_effect = None
        self.assertEqual(sync(CONFIG,self.store,snapshot(END+timedelta(days=1)),NOW)['status'],'synced')

    def test_network_failure_keeps_schedule_and_explicit_logout_cancels(self):
        self.register()
        self.assertEqual(sync(CONFIG,self.store,{'status':'unavailable','reason':'codex-timeout'},NOW)['status'],'read-unavailable')
        self.assertEqual(self.upload.call_count,1)
        sync(CONFIG,self.store,{'status':'unavailable','reason':'chatgpt-login-required'},NOW)
        self.assertEqual(self.upload.call_args.args[1]['jobs'],[])

    def test_ambiguous_upload_retry_reuses_id_even_after_deadline(self):
        self.upload.side_effect = TimeoutError()
        sync(CONFIG,self.store,snapshot(),NOW)
        attempted = copy.deepcopy(self.upload.call_args.args[1]['jobs'][0])
        self.upload.side_effect = None
        s = snapshot(END+timedelta(days=7)); s['checkedAt'] = stamp(END+timedelta(minutes=1))
        sync(CONFIG,self.store,s,END+timedelta(minutes=1))
        self.assertIn(attempted,self.upload.call_args.args[1]['jobs'])

    def test_disable_clears_remote_schedule_without_fresh_quota(self):
        self.register()
        config = copy.deepcopy(CONFIG); config['weeklyCloud']['enabled'] = False
        result = sync(config,self.store,{'status':'unavailable'},NOW)
        self.assertEqual(result['status'],'disabled'); self.assertFalse(result['enabled'])
        self.assertEqual(self.upload.call_args.args[1]['jobs'],[])

    def test_disable_after_uncertain_first_upload_still_cancels_remote(self):
        self.upload.side_effect = TimeoutError()
        sync(CONFIG,self.store,snapshot(),NOW)
        self.upload.side_effect = None
        config = copy.deepcopy(CONFIG); config['weeklyCloud']['enabled'] = False
        self.assertEqual(sync(config,self.store,snapshot(),NOW)['status'],'disabled')
        self.assertEqual(self.upload.call_args.args[1]['jobs'],[])

    def test_first_past_read_and_unused_window_never_schedule(self):
        for s in [snapshot(NOW-timedelta(minutes=1)), snapshot(used=0)]:
            result = sync(CONFIG,self.store,s,NOW)
            self.assertEqual(result['pending'],0)
        self.assertEqual(self.upload.call_args.args[1]['jobs'],[])

    def test_cloud_fires_after_six_days_without_any_local_read_exactly_once(self):
        payload = self.register(); client = Client(); ledger = {}; saves = []
        checkpoint = lambda v: saves.append(copy.deepcopy(v))
        self.assertEqual(deliver(payload,ledger,client,'sender@example.com',END-timedelta(seconds=1),checkpoint)['submitted'],0)
        result = deliver(payload,ledger,client,'sender@example.com',END,checkpoint)
        self.assertEqual(result['submitted'],1)
        self.assertEqual(list(saves[0].values()),['claimed'])
        self.assertEqual(list(saves[1].values()),['submitted'])
        # Restart with persisted ledger; same Secret can stay present for weeks.
        deliver(payload,json.loads(json.dumps(ledger)),client,'sender@example.com',END+timedelta(minutes=15),checkpoint)
        self.assertEqual(len(client.sent),1)
        self.assertEqual(client.sent[0]['to'],[{'email':'owner@example.com'}])
        self.assertNotIn('owner@example.com',json.dumps(ledger))

    def test_checkpoint_failure_prevents_any_external_send(self):
        payload = self.register(); client = Client()
        with self.assertRaises(OSError):
            deliver(payload,{},client,'sender@example.com',END,lambda v: (_ for _ in ()).throw(OSError()))
        self.assertFalse(client.sent)

    def test_uncertain_send_is_not_automatically_retried(self):
        payload = self.register(); client = Client(); client.fail = True; ledger = {}
        with self.assertRaises(TimeoutError): deliver(payload,ledger,client,'sender@example.com',END,lambda v: None)
        client.fail = False
        self.assertEqual(deliver(payload,ledger,client,'sender@example.com',END,lambda v: None)['status'],'needs-review')
        self.assertEqual(len(client.sent),1)

    def test_no_quota_keeps_job_retryable_and_expired_jobs_do_not_send(self):
        payload = self.register(); client = Client(); client.credits = 0; ledger = {}
        with self.assertRaises(RuntimeError): deliver(payload,ledger,client,'sender@example.com',END,lambda v: None)
        self.assertFalse(ledger); self.assertFalse(client.sent)
        client.credits = 300
        deliver(payload,ledger,client,'sender@example.com',END+timedelta(hours=25),lambda v: None)
        self.assertFalse(client.sent)

    def test_invalid_times_duplicate_ids_and_email_fail_closed(self):
        payload = self.register()
        for key,value in [('dueAt',stamp(NOW)),('dueAt',stamp(NOW+timedelta(days=100))),('email','a@example.com\nBcc:other@example.com'),('observedAt','bad')]:
            p = copy.deepcopy(payload); p['jobs'][0][key]=value
            with self.assertRaises((ValueError,TypeError)): validate(p)
        payload['jobs'] *= 2
        with self.assertRaises(ValueError): validate(payload)

    def test_mail_escapes_labels_and_never_claims_verified_reset(self):
        job = self.register()['jobs'][0]; job['label']='<script>alert(1)</script>'
        html = content(job)
        self.assertNotIn('<script>',html); self.assertIn('不代表已经确认额度到账',html)


class UploadTests(unittest.TestCase):
    def test_private_payload_goes_over_stdin_not_process_arguments(self):
        payload={'version':1,'jobs':[]}
        with patch('monitor.weekly_cloud.os.access',return_value=True), patch('monitor.weekly_cloud.subprocess.run') as run:
            run.return_value.returncode=0
            upload(CONFIG['weeklyCloud'],payload)
            self.assertEqual(json.loads(run.call_args.kwargs['input']),payload)
            self.assertNotIn(json.dumps(payload),run.call_args.args[0])
