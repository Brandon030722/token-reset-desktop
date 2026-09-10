import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import json
from monitor.store import Store
from monitor.engine import update
from monitor.feeds import stamp
from monitor.brevo import dispatch_brevo, render_brevo
from monitor.runner import load_config

NOW = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
CONFIG = {"listId":"42", "fromEmail":"alerts@example.org", "optInConfirmed":True, "maxRecipients":100}

class Client:
    def __init__(self, fail=None, lists=None, count=10, credits=300, plan="free"):
        self.calls=[]; self.fail=fail; self.lists=lists or [42]; self.count=count; self.credits=credits; self.plan=plan
    def request(self, method, path, payload=None):
        self.calls.append((method,path,payload))
        if path==self.fail: raise TimeoutError()
        if path=="/contacts/lists/42": return {"id":42,"totalSubscribers":self.count}
        if path=="/account": return {"plan":[{"type":self.plan,"creditsType":"sendLimit","credits":self.credits}]}
        if path=="/emailCampaigns" and method=="POST": return {"id":99}
        if path=="/emailCampaigns/99": return {"status":"draft","recipients":{"lists":self.lists},"sender":{"email":CONFIG['fromEmail']}}
        return {}

class BrevoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.store=Store(self.root/'state.sqlite3')
        update(self.store,[],NOW-timedelta(hours=1))
        self.snapshot=update(self.store,[{"id":"123","text":"We will reset Codex for everyone tomorrow.","postedAt":stamp(NOW),"url":"https://x.com/thsottiaux/status/123"}],NOW)
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def test_verified_list_sent_once_across_restart(self):
        c=Client(); self.assertEqual(dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c),'submitted')
        body=next(p for m,u,p in c.calls if m=='POST' and u=='/emailCampaigns')
        self.assertEqual(body['recipients'],{'listIds':[42]}); self.assertIn('{{ unsubscribe }}',body['htmlContent']); self.assertNotIn('{$',body['htmlContent'])
        self.store.close(); self.store=Store(self.root/'state.sqlite3')
        before=len(c.calls); dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c); self.assertEqual(len(c.calls),before)
    def test_limits_and_no_optin_never_create_or_send(self):
        for c,status in [(Client(count=0),'empty-list'),(Client(count=101),'audience-limit'),(Client(credits=1),'quota-limited'),(Client(plan='subscription'),'free-plan-unverified')]:
            self.assertEqual(dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c),status)
            self.assertFalse(any(m=='POST' for m,u,p in c.calls))
        c=Client()
        with self.assertRaises(ValueError): dispatch_brevo(self.store,self.snapshot,NOW,{**CONFIG,'optInConfirmed':False},c)
        self.assertEqual(c.calls,[])
    def test_wrong_list_never_sends(self):
        c=Client(lists=[42,43])
        with self.assertRaises(ValueError): dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c)
        self.assertFalse(any(u.endswith('/sendNow') for m,u,p in c.calls))
        self.assertEqual(self.store.alert('reset-123')['status'],'needs-review')
    def test_creation_timeout_never_creates_again(self):
        c=Client(fail='/emailCampaigns')
        with self.assertRaises(TimeoutError): dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c)
        self.assertEqual(dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c),'needs-review')
        self.assertEqual(sum(u=='/emailCampaigns' for m,u,p in c.calls),1)
    def test_send_timeout_never_resends(self):
        c=Client(fail='/emailCampaigns/99/sendNow')
        with self.assertRaises(TimeoutError): dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c)
        self.assertEqual(dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c),'needs-review')
        self.assertEqual(sum(u.endswith('/sendNow') for m,u,p in c.calls),1)
    def test_expiring_during_api_calls_is_not_sent(self):
        c = Client()
        with patch('monitor.brevo.monotonic', side_effect=[0, 3601]):
            with self.assertRaises(ValueError):
                dispatch_brevo(self.store, self.snapshot, NOW, CONFIG, c)
        self.assertFalse(any(u.endswith('/sendNow') for m,u,p in c.calls))
        self.assertEqual(self.store.alert('reset-123')['status'], 'needs-review')

    def test_incomplete_durable_claim_surfaces_manual_review(self):
        self.store.claim('reset-123', 'scheduling', stamp(NOW))
        c = Client()
        self.assertEqual(dispatch_brevo(self.store, self.snapshot, NOW, CONFIG, c), 'needs-review')
        self.assertEqual(c.calls, [])

    def test_failed_checkpoint_prevents_mutations(self):
        c=Client()
        def fail(): raise RuntimeError('checkpoint')
        with self.assertRaises(RuntimeError): dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c,fail)
        self.assertFalse(any(m=='POST' for m,u,p in c.calls))
    def test_stale_or_demo_never_requests_provider(self):
        c=Client(); dispatch_brevo(self.store,self.snapshot,NOW+timedelta(hours=2),CONFIG,c)
        self.snapshot['mode']='demo'; dispatch_brevo(self.store,self.snapshot,NOW,CONFIG,c)
        self.assertEqual(c.calls,[])
    def test_config_provider_secrets_do_not_cross_over(self):
        p=self.root/'config.json'; p.write_text(json.dumps({**CONFIG,'mailProvider':'brevo'}))
        with patch.dict('os.environ',{'MAILERLITE_FROM_EMAIL':'wrong@example.org','MAILERLITE_OPT_IN_CONFIRMED':''},clear=True):
            config=load_config(p); self.assertEqual(config['fromEmail'],CONFIG['fromEmail']); self.assertTrue(config['optInConfirmed'])
        p.write_text(json.dumps({'groupId':'42','apiKey':'legacy-token'}))
        with patch.dict('os.environ',{},clear=True): self.assertEqual(load_config(p)['mailProvider'],'mailerlite')

    def test_email_escapes_source_text_and_rejects_unsafe_links(self):
        self.snapshot['evidence'][0]['summary'] = '<img src=x onerror=alert(1)>'
        self.snapshot['evidence'][0]['url'] = 'javascript:alert(1)'
        body = render_brevo(self.snapshot)
        self.assertIn('&lt;img', body)
        self.assertNotIn('<img', body)
        self.assertNotIn('href="javascript:', body)
        self.assertIn('{{ unsubscribe }}', body)
        self.assertIn('北京时间', body)
