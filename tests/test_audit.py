"""Synthetic regression cases, NOT a historical accuracy/calibration dataset."""
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from monitor.engine import classify, eligible, update
from monitor.feeds import FeedUnavailable, fetch_feeds, parse_feed, stamp
from monitor.local_notifications import notification_candidate
from monitor.runner import run, main
from monitor.store import Store

NOW = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
PROMISE = 'We will reset Codex usage limits for all paid users tomorrow.'


def post(pid='100', text=PROMISE, at=NOW):
    return dict(id=pid, text=text, postedAt=stamp(at), url='https://x.com/thsottiaux/status/' + pid)


def rss(p):
    from html import escape
    return ('<rss><channel><item><title>R to @someone</title><link>' + p['url'] + '</link><pubDate>' +
            p['postedAt'] + '</pubDate><description>' + escape(p['text']) + '</description></item></channel></rss>').encode()


class Response(io.BytesIO):
    url = 'https://mirror.example/feed'
    headers = {'Age': '0'}


class AuditRules(unittest.TestCase):
    def test_mail_clients_reject_credential_bearing_redirects(self):
        from monitor.mail import NoRedirect as LegacyRedirect
        from monitor.brevo import NoRedirect as BrevoRedirect
        for handler in (LegacyRedirect(), BrevoRedirect()):
            with self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/")

    def test_scope_and_object_false_positives_do_not_cross_threshold(self):
        cases = [
            'We will reset Codex for all Pro users tomorrow.',
            'All good! We will reset Codex for Pro users tomorrow.',
            'We will reset all Codex settings tomorrow.',
            'We will reset Codex for everyone in Europe tomorrow.',
            'We will reset Codex for everyone tomorrow in Europe.',
            'We will reset Codex for everyone except beta users tomorrow.',
            'We will reset Codex for everyone with a Plus account tomorrow.',
            'We will reset Codex for all our Enterprise accounts tomorrow.',
            'We will reset Codex for everyone eligible tomorrow.',
            'We will reset Codex for all tomorrow if capacity permits.',
            'Someone said we will reset Codex for everyone tomorrow.',
            'We will reset Codex for everyone tomorrow?',
            'We will reset Codex for everyone tomorrow. Update: not happening.',
            '"We will reset Codex for everyone tomorrow." That is a rumor.',
        ]
        for text in cases:
            with self.subTest(text=text):
                result = classify(text)
                self.assertTrue(result is None or result[2] < 80)

    def test_unrelated_question_and_self_contained_reply_keep_commitment(self):
        for text in [PROMISE, 'Ready? ' + PROMISE, '@bob ' + PROMISE]:
            with self.subTest(text=text):
                self.assertEqual(classify(text)[0], 'promise')

    def test_subject_qualifiers_never_retract_global_forecast(self):
        self.assertEqual(classify('We will not reset Codex for Pro users tomorrow.')[0], 'correction')
        self.assertEqual(classify('Correction: no reset after all.')[0], 'correction')

    def test_reply_and_nested_quote_parser(self):
        p = post(text='<p>Ready?</p><div class="quote"><div>' + PROMISE + '</div></div><script>' + PROMISE + '</script>')
        result = parse_feed(rss(p), NOW)
        self.assertEqual(result[0]['text'], 'Ready?')
        self.assertIsNone(classify(result[0]['text']))
        self.assertEqual(classify(parse_feed(rss(post()), NOW)[0]['text'])[0], 'promise')

    def test_atom_xhtml_retains_author_text_but_discards_quote(self):
        body = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="' + post()['url'] + '"/>'
                '<published>' + stamp(NOW) + '</published><content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">'
                '<p>' + PROMISE + '</p><blockquote>quoted misinformation</blockquote></div></content></entry></feed>').encode()
        self.assertEqual(parse_feed(body, NOW)[0]['text'], PROMISE)

    def test_regressed_feed_falls_back_even_with_fresh_http_header(self):
        with patch('monitor.feeds.urllib.request.urlopen', side_effect=[Response(rss(post(at=NOW-timedelta(days=1)))), Response(rss(post()))]):
            posts, _, failures = fetch_feeds(['https://a.example/rss', 'https://b.example/rss'], NOW, stamp(NOW))
        self.assertEqual(posts[0]['postedAt'], stamp(NOW))
        self.assertEqual(len(failures), 1)

    def test_all_feeds_contribute_and_conflicts_fail_closed(self):
        urls = ['https://a.example/rss', 'https://b.example/rss']
        with patch('monitor.feeds.urllib.request.urlopen', side_effect=[Response(rss(post())), Response(rss(post('101')))]):
            self.assertEqual(len(fetch_feeds(urls, NOW)[0]), 2)
        with patch('monitor.feeds.urllib.request.urlopen', side_effect=[Response(rss(post())), Response(rss(post(text='Edited post')))]):
            with self.assertRaises(FeedUnavailable):
                fetch_feeds(urls, NOW)


class AuditState(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state.db')
        update(self.store, [], NOW-timedelta(hours=1))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_24_hour_promise_expires_at_24_and_repeated_promises_do_not_extend(self):
        p = post(text='We will reset Codex for everyone within 24 hours.')
        snapshot = update(self.store, [p], NOW)
        self.assertEqual(snapshot['forecast']['windowEndsAt'], stamp(NOW+timedelta(hours=24)))
        later = update(self.store, [post('101', at=NOW+timedelta(hours=1))], NOW+timedelta(hours=1))
        self.assertEqual(snapshot['forecast']['windowEndsAt'], later['forecast']['windowEndsAt'])
        self.assertIsNone(update(self.store, [], NOW+timedelta(hours=24))['forecast'])
        new = update(self.store, [post('102', at=NOW+timedelta(hours=25))], NOW+timedelta(hours=25))
        self.assertEqual(new['forecast']['eventId'], 'reset-102')

    def test_source_edits_suspend_instead_of_reusing_old_score(self):
        update(self.store, [post()], NOW)
        snapshot = update(self.store, [post(text='We will not reset Codex tomorrow.')], NOW+timedelta(minutes=30))
        self.assertIsNone(snapshot['forecast'])
        self.assertTrue(snapshot['events'][0]['reviewRequired'])
        self.assertIn('will not', snapshot['evidence'][0]['text'])

    def test_state_failure_rolls_back_scores_snapshot_seen_and_suppression(self):
        before = self.store.get('snapshot')
        with patch.object(self.store, 'mark_seen', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                update(self.store, [post()], NOW)
        self.assertEqual(self.store.get('snapshot'), before)
        self.assertTrue(self.store.unseen('100'))
        self.assertIsNone(self.store.get('score:reset-100'))
        self.store.close()
        self.store = Store(self.root/'state.db')
        first = update(self.store, [post()], NOW)
        again = update(self.store, [post()], NOW+timedelta(minutes=30))
        self.assertEqual(len(first['evidence']), 1)
        self.assertEqual(first['events'], again['events'])

    def test_legacy_and_malformed_forecasts_cannot_send(self):
        original = update(self.store, [post()], NOW)
        mutations = [
            lambda s: s['forecast'].update(method='rules-v1'),
            lambda s: s['forecast'].update(probability48h=101),
            lambda s: s['forecast'].update(probability48h=True),
            lambda s: s['forecast'].update(probability48h=float('nan')),
            lambda s: s['forecast'].update(generatedAt=stamp(NOW+timedelta(seconds=1))),
            lambda s: s['forecast'].update(validUntil=stamp(NOW+timedelta(hours=2))),
            lambda s: s['forecast'].update(evidenceIds=['post-100', 'post-100']),
            lambda s: s['evidence'][0].update(text='All good! We will reset Codex for Pro users tomorrow.'),
            lambda s: s['evidence'][0].update(postedAt=stamp(NOW+timedelta(seconds=1))),
            lambda s: s['events'][0].update(status='confirmed'),
            lambda s: s['events'][0].update(reviewRequired=True),
        ]
        self.assertTrue(eligible(original, NOW))
        for change in mutations:
            s = copy.deepcopy(original)
            change(s)
            self.assertFalse(eligible(s, NOW))

    def test_limited_cancellation_leaves_broad_promise_intact(self):
        update(self.store, [post()], NOW)
        now = NOW+timedelta(minutes=30)
        s = update(self.store, [post('101', 'We will not reset Codex for Pro users tomorrow.', now)], now)
        self.assertTrue(eligible(s, now))
        self.assertEqual(s['events'][0]['type'], 'limited-reset')

    def test_retained_promise_survives_many_hints_and_replays(self):
        update(self.store, [post()], NOW)
        posts = [post()] + [post(str(101+i), 'Codex reset soon.', NOW+timedelta(minutes=i+1)) for i in range(8)]
        for offset in (30, 60, 90):
            now = NOW+timedelta(minutes=offset)
            s = update(self.store, posts, now)
            self.assertEqual(len(s['events']), 1)
            self.assertEqual(len(s['evidence']), 5)
            self.assertTrue(eligible(s, now))
            self.assertEqual(len(s['events'][0]['evidenceIds']), len(set(s['events'][0]['evidenceIds'])))

    def test_ambiguous_evidence_is_preserved_without_guessing(self):
        update(self.store, [post()], NOW)
        update(self.store, [post('101', 'We will reset Codex for everyone tomorrow, another reset.', NOW+timedelta(minutes=1))], NOW+timedelta(minutes=2))
        s = update(self.store, [post('102', 'Correction: no reset after all.', NOW+timedelta(minutes=3))], NOW+timedelta(minutes=4))
        self.assertIsNone(s['forecast'])
        self.assertTrue(any(e['id']=='unlinked-102' and e['reviewRequired'] for e in s['events']))
        self.assertEqual(len(s['evidence']), 3)

    def test_mail_failure_keeps_collection_and_local_notification_available(self):
        now = datetime.now(timezone.utc)
        notification_candidate(self.store, now-timedelta(hours=1))
        cfg = self.root/'config.json'
        cfg.write_text(json.dumps({'sendEmail': True, 'mailProvider': 'brevo', 'apiKey': 'fake-test-only'}))
        with patch.dict('os.environ', {}, clear=True), patch('monitor.runner.fetch_feeds', return_value=([post(at=now)], 'mock', [])), patch('monitor.runner.dispatch_brevo', side_effect=TimeoutError()):
            result = run(cfg, self.store.path, self.root/'data')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['notification'], 'mail-failed')
        self.assertEqual(json.loads((self.root/'data/health.json').read_text())['status'], 'ok')
        self.assertEqual(notification_candidate(self.store, now+timedelta(seconds=1))['status'], 'candidate')

    def test_mail_failure_marks_cli_failure_without_losing_json_success(self):
        with patch('monitor.runner.run', return_value={'status':'ok', 'notification':'mail-failed'}), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main([]), 2)


if __name__ == '__main__':
    unittest.main()
