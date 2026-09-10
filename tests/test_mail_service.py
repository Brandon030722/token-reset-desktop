import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from monitor.mail_service import read_session, sync, set_enabled
from monitor.store import Store
from monitor.feeds import stamp

class SubscriberMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state.sqlite3')
        self.now = datetime(2026,9,10,tzinfo=timezone.utc)
        self.session = {'serviceUrl':'https://mail.example.com','token':'a'*64,'email':'qa@example.com','subscriptionStatus':'active','weeklyEnabled':True}
        self.save()
        self.snapshot = {'status':'ok','checkedAt':stamp(self.now),'accountKey':'private-account', 'windows':[{'id':'codex','label':'Codex','usedPercent':50,'resetsAt':stamp(self.now+timedelta(days=7))}]}
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def save(self): (self.root/'mail-session.json').write_text(json.dumps(self.session))
    def test_credentials_never_uploaded_with_usage(self):
        with patch('monitor.mail_service.request',return_value={'status':'synced','pending':1}) as send:
            state=sync(self.root,self.store,self.snapshot,self.now)
            payload=send.call_args.args[1]
            self.assertEqual(state['status'],'synced')
            for private in ('accountKey','private-account','usedPercent','email','token'):
                self.assertNotIn(private,json.dumps(payload))
            first=payload['scope']
            sync(self.root,self.store,self.snapshot,self.now)
            self.assertEqual(first,send.call_args.args[1]['scope'])
            self.snapshot['accountKey']='other-account'
            sync(self.root,self.store,self.snapshot,self.now)
            self.assertNotEqual(first,send.call_args.args[1]['scope'])
    def test_pending_cannot_schedule_or_enable(self):
        self.session['subscriptionStatus']='pending';self.save()
        with patch('monitor.mail_service.request') as send:
            self.assertFalse(sync(self.root,self.store,self.snapshot,self.now)['configured']);send.assert_not_called()
        with self.assertRaises(ValueError): set_enabled(self.root,True)
    def test_failure_preserves_remote_and_disable_submits_empty(self):
        with patch('monitor.mail_service.request') as send:
            result=sync(self.root,self.store,{**self.snapshot,'status':'unavailable','reason':'network'},self.now)
            self.assertEqual(result['status'],'read-unavailable');send.assert_not_called()
        set_enabled(self.root,False)
        with patch('monitor.mail_service.request',return_value={'status':'disabled','pending':0}) as send:
            sync(self.root,self.store,self.snapshot,self.now)
            self.assertEqual(send.call_args.args[1]['windows'],[])
            self.assertFalse(send.call_args.args[1]['enabled'])
    def test_logout_cancels_but_unused_window_not_scheduled(self):
        with patch('monitor.mail_service.request',return_value={'status':'disabled'}) as send:
            sync(self.root,self.store,{'status':'unavailable','reason':'chatgpt-login-required'},self.now)
            self.assertFalse(send.call_args.args[1]['enabled'])
            self.snapshot['windows'][0]['usedPercent']=0
            sync(self.root,self.store,self.snapshot,self.now)
            self.assertEqual(send.call_args.args[1]['windows'],[])
    def test_invalid_endpoint_and_missing_session(self):
        for url in ('http://example.com','https://a:b@example.com','https://example.com/path','https://example.com/#x'):
            self.session['serviceUrl']=url;self.save();self.assertIsNone(read_session(self.root))
        (self.root/'mail-session.json').unlink()
        self.assertIsNone(sync(self.root,self.store,self.snapshot,self.now))

    def test_owner_migration_must_cancel_old_appointment_first(self):
        from monitor.codex_usage import sync_mail
        self.store.put('weeklyCloudPrivate', {'entries':{'old':{}}})
        with patch('monitor.codex_usage.sync_weekly_cloud', return_value={'status':'sync-failed'}) as legacy, patch('monitor.codex_usage.sync_subscriber_mail') as cloud:
            result=sync_mail({'weeklyCloud':{'enabled':True}},self.root,self.store,self.snapshot,self.now)
            self.assertEqual(result['status'],'sync-failed');cloud.assert_not_called()
            self.assertFalse(legacy.call_args.args[0]['weeklyCloud']['enabled'])
        with patch('monitor.codex_usage.sync_weekly_cloud', return_value={'status':'disabled'}), patch('monitor.codex_usage.sync_subscriber_mail', return_value={'status':'synced'}):
            self.assertEqual(sync_mail({},self.root,self.store,self.snapshot,self.now)['status'],'synced')
            self.assertTrue(self.store.get('subscriberMailMigrated'))
