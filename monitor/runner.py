import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .engine import eligible, update
from .feeds import FeedUnavailable, date, fetch_feeds, stamp
from .mail import MailerLite, dispatch, render
from .brevo import Brevo, dispatch_brevo, render_brevo
from .local_notifications import run_local_notification
from .store import Store, atomic_json, process_lock

DEFAULT_FEEDS = ["https://fxtwitter.com/thsottiaux/feed.xml?count=100&with_replies=1"]


def load_config(path):
    config_file = Path(path) if str(path).strip() else None
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file is not None and config_file.exists() else {}
    feeds = os.environ.get("TIBO_FEED_URLS")
    config["feeds"] = json.loads(feeds) if feeds else config.get("feeds", DEFAULT_FEEDS)
    if not isinstance(config["feeds"], list) or not 1 <= len(config["feeds"]) <= 3 or not all(isinstance(x, str) for x in config["feeds"]):
        raise ValueError("Configure 1–3 public RSS/Atom feed URLs")
    legacy = bool(config.get("groupId") or os.environ.get("MAILERLITE_GROUP_ID"))
    config["mailProvider"] = os.environ.get("TIBO_MAIL_PROVIDER") or config.get("mailProvider", "mailerlite" if legacy else "brevo")
    if config["mailProvider"] not in ("brevo", "mailerlite"):
        raise ValueError("Unsupported mail provider")
    prefix = "BREVO" if config["mailProvider"] == "brevo" else "MAILERLITE"
    config["fromEmail"] = os.environ.get(prefix + "_FROM_EMAIL") or config.get("fromEmail", "")
    config["optInConfirmed"] = (os.environ.get(prefix + "_OPT_IN_CONFIRMED") or str(config.get("optInConfirmed", False))).lower() == "true"
    if prefix == "BREVO":
        config["listId"] = os.environ.get("BREVO_LIST_ID") or str(config.get("listId", ""))
    else:
        config["groupId"] = os.environ.get("MAILERLITE_GROUP_ID") or str(config.get("groupId", ""))
    return config


def run(config_path="monitor.config.json", state=".local/state.sqlite3",
        output="public/data", dry_run=False, git_checkpoint=False):
    config = load_config(config_path)
    now = datetime.now(timezone.utc)
    state, output = Path(state).resolve(), Path(output).resolve()
    if git_checkpoint:
        repo = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
        # Only these public-data paths are eligible for automatic commits.
        if state != repo / ".monitor/state.sqlite3" or output != repo / "public/data":
            raise ValueError("Git checkpoint requires repository .monitor/state.sqlite3 and public/data")
        if dry_run:
            raise ValueError("Dry-run cannot commit or send")
    with process_lock(state.with_suffix(".lock")):
        store = Store(state)
        try:
            def checkpoint():
                if not git_checkpoint:
                    return
                subprocess.run(["git", "add", "--", ".monitor/state.sqlite3", "public/data"], check=True)
                diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", ".monitor/state.sqlite3", "public/data"], check=False)
                if diff.returncode == 1:
                    subprocess.run(["git", "commit", "--only", "-m", "chore: persist monitor state", "--", ".monitor/state.sqlite3", "public/data"], check=True)
                    subprocess.run(["git", "push", "origin", "HEAD"], check=True)
                elif diff.returncode:
                    raise RuntimeError("Could not verify checkpoint")

            last_attempt = store.get("lastAttempt")
            if last_attempt and now - date(last_attempt) < timedelta(minutes=15):
                return {"status": "cooldown", "nextCheckAt": stamp(date(last_attempt) + timedelta(minutes=15))}
            # Persist an attempt even on network failure, preventing rapid retry loops.
            store.put("lastAttempt", stamp(now))
            try:
                posts, source, failures = fetch_feeds(config["feeds"], now, store.get("lastTimelinePostAt"))
            except FeedUnavailable as exc:
                atomic_json(output / "health.json", {"status": "unavailable", "attemptedAt": stamp(now), "failures": exc.failures})
                checkpoint()
                return {"status": "source-unavailable", "failures": exc.failures}
            snapshot = update(store, posts, now)
            store.put("lastTimelinePostAt", max((p["postedAt"] for p in posts), key=date))
            atomic_json(output / "snapshot.json", snapshot)
            atomic_json(output / "health.json", {
                "status": "ok", "attemptedAt": stamp(now), "source": source, "posts": len(posts), "failures": failures,
                "note": "镜像成功响应不保证时间线完整；按规则判断，非官方确认。",
            })
            checkpoint()
            result = "below-threshold"
            if eligible(snapshot, now):
                draft = state.parent / "draft.html"
                draft.write_text(render_brevo(snapshot) if config["mailProvider"] == "brevo" else render(snapshot), encoding="utf-8")
                result = "draft-only"
                enabled = os.environ.get("TIBO_SEND_EMAIL", str(config.get("sendEmail", False))).lower() == "true"
                if enabled and not dry_run:
                    try:
                        if config["mailProvider"] == "brevo":
                            token = os.environ.get("BREVO_API_KEY") or config.get("apiKey", "")
                            if not token:
                                raise ValueError("Missing BREVO_API_KEY")
                            result = dispatch_brevo(store, snapshot, now, config, Brevo(token), checkpoint)
                        else:
                            token = os.environ.get("MAILERLITE_API_KEY") or config.get("apiKey", "")
                            if not token:
                                raise ValueError("Missing MAILERLITE_API_KEY")
                            result = dispatch(store, snapshot, now, config, MailerLite(token), checkpoint)
                    except Exception as exc:
                        # A mail failure must not hide successfully collected data or
                        # prevent the separate local-notification outbox from running.
                        result = "needs-review" if store.alert(snapshot["forecast"]["eventId"]) else "mail-failed"
                        store.put("lastMailError", {"at": stamp(now), "reason": type(exc).__name__, "status": result})
            return {"status": "ok", "posts": len(posts), "notification": result, "source": source}
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tibo public-feed monitor (minimum interval: 15 minutes)")
    parser.add_argument("--config", default="monitor.config.json")
    parser.add_argument("--state", default=".local/state.sqlite3")
    parser.add_argument("--output", default="public/data")
    parser.add_argument("--once", action="store_true", help="One poll, then exit (default)")
    parser.add_argument("--dry-run", action="store_true", help="Collect locally, export draft; never send or commit")
    parser.add_argument("--git-checkpoint", action="store_true", help="Actions only: push durable state before mail")
    parser.add_argument("--read-codex-usage", action="store_true", help="Read local Codex weekly quota; never sends email")
    parser.add_argument("--weekly-email", choices=['on', 'off'], help="Enable/cancel the configured private cloud schedule")
    parser.add_argument("--weekly", action="store_true", help="Use the private weekly-notification outbox")
    local = parser.add_mutually_exclusive_group()
    local.add_argument("--local-notification-candidate", action="store_true", help="Read local notification candidate without collecting")
    local.add_argument("--claim-local-notification", metavar="EVENT_ID", help="Claim a local notification after OS permission is granted")
    local.add_argument("--ack-local-notification", metavar="EVENT_ID", help="Confirm the OS accepted a claimed notification")
    local.add_argument("--release-local-notification", metavar="EVENT_ID", help="Release only after the OS explicitly rejected enqueueing")
    parser.add_argument("--claim-token", help="Token returned by a successful local notification claim")
    args = parser.parse_args(argv)
    try:
        action = ("candidate" if args.local_notification_candidate else
                  "claim" if args.claim_local_notification else
                  "ack" if args.ack_local_notification else
                  "release" if args.release_local_notification else None)
        if args.read_codex_usage or args.weekly_email:
            if action or args.weekly or args.git_checkpoint:
                raise ValueError("Weekly usage cannot be combined with notification or cloud actions")
            from .codex_usage import refresh
            if args.weekly_email:
                path = Path(args.config)
                private = json.loads(path.read_text())
                settings = private.get('weeklyCloud', {})
                if not settings.get('repository') or not settings.get('recipient'):
                    raise ValueError('Private weekly cloud configuration required')
                settings['enabled'] = args.weekly_email == 'on'
                private['weeklyCloud'] = settings
                atomic_json(path, private); path.chmod(0o600)
            result = refresh(load_config(args.config), Path(args.state).resolve().parent)
        elif action:
            event_id = args.claim_local_notification or args.ack_local_notification or args.release_local_notification
            notification_state = Path(args.state).with_name("codex-usage.sqlite3") if args.weekly else args.state
            result = run_local_notification(action, notification_state, event_id, args.claim_token)
        else:
            if args.weekly: raise ValueError("Weekly flag requires a notification action")
            if args.claim_token:
                raise ValueError("Claim token requires a local notification action")
            result = run(args.config, args.state, args.output, args.dry_run, args.git_checkpoint)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["status"] == "source-unavailable" or result.get("notification") in ("needs-review", "mail-failed") else 0
    except Exception as exc:
        # Avoid printing HTTP response contents or environment variables.
        print(json.dumps({"status": "failed", "reason": type(exc).__name__,
                          "action": ("核查本地通知状态；领取或投递结果不明时，不要自动重发。" if action else
                                     "检查来源、配置或邮件 campaign；不自动重试不确定的发送。")}, ensure_ascii=False))
        return 1
