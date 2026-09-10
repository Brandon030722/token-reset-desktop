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
from monitor.mail import dispatch, render
from monitor.runner import DEFAULT_FEEDS, load_config, run
from monitor.store import Store, process_lock

NOW = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
PROMISE = "We will reset Codex usage limits for all paid users tomorrow."
TARGETED_COMPENSATION = (
    "There was a bit of a kerfuffle this morning with some banked resets not fully applying "
    "when used in ChatGPT Work and Codex. Everyone who used one in the affected time window "
    "is getting another one and an email to apologize."
)
CONFIG = {"groupId": "42", "fromEmail": "alerts@example.org", "optInConfirmed": True}


def post(pid="123", text=PROMISE, at=NOW):
    return {"id": pid, "text": text, "postedAt": stamp(at),
            "url": "https://x.com/thsottiaux/status/" + pid}


class FakeMailer:
    def __init__(self, fail=None, audience=None):
        self.calls, self.fail, self.audience = [], fail, audience
    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.fail == path:
            raise TimeoutError()
        if method == "GET":
            return {"data": {"id": "99", "status": "draft", "can_be_scheduled": True,
                             "filter": self.audience if self.audience is not None else
                             [[{"operator": "in_any", "args": ["groups", ["42"]]}]]}}
        return {"data": {"id": "99"}}


class ConfigTests(unittest.TestCase):
    def test_blank_path_uses_defaults_without_reading_a_directory(self):
        with patch.dict("os.environ", {}, clear=True), patch("monitor.runner.Path.read_text") as read:
            for path in ("", " ", "\t\n"):
                with self.subTest(path=repr(path)):
                    config = load_config(path)
                    self.assertEqual(config["feeds"], DEFAULT_FEEDS)
                    self.assertFalse(config.get("sendEmail", False))
                    self.assertFalse(config["optInConfirmed"])
            read.assert_not_called()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def active(self):
        update(self.store, [], NOW - timedelta(hours=1))
        return update(self.store, [post()], NOW)

    def test_bootstrap_never_notifies_history(self):
        snapshot = update(self.store, [post()], NOW)
        client = FakeMailer()
        self.assertEqual(dispatch(self.store, snapshot, NOW, CONFIG, client), "bootstrap-suppressed")
        self.assertEqual(client.calls, [])

    def test_qualifying_event_sent_once_including_after_restart(self):
        snapshot = self.active()
        client, checks = FakeMailer(), []
        dispatch(self.store, snapshot, NOW, CONFIG, client, lambda: checks.append(self.store.alert("reset-123")["status"]))
        self.assertEqual(checks, ["creating", "created", "scheduling", "submitted"])
        self.store.close()
        self.store = Store(self.root / "state.sqlite3")
        self.assertEqual(dispatch(self.store, snapshot, NOW, CONFIG, client), "submitted")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0][2]["groups"], ["42"])

    def test_creation_timeout_never_retries_blindly(self):
        snapshot, client = self.active(), FakeMailer(fail="/campaigns")
        with self.assertRaises(TimeoutError):
            dispatch(self.store, snapshot, NOW, CONFIG, client)
        self.assertEqual(dispatch(self.store, snapshot, NOW, CONFIG, client), "needs-review")
        self.assertEqual(len(client.calls), 1)

    def test_scheduling_timeout_never_sends_second_campaign(self):
        snapshot, client = self.active(), FakeMailer(fail="/campaigns/99/schedule")
        with self.assertRaises(TimeoutError):
            dispatch(self.store, snapshot, NOW, CONFIG, client)
        dispatch(self.store, snapshot, NOW, CONFIG, client)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(self.store.alert("reset-123")["campaignId"], "99")

    def test_checkpoint_failure_prevents_external_calls(self):
        snapshot, client = self.active(), FakeMailer()
        def fail():
            raise RuntimeError("push failed")
        with self.assertRaises(RuntimeError):
            dispatch(self.store, snapshot, NOW, CONFIG, client, fail)
        self.assertEqual(client.calls, [])

    def test_wrong_audience_is_never_scheduled(self):
        snapshot, client = self.active(), FakeMailer(audience=[])
        with self.assertRaises(ValueError):
            dispatch(self.store, snapshot, NOW, CONFIG, client)
        self.assertFalse(any(c[1].endswith("/schedule") for c in client.calls))

    def test_duplicate_promise_same_event_and_completion(self):
        snapshot = self.active()
        snapshot = update(self.store, [post(), post("124", at=NOW + timedelta(minutes=10))], NOW + timedelta(minutes=30))
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(len(snapshot["evidence"]), 2)
        snapshot = update(self.store, [post("125", "We have reset Codex limits for everyone.", NOW + timedelta(hours=1))], NOW + timedelta(hours=1))
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(snapshot["events"][0]["status"], "confirmed")
        self.assertIsNone(snapshot["forecast"])

    def test_cancellation_clears_forecast(self):
        self.active()
        snapshot = update(self.store, [post("124", "We will not reset Codex limits for everyone tomorrow.", NOW + timedelta(minutes=5))], NOW + timedelta(minutes=30))
        self.assertEqual(snapshot["events"][0]["status"], "retracted")
        self.assertIsNone(snapshot["forecast"])

    def test_future_window_does_not_extend_on_poll(self):
        snapshot = self.active()
        end = snapshot["forecast"]["windowEndsAt"]
        later = update(self.store, [post()], NOW + timedelta(hours=2))
        self.assertEqual(later["forecast"]["windowEndsAt"], end)
        self.assertIsNone(update(self.store, [post()], NOW + timedelta(hours=49))["forecast"])

    def test_stale_demo_missing_evidence_do_not_alert(self):
        snapshot = self.active()
        self.assertFalse(eligible(snapshot, NOW + timedelta(hours=2)))
        for mutate in [lambda s: s.update(mode="demo"), lambda s: s.update(evidence=[]),
                       lambda s: s["evidence"][0].update(url="https://evil.example/status/123")]:
            s = copy.deepcopy(snapshot)
            mutate(s)
            self.assertFalse(eligible(s, NOW))

    def test_fan_requests_and_conditional_promises_below_threshold(self):
        for text in ["Please reset Codex for everyone?",
                     "If we get capacity we will reset Codex for everyone tomorrow.",
                     "We might reset Codex for everyone tomorrow.", "My Codex will reset tomorrow."]:
            result = classify(text)
            self.assertTrue(result is None or result[2] < 80, text)

    def test_rules_require_codex_and_scope(self):
        self.assertIsNone(classify("We will reset passwords for everyone tomorrow."))
        self.assertLess(classify("We will reset Codex tomorrow.")[2], 80)
        self.assertGreaterEqual(classify(PROMISE)[2], 80)
        self.assertLess(classify("We will discuss whether to reset Codex for all users tomorrow.")[2], 80)

    def test_actual_banked_reset_compensation_is_not_a_global_event(self):
        self.assertEqual(classify(TARGETED_COMPENSATION), ("context", 0, 0))
        snapshot = update(self.store, [post(text=TARGETED_COMPENSATION)], NOW)
        self.assertEqual(snapshot["events"][0]["type"], "limited-reset")
        self.assertEqual(snapshot["events"][0]["status"], "promised")
        self.assertEqual(snapshot["evidence"][0]["text"], TARGETED_COMPENSATION)
        self.assertEqual(snapshot["history"], [])
        self.assertIsNone(snapshot["forecast"])
        self.assertFalse(eligible(snapshot, NOW))

    def test_explicit_incident_cohort_compensation_does_not_use_all_as_global(self):
        for text in [
            "We will reset Codex for all affected users tomorrow as compensation for the outage.",
            "Impacted accounts will receive another reset credit for Codex tomorrow.",
            "We will reset Codex for everyone who used a credit in the affected time window "
            "tomorrow as compensation.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(classify(text), ("context", 0, 0))

    def test_targeted_compensation_does_not_change_an_existing_global_forecast(self):
        before = self.active()
        snapshot = update(self.store, [post("124", TARGETED_COMPENSATION, NOW + timedelta(minutes=10))],
                          NOW + timedelta(minutes=30))
        self.assertEqual([e for e in snapshot["events"] if e["type"] == "global-reset"], before["events"])
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertEqual(len(snapshot["evidence"]), 2)
        self.assertEqual(snapshot["forecast"]["eventId"], before["forecast"]["eventId"])
        self.assertTrue(eligible(snapshot, NOW + timedelta(minutes=30)))

    def test_previously_discarded_seen_post_is_recovered_once_without_resetting_ledgers(self):
        self.store.mark_seen(["123"])
        self.store.claim("mail-old", "submitted", stamp(NOW))
        for offset in (0, 30, 60):
            snapshot = update(self.store, [post(text=TARGETED_COMPENSATION)], NOW + timedelta(minutes=offset))
            self.assertEqual(len(snapshot["events"]), 1)
            self.assertEqual(len(snapshot["evidence"]), 1)
            self.assertIsNone(snapshot["forecast"])
        self.assertFalse(self.store.unseen("123"))
        self.assertEqual(self.store.alert("mail-old")["status"], "submitted")

    def test_global_promise_does_not_merge_into_limited_event(self):
        update(self.store, [post(text=TARGETED_COMPENSATION)], NOW)
        snapshot = update(self.store, [post("124", PROMISE, NOW + timedelta(minutes=1))], NOW + timedelta(minutes=30))
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertEqual(snapshot["forecast"]["eventId"], "reset-124")

    def test_limited_cancellation_does_not_retract_global_event(self):
        self.active()
        snapshot = update(self.store, [post("124", "We will not reset Codex for all affected users tomorrow as compensation.", NOW + timedelta(minutes=1))], NOW + timedelta(minutes=30))
        self.assertEqual(snapshot["forecast"]["eventId"], "reset-123")
        self.assertTrue(eligible(snapshot, NOW + timedelta(minutes=30)))
        self.assertEqual(snapshot["events"][0]["status"], "retracted")

    def test_compensation_context_does_not_hide_an_explicit_global_promise(self):
        for text in [
            PROMISE,
            TARGETED_COMPENSATION + " " + PROMISE,
            "We will reset Codex for everyone tonight as compensation for affected users.",
            "Some users were affected by an outage. We will reset Codex for all paid users tomorrow.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(classify(text)[0], "promise")
                self.assertGreaterEqual(classify(text)[2], 80)

    def test_compensation_context_does_not_hide_a_global_completion(self):
        text = TARGETED_COMPENSATION + " We have reset Codex limits for everyone."
        self.assertEqual(classify(text), ("confirmation", 0, 0))

    def test_mail_escapes_text_and_contains_unsubscribe(self):
        snapshot = self.active()
        snapshot["evidence"][0]["summary"] = '<img src=x onerror="alert(1)">'
        output = render(snapshot)
        self.assertNotIn("<img", output)
        self.assertIn("{$unsubscribe}", output)
        self.assertIn("未经统计校准", output)

    def test_failed_feed_keeps_previous_success_timestamp(self):
        before = self.active()
        config = self.root / "config.json"
        config.write_text("{}")
        with patch("monitor.runner.fetch_feeds", side_effect=FeedUnavailable([])):
            result = run(config, self.store.path, self.root / "data", dry_run=True)
        self.assertEqual(result["status"], "source-unavailable")
        self.assertEqual(self.store.get("snapshot")["checkedAt"], before["checkedAt"])

    def test_poll_cooldown_does_not_fetch_again(self):
        self.store.put("lastAttempt", stamp(datetime.now(timezone.utc)))
        with patch("monitor.runner.fetch_feeds") as fetch:
            result = run(self.root / "missing.json", self.store.path, self.root / "data")
        self.assertEqual(result["status"], "cooldown")
        fetch.assert_not_called()

    def test_lock_prevents_two_local_pollers(self):
        with process_lock(self.root / "monitor.lock"):
            with self.assertRaises(OSError):
                with process_lock(self.root / "monitor.lock"):
                    pass


class FeedTests(unittest.TestCase):
    def test_fallback_after_unavailable_mirror(self):
        body = b"""<rss><channel><item><link>https://x.com/thsottiaux/status/123</link>
        <pubDate>Thu, 10 Sep 2026 02:00:00 GMT</pubDate><description>Public update</description>
        </item></channel></rss>"""
        class Response(io.BytesIO):
            url = "https://second.example/feed"
            headers = {"Date": "Thu, 10 Sep 2026 03:00:00 GMT", "Age": "0"}
        with patch("monitor.feeds.urllib.request.urlopen", side_effect=[TimeoutError(), Response(body)]) as request:
            posts, source, failures = fetch_feeds(["https://first.example/feed", "https://second.example/feed"], NOW)
        self.assertEqual(source, "second.example")
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(request.call_count, 2)

    def test_stale_cached_response_is_not_fresh(self):
        class Response(io.BytesIO):
            url = "https://mirror.example/feed"
            headers = {"Age": "7200"}
        with patch("monitor.feeds.urllib.request.urlopen", return_value=Response(b"<rss/>")):
            with self.assertRaises(FeedUnavailable):
                fetch_feeds(["https://mirror.example/feed"], NOW)

    def test_filters_reposts_and_quoted_promises(self):
        body = b"""<rss><channel>
          <item><link>https://x.com/thsottiaux/status/123</link><pubDate>Thu, 10 Sep 2026 02:00:00 GMT</pubDate>
          <description>&lt;p&gt;Interesting.&lt;/p&gt;&lt;blockquote&gt;We will reset Codex for all tomorrow.&lt;/blockquote&gt;</description></item>
          <item><link>https://x.com/other/status/456</link><pubDate>Thu, 10 Sep 2026 02:00:00 GMT</pubDate><description>Fake promise</description></item>
          </channel></rss>"""
        posts = parse_feed(body, NOW)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["text"], "Interesting.")
        self.assertIsNone(classify(posts[0]["text"]))

    def test_atom_and_nitter_canonicalize(self):
        body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <link href="https://nitter.example/thsottiaux/status/124#m"/>
          <published>2026-09-10T02:00:00Z</published><content>Codex reset soon</content>
          </entry></feed>"""
        self.assertEqual(parse_feed(body, NOW, "nitter.example")[0]["url"], "https://x.com/thsottiaux/status/124")

    def test_rejects_html_and_entities(self):
        for body in [b"<html>Just a moment</html>", b'<!DOCTYPE rss [<!ENTITY x "secret">]><rss/>']:
            with self.assertRaises(ValueError):
                parse_feed(body, NOW)


if __name__ == "__main__":
    unittest.main()
