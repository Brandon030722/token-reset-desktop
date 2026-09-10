"""Brevo campaign delivery for a dedicated, confirmed subscription list."""
import json
import math
import re
import urllib.request
from time import monotonic
from datetime import timedelta
from .engine import eligible
from .feeds import stamp
from .email_design import forecast_email


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("API redirects are not allowed")


class Brevo:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None):
        req = urllib.request.Request("https://api.brevo.com/v3" + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"api-key": self.token, "Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Oversized API response")
        return json.loads(raw) if raw else {}


def render_brevo(snapshot):
    return forecast_email(snapshot)


def readiness(client, config):
    details = client.request("GET", "/contacts/lists/" + config["listId"])
    count = details.get("totalSubscribers")
    if type(count) is not int or count < 0 or details.get("id") != int(config["listId"]):
        raise ValueError("Invalid subscription list")
    if count == 0:
        return "empty-list"
    limit = config.get("maxRecipients", 100)
    if type(limit) is not int or not 1 <= limit <= 300 or count > limit:
        return "audience-limit"
    plans = client.request("GET", "/account").get("plan", [])
    free = [p.get("credits") for p in plans if p.get("type") == "free" and p.get("creditsType") == "sendLimit"]
    # This integration is intentionally limited to the user's free-plan scope.
    if len(free) != 1 or type(free[0]) not in (int, float) or not math.isfinite(free[0]):
        return "free-plan-unverified"
    if free[0] < count:
        return "quota-limited"
    return "ready"


def dispatch_brevo(store, snapshot, now, config, client, checkpoint=lambda: None):
    started = monotonic()
    if not eligible(snapshot, now):
        return "not-eligible"
    event_id = snapshot["forecast"]["eventId"]
    prior = store.alert(event_id)
    if prior:
        return "needs-review" if prior["status"] in ("creating", "created", "scheduling") else prior["status"]
    if not re.fullmatch(r"[1-9]\d*", config.get("listId", "")):
        raise ValueError("Dedicated Brevo list ID required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", config.get("fromEmail", "")):
        raise ValueError("Verified sender email required")
    if config.get("optInConfirmed") is not True:
        raise ValueError("Verify the dedicated double-opt-in form before enabling sending")
    ready = readiness(client, config)
    if ready != "ready":
        return ready
    if not store.claim(event_id, "creating", stamp(now)):
        return "already-claimed"
    checkpoint()
    campaign_id = None
    try:
        campaign = client.request("POST", "/emailCampaigns", {
            "name": "token-reset:" + event_id, "type": "classic",
            "sender": {"name": "Token重置", "email": config["fromEmail"]},
            "subject": f'Codex 重置观察：信号评分 {snapshot["forecast"]["probability48h"]}/100',
            "htmlContent": render_brevo(snapshot),
            "recipients": {"listIds": [int(config["listId"])]},
        })
        campaign_id = str(campaign.get("id", ""))
        if not re.fullmatch(r"[1-9]\d*", campaign_id):
            raise ValueError("Missing campaign ID")
        store.set_alert(event_id, "created", campaign_id, stamp(now)); checkpoint()
        verified = client.request("GET", "/emailCampaigns/" + campaign_id)
        audience = verified.get("recipients", {})
        if (verified.get("status") != "draft" or audience.get("lists") != [int(config["listId"])]
                or audience.get("segments") or audience.get("exclusionLists") or audience.get("excludedSegments")
                or verified.get("sender", {}).get("email", "").lower() != config["fromEmail"].lower()):
            raise ValueError("Campaign audience or sender changed")
        if readiness(client, config) != "ready":
            raise ValueError("Quota or audience changed; review the unsent draft")
        store.set_alert(event_id, "scheduling", campaign_id, stamp(now)); checkpoint()
        if not eligible(snapshot, now + timedelta(seconds=monotonic() - started)):
            raise ValueError("Forecast expired before sending; review the unsent draft")
        client.request("POST", "/emailCampaigns/" + campaign_id + "/sendNow")
        store.set_alert(event_id, "submitted", campaign_id, stamp(now)); checkpoint()
        return "submitted"
    except Exception:
        store.set_alert(event_id, "needs-review", campaign_id, stamp(now)); checkpoint()
        raise
