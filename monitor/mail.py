import html
import json
import re
import urllib.request
from time import monotonic
from datetime import timedelta
from .engine import eligible
from .feeds import stamp


def render(snapshot):
    f = snapshot["forecast"]
    evidence = [e for e in snapshot["evidence"] if e["id"] in f["evidenceIds"]]
    links = "".join(f'<li><a href="{html.escape(e["url"], quote=True)}">'
                    f'{html.escape(e["postedAt"])}</a>：{html.escape(e["summary"])}</li>' for e in evidence)
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body>'
        '<h1>Tibo 观察站：重置线索达到提醒线</h1>'
        f'<p>未来 48 小时规则评分：<strong>{f["probability48h"]}/100</strong>。</p>'
        '<p>这是未经统计校准的实验性评分，不保证发生。适用套餐和实际完成情况请以原帖为准。</p>'
        f'<p>本次窗口最晚结束于 {html.escape(f["windowEndsAt"])}（UTC）。</p>'
        f'<ul>{links}</ul><p>同一事件仅提醒一次。你收到本信是因为已确认订阅重置提醒。</p>'
        '<p>{$account} · {$address} · {$country}</p>'
        '<p><a href="{$unsubscribe}">退订提醒</a></p></body></html>'
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API redirects are not allowed")


class MailerLite:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("https://connect.mailerlite.com/api" + path, data=body, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json", "Accept": "application/json"})
        # No automatic retry: a timeout may occur AFTER a campaign was created/scheduled.
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Oversized API response")
        return json.loads(raw) if raw else {}


def dispatch(store, snapshot, now, config, client, checkpoint=lambda: None):
    """Durable at-most-once submission; uncertainty needs operator reconciliation."""
    started = monotonic()
    if not eligible(snapshot, now):
        return "not-eligible"
    event_id = snapshot["forecast"]["eventId"]
    if store.alert(event_id):
        return "needs-review" if store.alert(event_id)["status"] in ("creating", "created", "scheduling") else store.alert(event_id)["status"]
    if not re.fullmatch(r"\d+", config.get("groupId", "")):
        raise ValueError("A dedicated confirmed-opt-in MailerLite group ID is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", config.get("fromEmail", "")):
        raise ValueError("Verified MailerLite sender address required")
    if not config.get("optInConfirmed"):
        raise ValueError("Configure and verify double opt-in before enabling mail")
    if not store.claim(event_id, "creating", stamp(now)):
        return "already-claimed"
    # In Actions this pushes the claim BEFORE any external side effect.
    checkpoint()
    campaign_id = None
    try:
        result = client.request("POST", "/campaigns", {
            "name": "tibo-reset:" + event_id, "type": "regular",
            "groups": [config["groupId"]],
            "emails": [{"subject": f'Codex 重置观察：信号评分 {snapshot["forecast"]["probability48h"]}/100',
                        "from_name": "Tibo 观察站", "from": config["fromEmail"], "content": render(snapshot)}],
        })
        campaign = result.get("data", {})
        campaign_id = str(campaign.get("id", ""))
        if not re.fullmatch(r"\d+", campaign_id):
            raise ValueError("Missing campaign ID")
        store.set_alert(event_id, "created", campaign_id, stamp(now))
        checkpoint()
        # Verify the effective audience before scheduling; never fall back to all subscribers.
        verified = client.request("GET", "/campaigns/" + campaign_id).get("data", {})
        audience = verified.get("filter")
        expected = [[{"operator": "in_any", "args": ["groups", [config["groupId"]]]}]]
        if audience != expected or verified.get("status") != "draft" or not verified.get("can_be_scheduled"):
            raise ValueError("Campaign audience/content/sender is not ready")
        store.set_alert(event_id, "scheduling", campaign_id, stamp(now))
        checkpoint()
        if not eligible(snapshot, now + timedelta(seconds=monotonic() - started)):
            raise ValueError("Forecast expired before sending; review the unsent draft")
        client.request("POST", "/campaigns/" + campaign_id + "/schedule", {"delivery": "instant"})
        store.set_alert(event_id, "submitted", campaign_id, stamp(now))
        checkpoint()
        return "submitted"  # Accepted for scheduling, NOT proof of inbox delivery.
    except Exception:
        store.set_alert(event_id, "needs-review", campaign_id, stamp(now))
        checkpoint()
        raise
