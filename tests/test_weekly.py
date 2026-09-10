import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from monitor.codex_usage import weekly_windows, record_usage, refresh, CodexUnavailable
from monitor.local_notifications import notification_candidate, claim_notification, finish_notification
from monitor.store import Store
from monitor.feeds import stamp
from monitor.runner import run, main
from monitor.cloud_check import main as cloud_check

NOW = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
END = NOW + timedelta(minutes=10)


def payload(end=END, used=75, slot='primary'):
    return {'rateLimitsByLimitId': {'codex': {slot: {'windowDurationMins': 10080, 'usedPercent': used, 'resetsAt': end.timestamp()}}}}


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.store = Store(self.root/'codex-usage.sqlite3')
        notification_candidate(self.store, NOW)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_actual_primary_weekly_window_without_five_hour_quota(self):
        windows = weekly_windows(payload(), NOW)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]['remainingPercent'], 25)
        self.assertEqual(windows[0]['windowDurationMins'], 10080)
        self.assertEqual(windows[0]['resetsAt'], stamp(END))

    def test_secondary_weekly_and_separate_model_bucket(self):
        p = payload(slot='secondary')
        p['rateLimitsByLimitId']['codex']['primary'] = {'windowDurationMins': 300,'usedPercent':5,'resetsAt':END.timestamp()}
        p['rateLimitsByLimitId']['spark'] = {'limitName': 'Spark', 'secondary': {'windowDurationMins':10080,'usedPercent':0,'resetsAt':END.timestamp()}}
        windows = weekly_windows(p, NOW)
        self.assertEqual([w['id'] for w in windows], ['codex','spark'])
        self.assertEqual(windows[1]['remainingPercent'], 100)

    def test_missing_weekly_data_is_not_invented(self):
        for p in [{}, {'rateLimits': {'primary': None}}, {'rateLimitsByLimitId': {'codex': {'primary': {'windowDurationMins':300,'usedPercent':100,'resetsAt':END.timestamp()}}}}]:
            self.assertEqual(weekly_windows(p, NOW), [])

    def test_bad_values_and_ambiguous_weekly_windows_fail_closed(self):
        for field, value in [('usedPercent',None),('usedPercent',True),('usedPercent',101),('usedPercent',float('nan')),('resetsAt',None),('resetsAt',True),('resetsAt',(NOW+timedelta(days=100)).timestamp())]:
            p=payload();p['rateLimitsByLimitId']['codex']['primary'][field]=value
            self.assertEqual(weekly_windows(p,NOW),[])
        p=payload();p['rateLimitsByLimitId']['codex']['secondary']=p['rateLimitsByLimitId']['codex']['primary']
        with self.assertRaises(CodexUnavailable): weekly_windows(p,NOW)

    def test_deadline_triggers_once_even_when_server_already_advanced(self):
        record_usage(self.store,payload(),'account-a',NOW)
        self.assertEqual(notification_candidate(self.store,NOW)['status'],'none')
        later=END+timedelta(minutes=5)
        record_usage(self.store,payload(END+timedelta(days=7),used=2),'account-a',later)
        candidate=notification_candidate(self.store,later)['candidate'];self.assertIsNotNone(candidate)
        self.assertIn('每周恢复时间已到',candidate['title'])
        claim=claim_notification(self.store,candidate['eventId'],later)
        finish_notification(self.store,candidate['eventId'],claim['claimToken'],later)
        self.store.close();self.store=Store(self.root/'codex-usage.sqlite3')
        record_usage(self.store,payload(END+timedelta(days=7),used=3),'account-a',later+timedelta(minutes=15))
        self.assertEqual(notification_candidate(self.store,later+timedelta(minutes=15))['status'],'none')

    def test_new_week_late_account_switch_never_notifies_old_account(self):
        record_usage(self.store,payload(),'account-a',NOW)
        later=END+timedelta(minutes=1)
        record_usage(self.store,payload(END+timedelta(days=7)),'account-b',later)
        self.assertEqual(notification_candidate(self.store,later)['status'],'none')
        self.assertEqual(self.store.get('weeklyDue'),[])

    def test_first_read_after_old_deadline_does_not_backfill(self):
        later=END+timedelta(minutes=1)
        record_usage(self.store,payload(),'account-a',later)
        record_usage(self.store,payload(END+timedelta(days=7)),'account-a',later+timedelta(minutes=15))
        self.assertEqual(self.store.get('weeklyDue'),[])

    def test_unrelated_early_quota_reset_replaces_old_deadline(self):
        record_usage(self.store,payload(),'account-a',NOW)
        record_usage(self.store,payload(END+timedelta(days=7)),'account-a',NOW+timedelta(minutes=5))
        later=END+timedelta(minutes=1)
        record_usage(self.store,payload(END+timedelta(days=7)),'account-a',later)
        self.assertEqual(notification_candidate(self.store,later)['status'],'none')

    def test_sleep_catchup_is_bounded(self):
        record_usage(self.store,payload(),'account-a',NOW)
        later=END+timedelta(hours=25)
        record_usage(self.store,payload(END+timedelta(days=7)),'account-a',later)
        self.assertEqual(notification_candidate(self.store,later)['status'],'none')

    def test_quota_read_failure_retains_history_but_disables_notification(self):
        with patch('monitor.codex_usage.read_local',return_value=(payload(),'private-account-key')):
            self.assertEqual(refresh({},self.root,NOW)['status'],'ok')
        with patch('monitor.codex_usage.read_local',side_effect=CodexUnavailable('chatgpt-login-required')):
            self.assertEqual(refresh({},self.root,END+timedelta(minutes=15))['status'],'unavailable')
        self.assertEqual(notification_candidate(self.store,END+timedelta(minutes=15))['status'],'none')
        raw=(self.root/'codex-usage.json').read_text()
        self.assertNotIn('private-account-key',raw)
        self.assertNotIn('accountKey',raw)
        self.assertEqual(len(json.loads(raw)['windows']),1)
        self.assertFalse((self.root/'data').exists())

    def test_read_rate_limited_at_15_minutes(self):
        with patch('monitor.codex_usage.read_local',return_value=(payload(),'account-a')) as read:
            refresh({},self.root,NOW)
            self.assertEqual(refresh({},self.root,NOW+timedelta(minutes=14,seconds=59))['status'],'cooldown')
            self.assertEqual(read.call_count,1)
            refresh({},self.root,NOW+timedelta(minutes=15))
            self.assertEqual(read.call_count,2)

    def test_failed_read_does_not_lose_due_catchup_after_reconnect(self):
        with patch('monitor.codex_usage.read_local',return_value=(payload(),'account-a')):refresh({},self.root,NOW)
        later=END+timedelta(minutes=6)
        with patch('monitor.codex_usage.read_local',side_effect=TimeoutError()):refresh({},self.root,later)
        with patch('monitor.codex_usage.read_local',return_value=(payload(END+timedelta(days=7)),'account-a')):refresh({},self.root,later+timedelta(minutes=15))
        self.assertEqual(notification_candidate(self.store,later+timedelta(minutes=15))['status'],'candidate')

    def test_weekly_cli_uses_private_state_and_never_calls_mail_or_feed(self):
        with patch('monitor.codex_usage.read_local',return_value=(payload(),'account-a')), patch('monitor.runner.fetch_feeds') as feed, patch('monitor.runner.dispatch_brevo') as mail, patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['--read-codex-usage','--config',str(self.root/'missing.json'),'--state',str(self.root/'state.sqlite3')]),0)
        feed.assert_not_called();mail.assert_not_called()
        self.assertFalse((self.root/'state.sqlite3').exists())

    def test_mirror_poll_15_minute_boundary(self):
        path=self.root/'monitor.sqlite3';s=Store(path);s.put('lastAttempt',stamp(NOW));s.close()
        p={'id':'100','text':'Public update','postedAt':stamp(NOW),'url':'https://x.com/thsottiaux/status/100'}
        with patch('monitor.runner.datetime') as clock, patch('monitor.runner.fetch_feeds',return_value=([p],'mock',[])) as feed, patch.dict('os.environ',{},clear=True):
            clock.now.return_value=NOW+timedelta(minutes=14,seconds=59)
            self.assertEqual(run(self.root/'missing.json',path,self.root/'public')['status'],'cooldown');feed.assert_not_called()
            clock.now.return_value=NOW+timedelta(minutes=15)
            self.assertEqual(run(self.root/'missing.json',path,self.root/'public')['status'],'ok');self.assertEqual(feed.call_count,1)


class CloudChecks(unittest.TestCase):
    def test_scheduled_readiness_never_sends_connection_test(self):
        env={'BREVO_API_KEY':'fake','BREVO_LIST_ID':'3','GITHUB_EVENT_NAME':'schedule','SEND_CONNECTION_TEST':'false'}
        with patch.dict('os.environ',env,clear=True),patch('monitor.cloud_check.Brevo') as client,patch('monitor.cloud_check.readiness',return_value='ready'),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(cloud_check(),0)
            client.return_value.request.assert_not_called()

    def test_test_flag_cannot_send_from_scheduled_trigger(self):
        env={'BREVO_API_KEY':'fake','BREVO_LIST_ID':'3','GITHUB_EVENT_NAME':'schedule','SEND_CONNECTION_TEST':'true'}
        with patch.dict('os.environ',env,clear=True),patch('monitor.cloud_check.Brevo') as client,patch('monitor.cloud_check.readiness',return_value='ready'),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(cloud_check(),1)
            client.return_value.request.assert_not_called()
