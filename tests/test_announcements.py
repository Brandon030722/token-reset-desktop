import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor.engine import (ANNOUNCEMENT_RULES_VERSION, RULES_VERSION,
                            announcement_candidates, classify, eligible, update)
from monitor.feeds import stamp
from monitor.local_notifications import notification_candidate
from monitor.store import Store


NOW = datetime(2026, 9, 12, 3, 20, 36, tzinfo=timezone.utc)
POST_ID = "2098612714704891959"
ASTRA_ANNOUNCEMENT = """Hi Astra users. A reset and a quick update on quality issues that have been posted around.

Working with some of you, we have found and fixed the following issues:
- Some skills written for previous models were triggering too often or preventing the model from checking its work.
- An opt-in context management experiment that could cause early stops or replies to older messages. We've disabled it. Our rough estimate is that 4-5k users were affected by this experiment.
- We've also removed some badly configured engines that resulted in a measured quality degradation for a long tail of traffic flowing through them.

We’ve also made some more minor improvements and things should feel significantly better across the board. More consistent follow-through, better tracking of your latest message, and better checks on the work as it’s going through the motions.

The examples posted and all the users who worked directly with us were incredibly useful in helping fix things quickly. Always grateful for this incredible community.

And of course, a reset is also landing by midnight today."""
COMPENSATION = ("There was a bit of a kerfuffle this morning with some banked resets not fully applying "
                "when used in ChatGPT Work and Codex. Everyone who used one in the affected time window "
                "is getting another one and an email to apologize.")


def post(text=ASTRA_ANNOUNCEMENT, pid=POST_ID, at=NOW):
    return {"id": pid, "text": text, "postedAt": stamp(at),
            "url": "https://x.com/thsottiaux/status/" + pid}


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def active(self, text=ASTRA_ANNOUNCEMENT):
        update(self.store, [], NOW - timedelta(minutes=30))
        notification_candidate(self.store, NOW - timedelta(minutes=30))
        return update(self.store, [post(text)], NOW)

    def test_real_astra_announcement_has_no_invented_probability_or_timezone(self):
        snapshot = self.active()
        self.assertEqual(classify(ASTRA_ANNOUNCEMENT), ("context", 0, 0))
        self.assertIsNone(snapshot["forecast"])
        self.assertEqual(snapshot["history"], [])
        self.assertFalse(eligible(snapshot, NOW))
        candidates = announcement_candidates(snapshot, NOW)
        self.assertEqual(len(candidates), 1)
        event, source = candidates[0]["event"], candidates[0]["source"]
        self.assertEqual(source["text"], ASTRA_ANNOUNCEMENT)
        self.assertEqual(event["title"], "Astra 重置公告")
        self.assertEqual(event["status"], "promised")
        self.assertTrue(event["announcement"])
        self.assertIn("Astra 用户", event["scope"])
        self.assertIn("具体套餐和适用资格未说明", event["scope"])
        self.assertNotIn("expectedAt", event)
        self.assertNotIn("confirmedAt", event)
        self.assertIsNone(self.store.alert(event["id"]))
        self.assertEqual(notification_candidate(self.store, NOW)["status"], "candidate")

    def test_passive_and_separated_product_references_are_announcements(self):
        for text in (
            "Hi Astra users. A reset is also landing by midnight today.",
            "Hey Codex users. The reset will be done by midnight today.",
            "Codex users: a reset is coming tonight.",
            "A Codex reset is landing by midnight.",
            "Hi Astra users. We will reset usage limits tonight.",
            "Hi Astra users. The reset is now complete.",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(text), ("context", 0, 0))

    def test_ambiguous_requests_other_products_quotes_and_cancellation_do_not_announce(self):
        for text in (
            "A reset is landing by midnight today.",
            "Hi Astra users. A reset and a quick update on quality issues.",
            "Hi Astra users. A reset is landing soon.",
            "Hi Astra users. Maybe a reset is landing by midnight today.",
            "Hi Astra users. If capacity permits a reset is landing by midnight today.",
            "Hi Astra users. A reset is landing by midnight today?",
            "Hi Astra users. Someone said a reset is landing by midnight today.",
            'Hi Astra users. "A reset is landing by midnight today."',
            '"Hi Astra users." A reset is landing by midnight today.',
            "Hi Astra users. A password reset is landing by midnight today.",
            "Hi Astra users. A reset of conversation sessions is landing by midnight today.",
            "Hi Astra users. A ChatGPT reset is landing by midnight today.",
            "Hi Astra users. A Sora reset is landing by midnight today.",
            "Hi Astra users. A reset is landing by midnight today. Update: cancelled.",
            "Hi Astra users. A reset is landing by midnight today. No reset after all.",
        ):
            with self.subTest(text=text):
                result = classify(text)
                self.assertNotEqual(result, ("context", 0, 0))

    def test_global_prediction_rules_are_unchanged(self):
        text = "We will reset Codex limits for everyone tonight."
        snapshot = self.active(text)
        self.assertEqual(RULES_VERSION, "rules-v2")
        self.assertEqual(classify(text), ("promise", 80, 85))
        self.assertTrue(eligible(snapshot, NOW))
        self.assertEqual(announcement_candidates(snapshot, NOW), [])

    def test_direct_announcement_does_not_consume_existing_global_event(self):
        self.active("We will reset Codex limits for everyone tonight.")
        snapshot = update(self.store, [post(pid="2098612714704891960", at=NOW + timedelta(minutes=1))],
                          NOW + timedelta(minutes=1))
        self.assertEqual(len(snapshot["events"]), 2)
        self.assertTrue(eligible(snapshot, NOW + timedelta(minutes=1)))
        self.assertEqual(len(announcement_candidates(snapshot, NOW + timedelta(minutes=1))), 1)

    def test_first_run_preserves_archive_without_sending_old_announcements(self):
        snapshot = update(self.store, [post()], NOW)
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertTrue(snapshot["events"][0]["notificationSuppressed"])
        self.assertEqual(announcement_candidates(snapshot, NOW), [])
        snapshot = update(self.store, [post()], NOW + timedelta(minutes=15))
        self.assertEqual(announcement_candidates(snapshot, NOW + timedelta(minutes=15)), [])

    def test_seen_missed_post_recovers_once_without_resetting_any_delivery_ledger(self):
        update(self.store, [], NOW - timedelta(minutes=30))
        self.store.put("announcementRecoveryVersion", "old")
        self.store.mark_seen([POST_ID])
        self.store.claim("old-delivery", "submitted", stamp(NOW))
        self.store.claim("limited-" + POST_ID, "scope-excluded", stamp(NOW))
        for minutes in (0, 15, 30):
            now = NOW + timedelta(minutes=minutes)
            snapshot = update(self.store, [post()], now)
            self.assertEqual(len(snapshot["events"]), 1)
            self.assertEqual(len(snapshot["evidence"]), 1)
            self.assertEqual(len(announcement_candidates(snapshot, now)), 1)
        self.assertEqual(self.store.get("announcementRecoveryVersion"), ANNOUNCEMENT_RULES_VERSION)
        self.assertEqual(self.store.alert("old-delivery")["status"], "submitted")
        self.assertEqual(self.store.alert("limited-" + POST_ID)["status"], "scope-excluded")

    def test_limited_compensation_is_also_a_candidate_without_new_scope_excluded_claim(self):
        snapshot = self.active(COMPENSATION)
        candidates = announcement_candidates(snapshot, NOW)
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("announcement", candidates[0]["event"])
        self.assertIsNone(self.store.alert("limited-" + POST_ID))

    def test_old_future_or_stale_snapshots_cannot_notify(self):
        snapshot = self.active()
        for now in (NOW - timedelta(seconds=1), NOW + timedelta(hours=1, seconds=1)):
            self.assertEqual(announcement_candidates(snapshot, now), [])
        now = NOW + timedelta(hours=24)
        snapshot = update(self.store, [post()], now)
        self.assertEqual(announcement_candidates(snapshot, now), [])

    def test_old_recovered_post_remains_archived_without_a_catchup_email(self):
        update(self.store, [], NOW - timedelta(minutes=30))
        self.store.put("announcementRecoveryVersion", "old")
        self.store.mark_seen([POST_ID])
        now = NOW + timedelta(days=2)
        snapshot = update(self.store, [post()], now)
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(announcement_candidates(snapshot, now), [])

    def test_every_candidate_revalidates_original_source_and_event(self):
        original = self.active()
        changes = (
            lambda s: s.update(mode="demo"),
            lambda s: s["events"][0].update(reviewRequired=True),
            lambda s: s["events"][0].update(notificationSuppressed=True),
            lambda s: s["events"][0].update(status="confirmed"),
            lambda s: s["events"][0].update(scope="All users"),
            lambda s: s["events"][0].update(announcedAt=stamp(NOW - timedelta(minutes=1))),
            lambda s: s["events"][0].update(evidenceIds=["post-" + POST_ID] * 2),
            lambda s: s["events"][0].pop("announcement"),
            lambda s: s["evidence"].append(copy.deepcopy(s["evidence"][0])),
            lambda s: s["evidence"][0].update(kind="hint"),
            lambda s: s["evidence"][0].update(author="@someoneelse"),
            lambda s: s["evidence"][0].update(url="https://x.com/someoneelse/status/" + POST_ID),
            lambda s: s["evidence"][0].update(url="https://x.com/thsottiaux/status/999"),
            lambda s: s["evidence"][0].update(text="Hi Astra users. Maybe a reset is landing tonight."),
            lambda s: s["evidence"][0].update(text="Hi Codex users. A reset is landing tonight."),
        )
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                snapshot = copy.deepcopy(original)
                change(snapshot)
                self.assertEqual(announcement_candidates(snapshot, NOW), [])

    def test_same_id_source_edit_suspends_automatic_announcement(self):
        self.active()
        snapshot = update(self.store, [post(text="Hi Astra users. A reset is coming tomorrow.")], NOW)
        self.assertTrue(snapshot["events"][0]["reviewRequired"])
        self.assertEqual(announcement_candidates(snapshot, NOW), [])


if __name__ == "__main__":
    unittest.main()
