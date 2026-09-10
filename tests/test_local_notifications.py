import copy
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from monitor.engine import update
from monitor.feeds import stamp
from monitor.local_notifications import (
    ENABLED_AT, claim_notification, finish_notification, notification_candidate,
)
from monitor.runner import main
from monitor.store import Store

NOW = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)


def post(at, pid="123"):
    return {"id": pid, "text": "We will reset Codex for all paid users tomorrow.",
            "postedAt": stamp(at), "url": "https://x.com/thsottiaux/status/" + pid}


class LocalNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.state)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def active(self):
        notification_candidate(self.store, NOW)
        at = NOW + timedelta(minutes=1)
        update(self.store, [post(at)], at)
        return at

    def limited(self, at, pid="321", prefix=""):
        p = post(at, pid)
        p["text"] = prefix + "Impacted accounts will receive another reset credit for Codex tomorrow."
        return p

    def test_limited_announcement_notifies_with_scope_without_a_global_probability(self):
        notification_candidate(self.store, NOW)
        at = NOW + timedelta(minutes=1)
        snapshot = update(self.store, [self.limited(at)], at)
        self.assertIsNone(snapshot["forecast"])
        candidate = notification_candidate(self.store, at)["candidate"]
        self.assertEqual(candidate["eventId"], "limited-321")
        self.assertIn("受影响用户", candidate["body"])
        self.assertNotIn("probability48h", candidate)
        claim = claim_notification(self.store, candidate["eventId"], at)
        finish_notification(self.store, candidate["eventId"], claim["claimToken"], at)
        self.assertEqual(notification_candidate(self.store, at)["status"], "none")

    def test_limited_history_uncertainty_and_stale_data_never_notify(self):
        notification_candidate(self.store, NOW)
        at = NOW + timedelta(minutes=1)
        update(self.store, [self.limited(NOW - timedelta(minutes=1))], at)
        self.assertEqual(notification_candidate(self.store, at)["status"], "none")
        update(self.store, [self.limited(at, "322", "Maybe ")], at)
        self.assertEqual(notification_candidate(self.store, at)["status"], "none")
        update(self.store, [self.limited(at, "323")], at)
        self.assertEqual(notification_candidate(self.store, at + timedelta(hours=2))["status"], "none")
        self.store.put("lastAttempt", stamp(at + timedelta(minutes=1)))
        self.assertEqual(notification_candidate(self.store, at + timedelta(minutes=1))["status"], "none")

    def test_acknowledged_global_event_does_not_block_a_limited_announcement(self):
        at = self.active()
        claim = claim_notification(self.store, "reset-123", at)
        finish_notification(self.store, "reset-123", claim["claimToken"], at)
        update(self.store, [self.limited(at)], at)
        self.assertEqual(notification_candidate(self.store, at)["candidate"]["eventId"], "limited-321")

    def test_limited_candidate_revalidates_source_and_status_before_claim(self):
        notification_candidate(self.store, NOW)
        at = NOW + timedelta(minutes=1)
        original = update(self.store, [self.limited(at)], at)
        for mutate in [lambda s: s["events"][0].update(status="retracted"),
                       lambda s: s["evidence"][0].update(url="https://evil.example/status/321"),
                       lambda s: s.update(mode="demo")]:
            snapshot = copy.deepcopy(original)
            mutate(snapshot)
            self.store.put("snapshot", snapshot)
            self.assertEqual(claim_notification(self.store, "limited-321", at)["status"], "unavailable")

    def test_first_enable_suppresses_existing_event_across_restart(self):
        update(self.store, [post(NOW)], NOW)
        self.assertEqual(notification_candidate(self.store, NOW)["status"], "none")
        self.store.close()
        self.store = Store(self.state)
        self.assertEqual(notification_candidate(self.store, NOW + timedelta(minutes=1))["status"], "none")
        self.assertEqual(self.store.get(ENABLED_AT), stamp(NOW))

    def test_empty_initial_state_allows_later_new_event_without_rebaselining(self):
        at = self.active()
        result = notification_candidate(self.store, at)
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["candidate"]["eventId"], "reset-123")
        self.assertEqual(result["candidate"]["probability48h"], 85)
        self.assertEqual(self.store.get(ENABLED_AT), stamp(NOW))

    def test_backfilled_old_event_is_suppressed(self):
        notification_candidate(self.store, NOW)
        update(self.store, [post(NOW - timedelta(minutes=1))], NOW + timedelta(minutes=1))
        self.assertEqual(notification_candidate(self.store, NOW + timedelta(minutes=1))["status"], "none")

    def test_event_exactly_at_enable_time_can_qualify_if_not_already_in_baseline(self):
        notification_candidate(self.store, NOW)
        update(self.store, [post(NOW)], NOW)
        self.assertEqual(notification_candidate(self.store, NOW)["status"], "candidate")

    def test_permission_denial_leaves_candidate_available(self):
        at = self.active()
        before = notification_candidate(self.store, at)
        # The native caller does not claim while authorization is denied.
        after = notification_candidate(self.store, at + timedelta(minutes=2))
        self.assertEqual(after["candidate"], before["candidate"])
        self.assertEqual(after["needsReview"], 0)
        self.assertEqual(claim_notification(self.store, "reset-123", at)["status"], "claimed")

    def test_threshold_freshness_and_active_status_are_required(self):
        at = self.active()
        original = self.store.get("snapshot")
        changes = [
            lambda s: s.update(mode="demo"),
            lambda s: s["forecast"].update(probability48h=79),
            lambda s: s.update(checkedAt=stamp(at - timedelta(hours=2))),
            lambda s: s.update(checkedAt=stamp(at + timedelta(seconds=1))),
            lambda s: s["forecast"].update(generatedAt=stamp(at + timedelta(seconds=1))),
            lambda s: s["forecast"].update(validUntil=stamp(at)),
            lambda s: s["forecast"].update(status="confirmed"),
            lambda s: s["events"][0].update(status="retracted"),
            lambda s: s.update(evidence=[]),
        ]
        for i, change in enumerate(changes):
            with self.subTest(case=i):
                snapshot = copy.deepcopy(original)
                change(snapshot)
                self.store.put("snapshot", snapshot)
                self.assertEqual(notification_candidate(self.store, at)["status"], "none")
        original["forecast"]["probability48h"] = 80
        self.store.put("snapshot", original)
        self.assertEqual(notification_candidate(self.store, at)["status"], "candidate")

    def test_claim_rechecks_after_candidate_expires_or_event_is_retracted(self):
        at = self.active()
        notification_candidate(self.store, at)
        self.assertEqual(claim_notification(self.store, "reset-123", at + timedelta(hours=1))["status"], "unavailable")
        snapshot = self.store.get("snapshot")
        snapshot["events"][0]["status"] = "retracted"
        self.store.put("snapshot", snapshot)
        self.assertEqual(claim_notification(self.store, "reset-123", at)["status"], "unavailable")

    def test_later_failed_attempt_blocks_fresh_candidate_and_claim(self):
        at = self.active()
        self.assertEqual(notification_candidate(self.store, at)["status"], "candidate")
        failed_at = at + timedelta(minutes=5)
        self.store.put("lastAttempt", stamp(failed_at))
        result = notification_candidate(self.store, failed_at)
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["reason"], "latest-attempt-not-published")
        self.assertEqual(claim_notification(self.store, "reset-123", failed_at)["status"], "unavailable")
        self.assertEqual(self.store.get("snapshot")["checkedAt"], stamp(at))

    def test_attempt_matching_success_timestamp_allows_candidate_and_claim(self):
        at = self.active()
        self.store.put("lastAttempt", stamp(at))
        self.assertEqual(notification_candidate(self.store, at)["status"], "candidate")
        self.assertEqual(claim_notification(self.store, "reset-123", at)["status"], "claimed")

    def test_claim_rejects_candidate_when_current_forecast_changes_event(self):
        at = self.active()
        notification_candidate(self.store, at)
        snapshot = self.store.get("snapshot")
        snapshot["forecast"]["eventId"] = "reset-124"
        snapshot["events"][0]["id"] = "reset-124"
        snapshot["evidence"][0]["eventId"] = "reset-124"
        self.store.put("snapshot", snapshot)
        self.assertEqual(claim_notification(self.store, "reset-123", at)["status"], "unavailable")

    def test_two_connections_cannot_claim_same_event(self):
        at = self.active()
        notification_candidate(self.store, at)
        barrier = threading.Barrier(2)
        def claim():
            store = Store(self.state)
            try:
                barrier.wait(timeout=5)
                return claim_notification(store, "reset-123", at)["status"]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            self.assertCountEqual([f.result(timeout=10) for f in futures], ["claimed", "unavailable"])

    def test_unacknowledged_claim_survives_restart_and_expiry_without_retry(self):
        at = self.active()
        claim_notification(self.store, "reset-123", at)
        self.store.close()
        self.store = Store(self.state)
        for now in (at, at + timedelta(days=3)):
            result = notification_candidate(self.store, now)
            self.assertEqual(result["status"], "needs-review")
            self.assertEqual(result["needsReview"], 1)
            self.assertIsNone(result["candidate"])

    def test_ack_is_idempotent_and_does_not_redeliver_after_restart(self):
        at = self.active()
        claimed = claim_notification(self.store, "reset-123", at)
        token = claimed["claimToken"]
        for _ in range(2):
            self.assertEqual(finish_notification(self.store, "reset-123", token, at)["status"], "acknowledged")
        self.assertEqual(finish_notification(self.store, "reset-123", token, at, release=True)["status"], "unavailable")
        self.store.close()
        self.store = Store(self.state)
        self.assertEqual(notification_candidate(self.store, at)["status"], "none")

    def test_explicit_enqueue_failure_retries_with_new_token_and_rejects_old_callbacks(self):
        at = self.active()
        first = claim_notification(self.store, "reset-123", at)["claimToken"]
        for _ in range(2):
            self.assertEqual(finish_notification(self.store, "reset-123", first, at, release=True)["status"], "released")
        self.assertEqual(notification_candidate(self.store, at)["status"], "candidate")
        second = claim_notification(self.store, "reset-123", at)["claimToken"]
        self.assertNotEqual(first, second)
        for release in (False, True):
            self.assertEqual(finish_notification(self.store, "reset-123", first, at, release=release)["status"], "unavailable")
        self.assertEqual(finish_notification(self.store, "reset-123", second, at)["status"], "acknowledged")

    def test_actions_do_not_change_collection_or_email_state(self):
        at = self.active()
        self.store.put("lastAttempt", stamp(at))
        self.store.mark_seen(["example-unrelated"])
        self.store.claim("mail-only-event", "submitted", stamp(at))
        snapshot = self.store.get("snapshot")
        mail = self.store.db.execute("SELECT * FROM alerts ORDER BY event_id").fetchall()
        seen = self.store.db.execute("SELECT * FROM seen ORDER BY id").fetchall()
        notification_candidate(self.store, at)
        token = claim_notification(self.store, "reset-123", at)["claimToken"]
        finish_notification(self.store, "reset-123", token, at)
        self.assertEqual(self.store.get("snapshot"), snapshot)
        self.assertEqual(self.store.get("lastAttempt"), stamp(at))
        self.assertEqual(self.store.db.execute("SELECT * FROM alerts ORDER BY event_id").fetchall(), mail)
        self.assertEqual(self.store.db.execute("SELECT * FROM seen ORDER BY id").fetchall(), seen)

    def test_cli_local_actions_never_collect_load_configuration_or_send_mail(self):
        def cli(*args):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--state", str(self.state), "--config", "/unread/config.json", *args])
            self.assertEqual(code, 0)
            return json.loads(output.getvalue())
        with patch("monitor.runner.run") as collect, patch("monitor.runner.load_config") as config, \
             patch("monitor.feeds.urllib.request.urlopen") as network, \
             patch("monitor.mail.MailerLite.request") as mail, \
             patch("monitor.local_notifications.datetime") as clock:
            clock.now.return_value = NOW
            self.assertEqual(cli("--local-notification-candidate")["status"], "none")
            at = NOW + timedelta(minutes=1)
            update(self.store, [post(at)], at)
            clock.now.return_value = at
            self.assertEqual(cli("--local-notification-candidate")["status"], "candidate")
            claimed = cli("--claim-local-notification", "reset-123")
            self.assertEqual(claimed["status"], "claimed")
            self.assertEqual(cli("--ack-local-notification", "reset-123", "--claim-token", claimed["claimToken"])["status"], "acknowledged")
            collect.assert_not_called()
            config.assert_not_called()
            network.assert_not_called()
            mail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
