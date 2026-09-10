"""Offline macOS notification outbox, separate from MailerLite delivery records.

Read candidates before requesting a claim. Check OS permission before claiming;
ack only after the OS accepts the request. Release only when the OS explicitly
reports that enqueueing failed. A lost claim/ack response must not be retried as
another delivery: outstanding claims remain visible for manual review.
"""
import contextlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .engine import classify, eligible
from .feeds import date, stamp
from .store import Store, process_lock

ENABLED_AT = "localNotificationEnabledAt"


@contextlib.contextmanager
def _transaction(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS local_notifications (
        event_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('suppressed','pending','claimed','acknowledged')),
        claim_token TEXT, updated_at TEXT NOT NULL, last_error TEXT
    )""")
    store.db.commit()
    store.db.execute("BEGIN IMMEDIATE")
    try:
        yield
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def _review_count(store):
    return store.db.execute("SELECT count(*) FROM local_notifications WHERE status='claimed'").fetchone()[0]


def _initialize(store, now):
    if store.get(ENABLED_AT) is not None:
        return False
    # Commit the baseline and existing-event suppression together. Do not use
    # Store.put here: it commits and would split this transaction.
    store.db.execute("INSERT INTO meta VALUES (?,?)", (ENABLED_AT, json.dumps(stamp(now))))
    snapshot = store.get("snapshot") or {}
    for event in snapshot.get("events", []):
        store.db.execute("INSERT OR IGNORE INTO local_notifications VALUES (?,'suppressed',NULL,?,NULL)",
                         (event["id"], stamp(now)))
    for item in store.get("weeklyDue", []):
        store.db.execute("INSERT OR IGNORE INTO local_notifications VALUES (?,'suppressed',NULL,?,NULL)",
                         (item["eventId"], stamp(now)))
    return True


def _current_candidate(store, now):
    if store.get("weeklySnapshot") is not None:
        from .codex_usage import current_candidate
        try: return current_candidate(store, now)
        except (KeyError, TypeError, ValueError, OverflowError): return None, "invalid-weekly-snapshot"
    snapshot = store.get("snapshot")
    try:
        if not snapshot or snapshot.get("mode") != "live" or not (
            timedelta(0) <= now - date(snapshot["checkedAt"]) <= timedelta(hours=1)
        ):
            return None, "not-eligible"
        last_attempt = store.get("lastAttempt")
        if last_attempt and date(last_attempt) > date(snapshot["checkedAt"]):
            return None, "latest-attempt-not-published"
        candidates = []
        if eligible(snapshot, now):
            forecast = snapshot["forecast"]
            event = next(e for e in snapshot["events"] if e["id"] == forecast["eventId"])
            source = next(e for e in snapshot["evidence"] if e["id"] in forecast["evidenceIds"])
            candidates.append((event, {
                "eventId": event["id"], "title": "Codex 重置线索达到提醒线",
                "body": f"重置信号评分 {forecast['probability48h']:g}/100（不是发生概率）。打开 Token重置查看依据。",
                "probability48h": forecast["probability48h"], "validUntil": forecast["validUntil"],
                "windowEndsAt": forecast["windowEndsAt"], "sourceUrl": source["url"],
            }))
        for event in snapshot["events"]:
            if event.get("reviewRequired") or event["type"] != "limited-reset" or event["status"] not in ("promised", "confirmed"):
                continue
            if not (timedelta(0) <= now - date(event["announcedAt"]) < timedelta(hours=48)):
                continue
            sources = [e for e in snapshot["evidence"] if e["id"] in event["evidenceIds"]
                       and e["eventId"] == event["id"] and e["kind"] == "context" and classify(e["text"]) == ("context", 0, 0)
                       and re.fullmatch(r"https://x\.com/thsottiaux/status/\d+", e["url"])
                       and date(e["postedAt"]) <= now]
            if not sources:
                continue
            candidates.append((event, {
                "eventId": event["id"], "title": "Codex 小范围重置公告",
                "body": event["scope"] + (" 原帖宣布完成，请自行核对。" if event["status"] == "confirmed" else " 原帖已发布预告，请自行核对到账。"),
                "sourceUrl": sources[0]["url"],
            }))
        reason = "not-eligible"
        for event, candidate in candidates:
            announced = date(event["announcedAt"])
            if announced > now:
                reason = "future-announcement"
                continue
            if announced < date(store.get(ENABLED_AT)):
                store.db.execute("INSERT OR IGNORE INTO local_notifications VALUES (?,'suppressed',NULL,?,NULL)",
                                 (event["id"], stamp(now)))
                reason = "predates-enabling"
                continue
            row = store.db.execute("SELECT status FROM local_notifications WHERE event_id=?", (event["id"],)).fetchone()
            if row and row[0] != "pending":
                reason = row[0]
                continue
            return candidate, None
        return None, reason
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration, OverflowError):
        return None, "invalid-snapshot"


def _prepare(store, now):
    if _initialize(store, now):
        return None, "baseline-established"
    candidate, reason = _current_candidate(store, now)
    if candidate is None:
        return None, reason
    store.db.execute("INSERT OR IGNORE INTO local_notifications VALUES (?,'pending',NULL,?,NULL)",
                     (candidate["eventId"], stamp(now)))
    status = store.db.execute("SELECT status FROM local_notifications WHERE event_id=?",
                             (candidate["eventId"],)).fetchone()[0]
    return (candidate, None) if status == "pending" else (None, status)


def notification_candidate(store, now):
    """Establish the first-use baseline, or return an unconsumed eligible event."""
    with _transaction(store):
        candidate, reason = _prepare(store, now)
        review = _review_count(store)
        return {"status": "candidate" if candidate else "needs-review" if review else "none",
                "candidate": candidate, "needsReview": review, "reason": reason}


def claim_notification(store, event_id, now):
    with _transaction(store):
        # Re-read eligibility under the same write transaction as the claim:
        # permission dialogs can outlive a forecast or its event.
        candidate, reason = _prepare(store, now)
        if not candidate or candidate["eventId"] != event_id:
            return {"status": "unavailable", "candidate": None,
                    "reason": reason or "event-changed", "needsReview": _review_count(store)}
        token = str(uuid.uuid4())
        changed = store.db.execute(
            "UPDATE local_notifications SET status='claimed',claim_token=?,updated_at=?,last_error=NULL "
            "WHERE event_id=? AND status='pending'", (token, stamp(now), event_id))
        if changed.rowcount != 1:
            return {"status": "unavailable", "candidate": None, "reason": "already-claimed",
                    "needsReview": _review_count(store)}
        return {"status": "claimed", "candidate": candidate, "claimToken": token,
                "needsReview": _review_count(store)}


def finish_notification(store, event_id, token, now, *, release=False):
    with _transaction(store):
        row = store.db.execute("SELECT status,claim_token,last_error FROM local_notifications WHERE event_id=?",
                               (event_id,)).fetchone()
        target, success = ("pending", "released") if release else ("acknowledged", "acknowledged")
        # Keep the token on completion so repeating the same successful ack or
        # release is idempotent. A later claim replaces it, rejecting old callbacks.
        repeat = row and row[0] == target and (not release or row[2] == "enqueue-failed")
        if not token or not row or row[1] != token or (row[0] != "claimed" and not repeat):
            return {"status": "unavailable", "eventId": event_id, "reason": "claim-mismatch",
                    "needsReview": _review_count(store)}
        if not repeat:
            store.db.execute("UPDATE local_notifications SET status=?,updated_at=?,last_error=? "
                             "WHERE event_id=? AND claim_token=? AND status='claimed'",
                             (target, stamp(now), "enqueue-failed" if release else None, event_id, token))
        return {"status": success, "eventId": event_id, "needsReview": _review_count(store)}


def run_local_notification(action, state, event_id=None, claim_token=None, now=None):
    """Short-lived helper entry; never loads network or email configuration."""
    now = now or datetime.now(timezone.utc)
    state = Path(state).resolve()
    with process_lock(state.with_suffix(".lock")):
        store = Store(state)
        try:
            if action == "candidate":
                return notification_candidate(store, now)
            if action == "claim":
                return claim_notification(store, event_id, now)
            if action in ("ack", "release"):
                if not claim_token:
                    raise ValueError("A claim token is required")
                return finish_notification(store, event_id, claim_token, now, release=action == "release")
            raise ValueError("Unknown local notification action")
        finally:
            store.close()
