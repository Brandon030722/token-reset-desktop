"""Conservative, explainable rules. Scores are NOT calibrated probabilities."""
import re
from datetime import timedelta
from .feeds import date, stamp


def _limited_compensation(text):
    """A replacement for an incident cohort is not a new global reset."""
    limited = re.search(
        r"\b(?:affected|impacted)\s+(?:users|accounts|customers|people|time (?:window|period))\b"
        r"|\b(?:users|accounts|customers|people)\s+(?:who (?:were |are )?)?(?:affected|impacted)\b",
        text,
    )
    replacement = re.search(
        r"\b(?:compensat(?:e|ed|ing|ion)|refund(?:s|ed|ing)?|reimburse(?:d|ment)?|replacements?)\b"
        r"|\b(?:getting|receiv(?:e|ed|ing))\s+(?:another|a replacement)\s+(?:one|reset|credit)\b",
        text,
    )
    if not limited or not replacement:
        return False
    # Keep a separate, explicit global action even when the same post also
    # discusses compensation. "All affected users" does not qualify.
    action = (r"\b(?:we will|we'll|we are going to|we're going to|we have|we've|we just|we)\s+"
              r"(?:(?:also|once again)\s+)?(?:reset|be resetting)\b")
    audience = (r"\b(?:for\s+(?:all\s+(?:our\s+)?(?:paid\s+)?(?:codex\s+)?"
                r"(?:users|accounts|subscribers)|everyone|everybody)|all\s+(?:paid\s+)?codex\s+users)\b"
                r"(?!\s+(?:who|that|affected|impacted|in\s+(?:the\s+)?(?:affected|impacted)))")
    return not re.search(action + r"[^.!?\n]{0,160}" + audience, text)


RULES_VERSION = "rules-v2"
ANNOUNCEMENT_RULES_VERSION = "announcements-v1"
# These are ordinal policy scores, not probabilities learned from outcomes.
# A promise can cross 80 only if all four hard gates are met in one clause:
# Codex reset action, explicit commitment, broad audience, bounded time.
ACTION = r"\b(?:we will|we'll|we are going to|we're going to)\s+(?:(?:also|once again)\s+)?(?:reset|be resetting)\b"
COMPLETED = r"\b(?:we have reset|we've reset|we just reset|we reset|have been reset|has been reset|are now reset)\b"
WINDOW = r"\b(?:today|tomorrow|tonight|(?:in|within|next)\s+(?:24|48)\s+hours)\b"
HEDGED = r"\b(?:quote|rumou?r|someone said|reportedly|if|might|could|maybe|may|would|unless|provided)\b"
NON_QUOTA = r"\b(?:settings?|passwords?|repositories|repository|configs?|configuration|sessions?|conversations?|branches|environment|test data)\b"
NEGATED = r"\b(?:won't|will not|not going to|cancelled|canceled|no reset|not reset|not happening|no longer)\b"


def _clauses(text):
    t = text.lower().replace("’", "'")
    t = re.sub(r'"[^"\n]*"|“[^”\n]*”', ' ', t)
    return [p.strip() for p in re.findall(r"[^.!?;\n]+[.!?;\n]*", t) if p.strip()]


def _reset_clause(t):
    return (bool(re.search(r"\bcodex\b", t) and re.search(r"\bresets?(?:ting)?\b", t))
            and not re.search(NON_QUOTA, t))


def _broad_scope(t):
    if re.search(r"\b(?:except|excluding|selected|eligible|beta|only|pro|plus|enterprise|business|edu|team)\b", t):
        return False
    audience = re.search(
        r"\bfor\s+(?:everyone|everybody|all(?:\s+our)?(?:\s+paid)?(?:\s+codex)?(?:\s+(?:users|accounts|subscribers))?)\b"
        r"|\b(?:everyone's|everybody's)\s+(?:codex\s+)?(?:usage\s+)?(?:limits|quotas|credits)\b"
        r"|\ball\s+(?:codex\s+)(?:users|accounts|limits|quotas)\b", t)
    if not audience:
        return False
    rest = t[audience.end():].strip().lstrip(', :–-')
    # Never treat 'all Pro users', 'everyone who...', or a region as global.
    if re.search(r"\bin\s+(?!24\s+hours|48\s+hours)", rest):
        return False
    if re.match(r"(?:who\b|that\b|with\b|whose\b|affected\b|impacted\b|pro\b|plus\b|team\b|enterprise\b|business\b|edu\b|in\s+(?!24\s+hours|48\s+hours)|from\b|on\b|using\b)", rest):
        return False
    # Any unexplained audience suffix stays below the global threshold.
    return not rest or bool(re.match(r"[.!?]|(?:today|tomorrow|tonight|within|next|in|as|again|worldwide|globally|now|will|have|has|are)\b", rest))


def _strong(t):
    return _reset_clause(t) and not t.rstrip().endswith('?') and not re.search(HEDGED, t)


def _direct_announcement(text):
    """An explicit product announcement is independent of the global score.

    A greeting may identify the product while the reset appears later in the
    same post. It does not establish paid-plan eligibility or the author's
    timezone. Unrelated quality/incident paragraphs are not reset commitments.
    """
    clauses = _clauses(text)
    if any(_strong(c) and _broad_scope(c) and re.search(ACTION + '|' + COMPLETED, c)
           for c in clauses):
        return None
    addressed = set()
    for clause in clauses[:2]:
        match = re.match(r"^(?:(?:hi|hey|hello)\s+)?(astra|codex)\s+users\b", clause)
        if match:
            addressed.add(match[1])
    future = (r"\bresets?\s+(?:is|are)\s+(?:also\s+)?(?:landing|coming|rolling out)\b"
              r"|\bresets?\s+will\s+(?:also\s+)?be\s+(?:done|completed|applied|rolled out)\b")
    completed = r"\bresets?\s+(?:is|are)\s+(?:now\s+)?(?:complete|completed|done)\b"
    window = WINDOW + r"|\bby\s+(?:midnight|noon)\b"
    matches = []
    for clause in clauses:
        if (clause.rstrip().endswith('?') or re.search(HEDGED + '|' + NEGATED + '|' + NON_QUOTA, clause)
                or re.search(r"\b(?:chatgpt|sora|git)\b", clause)):
            continue
        if not re.search(r"\bresets?\b", clause):
            continue
        product = set(re.findall(r"\b(astra|codex)\s+(?:users|resets?|(?:usage\s+)?(?:limits|quotas|credits))\b", clause))
        product = product or addressed
        if len(product) != 1:
            continue
        if re.search(completed + '|' + COMPLETED, clause):
            status = "confirmed"
        elif re.search(future + '|' + ACTION, clause) and re.search(window, clause):
            status = "promised"
        else:
            continue
        matches.append((next(iter(product)), status))
    # A later cancellation can retract an announcement even without repeating
    # the product greeting. Never select one of contradictory statements.
    if any((re.search(r"\bresets?\b", c) and re.search(NEGATED, c))
           or re.search(r"\b(?:update|correction|actually)\b.*\b(?:cancelled|canceled|not happening)\b", c)
           for c in clauses):
        return None
    if not matches or len({product for product, _ in matches}) != 1:
        return None
    product, status = matches[-1]
    label = "Astra" if product == "astra" else "Codex"
    return {"status": status, "title": label + (" 重置完成公告" if status == "confirmed" else " 重置公告"),
            "scope": label + " 用户；具体套餐和适用资格未说明，请以原帖为准。"}


def _limited_scope(text):
    return ("在受影响时段使用过 banked reset 的用户；涉及 ChatGPT Work 和 Codex。"
            if re.search(r"affected time window", text, re.I) and re.search(r"banked resets", text, re.I) else
            "仅原帖所述的受影响用户；具体资格请核对原文。" if _limited_compensation(text.lower()) else
            "仅原帖限定的用户或地区；具体资格请核对原文。")


def _limited_action(text):
    t = text.lower().replace("’", "'")
    # A separate explicit broad action takes precedence over incident context.
    if any(_strong(c) and _broad_scope(c) and re.search(ACTION + '|' + COMPLETED, c)
           for c in _clauses(t)):
        return False
    if _direct_announcement(text):
        return True
    if _limited_compensation(t):
        return True
    return any(_reset_clause(c) and not _broad_scope(c)
               and re.search(r"\b(?:pro|plus|team|enterprise|business|edu|affected|impacted)\b|\b(?:everyone|users)\s+(?:who|in|with)\b", c)
               and re.search(ACTION + '|' + COMPLETED + '|' + NEGATED, c) for c in _clauses(t))


def classify(text):
    clauses = _clauses(text)
    if _direct_announcement(text):
        return ("context", 0, 0)
    relevant = [c for c in clauses if _reset_clause(c)]
    if not relevant:
        if any(re.match(r"^(?:update|correction|actually)\b", c) and re.search(r"\breset\b", c)
               and re.search(NEGATED, c) and not re.search(r"\b(?:chatgpt|sora|git|password)\b", c) for c in clauses):
            return ("correction", 0, 0)
        return None
    if any(re.search(NEGATED, c) for c in relevant):
        return ("correction", 0, 0)
    # Explicit retraction in an adjacent update invalidates an earlier promise.
    if any(re.search(r"\b(?:update|correction|actually)\b.*\b(?:cancelled|canceled|not happening)\b", c) for c in clauses):
        return ("correction", 0, 0)
    strong = [c for c in relevant if _strong(c)]
    if _limited_action(text):
        return ("context", 0, 0) if strong and not any(re.search(HEDGED, c) for c in relevant) else ("hint", 30, 45)
    for c in reversed(strong):
        if _broad_scope(c) and re.search(ACTION, c) and re.search(WINDOW, c):
            window = re.search(WINDOW, c)[0]
            return ("promise", 65 if "48" in window or "tomorrow" in window else 80, 85)
        if _broad_scope(c) and re.search(COMPLETED, c):
            return ("confirmation", 0, 0)
    if not strong:
        return ("hint", 30, 45)
    return ("hint", 35, 55) if not any(_broad_scope(c) for c in strong) else ("hint", 40, 60)


def promise_deadline(text, posted):
    windows = [re.search(WINDOW, c)[0] for c in _clauses(text)
               if _strong(c) and _broad_scope(c) and re.search(ACTION, c) and re.search(WINDOW, c)]
    # Local 'today/tonight' has no known author timezone: 24h is a bounded
    # observation cap, not a claimed calendar deadline. Tomorrow is capped at 48h.
    hours = min((48 if '48' in w or 'tomorrow' in w else 24 for w in windows), default=0)
    return posted + timedelta(hours=hours)


def update(store, posts, now):
    # Seen IDs, score, snapshot and first-run suppression must commit together.
    with store.transaction():
        return _update(store, posts, now)


def _update(store, posts, now):
    current = store.get("snapshot", {
        "version": 1, "mode": "live", "checkedAt": stamp(now), "forecast": None,
        "events": [], "evidence": [], "history": [],
    })
    bootstrap = not store.get("initialized", False)
    posts = sorted({p["id"]: p for p in posts if date(p["postedAt"]) <= now}.values(), key=lambda p: (date(p["postedAt"]), int(p["id"])))
    represented = {e["id"]: e for e in current["evidence"]}
    for post in posts:
        old = represented.get("post-" + post["id"])
        if old and (old["text"], old["postedAt"]) != (post["text"], post["postedAt"]):
            # Same-ID edits can change scope or retract a promise. Preserve the
            # new source but suspend its event until a human resolves linkage.
            old.update(text=post["text"], postedAt=post["postedAt"], kind="hint",
                       summary="原帖内容发生变化；暂停该事件自动提醒，需人工核对。")
            for event in current["events"]:
                if event["id"] == old["eventId"]:
                    event["reviewRequired"] = True
                    event["reviewReason"] = "source-edited"
    recovering = (store.get("classificationRecoveryVersion") != RULES_VERSION
                  or store.get("announcementRecoveryVersion") != ANNOUNCEMENT_RULES_VERSION)
    # Recover recognized posts omitted by an earlier classifier without
    # resetting seen IDs or either notification ledger. Replays are idempotent.
    new = [p for p in posts if store.unseen(p["id"]) or (
        recovering and "post-" + p["id"] not in represented and classify(p["text"]) is not None)]
    for post in new:
        result = classify(post["text"])
        if not result:
            continue
        kind, p24, p48 = result
        posted = date(post["postedAt"])
        limited = _limited_action(post["text"])
        if limited:
            # Limited cohorts must never merge into or retract a global event.
            # Keep the source wording: an announced replacement is not proof
            # that every eligible account has already received it.
            direct = _direct_announcement(post["text"])
            if direct:
                title, status = direct["title"], direct["status"]
            elif kind == "correction":
                title, status = "小范围补偿撤回线索", "retracted"
            elif kind == "context":
                completed = any(re.search(COMPLETED, c) for c in _clauses(post["text"]))
                title, status = ("小范围重置完成公告", "confirmed") if completed else ("小范围重置公告", "promised")
            else:
                title, status = "小范围重置线索", "watching"
            eid = "post-" + post["id"]
            event_id = "limited-" + post["id"]
            scope = direct["scope"] if direct else _limited_scope(post["text"])
            current["events"].append({
                "id": event_id, "title": title, "type": "limited-reset", "status": status,
                "scope": scope, "announcedAt": post["postedAt"], "evidenceIds": [eid],
                **({"announcement": True} if direct else {}),
                **({"notificationSuppressed": True} if bootstrap else {}),
            })
            current["evidence"].append({
                "id": eid, "eventId": event_id, "author": "@thsottiaux", "postedAt": post["postedAt"],
                "text": post["text"], "url": post["url"], "kind": kind,
                "summary": ("原帖明确宣布重置；具体套餐和适用资格未说明，时间以原帖为准。" if direct else
                            "原帖宣布限定范围的额度重置；不代表全体用户适用，完成情况请核对原文。"
                            if kind == "context" else "限定人群的相关表述，请核对原文和最新进展。"),
            })
            continue
        candidates = [e for e in current["events"]
                      if e["type"] == "global-reset" and not e.get("reviewRequired") and e["status"] in ("watching", "promised")
                      and timedelta(0) <= posted - date(e["announcedAt"]) < timedelta(hours=48)
                      and (not e.get("expectedAt") or posted < date(e["expectedAt"]))]
        explicit_new = bool(re.search(r"\b(?:another (?:codex )?reset|new reset)\b", post["text"], re.I))
        if len(candidates) > 1 and not explicit_new:
            # Preserve ambiguous evidence in the archive, but don't guess
            # which event to retract, confirm or notify.
            eid, event_id = "post-" + post["id"], "unlinked-" + post["id"]
            current["events"].append({"id": event_id, "title": "归属待核对的重置线索",
                "type": "global-reset", "status": "watching", "scope": "存在多项事件，无法可靠关联",
                "announcedAt": post["postedAt"], "evidenceIds": [eid], "reviewRequired": True})
            current["evidence"].append({"id": eid, "eventId": event_id, "author": "@thsottiaux",
                "postedAt": post["postedAt"], "text": post["text"], "url": post["url"], "kind": "hint",
                "summary": "已保存原帖；事件归属不明确，不据此自动提醒或改写其他事件。"})
            continue
        event = candidates[0] if candidates and not explicit_new else None
        if event is None:
            if kind == "correction":
                continue
            # A completion follow-up is not a second completed event.
            prior = [e for e in current["events"] if e["type"] == "global-reset" and e["status"] == "confirmed"
                     and timedelta(0) <= posted - date(e["confirmedAt"]) < timedelta(hours=4)]
            if kind == "confirmation" and prior and not explicit_new:
                event = prior[-1]
            else:
                event = {
                    "id": "reset-" + post["id"], "title": "Codex 额度重置线索",
                    "type": "global-reset", "status": "watching",
                    "scope": "以原帖说明为准；规则不推断具体套餐资格",
                    "announcedAt": post["postedAt"], "evidenceIds": [],
                }
                current["events"].append(event)
        eid = "post-" + post["id"]
        event["evidenceIds"].append(eid)
        current["evidence"].append({
            "id": eid, "eventId": event["id"], "author": "@thsottiaux",
            "postedAt": post["postedAt"], "text": post["text"], "url": post["url"],
            "kind": kind, "summary": {
                "promise": "检测到明确重置承诺和时间窗口，仍需等待完成公告。",
                "hint": "相关公开线索，语义或适用范围还不充分。",
                "confirmation": "检测到完成表述，请结合原文核对适用范围。",
                "correction": "检测到否定或取消表述，停止这次提醒。",
            }[kind],
        })
        if kind == "promise":
            event["status"] = "promised"
            # Never extend indefinitely on each poll or repeated promise.
            deadline = promise_deadline(post["text"], posted)
            event["expectedAt"] = stamp(min(date(event["expectedAt"]), deadline) if event.get("expectedAt") else deadline)
        elif kind == "confirmation":
            event["status"] = "confirmed"
            event.setdefault("confirmedAt", post["postedAt"])
            event["title"] = "Codex 额度重置完成线索"
        elif kind == "correction":
            event["status"] = "retracted"
        if kind in ("promise", "hint"):
            # Unknown follow-ups do not erase an earlier explicit promise.
            if event["status"] != "promised" or kind == "promise":
                store.put("score:" + event["id"], [p24, p48])
        if bootstrap or not store.unseen(post["id"]):
            store.claim(event["id"], "bootstrap-suppressed", stamp(now))

    # Keep a complete bounded public archive (whole events + their evidence).
    current["events"] = sorted(current["events"], key=lambda e: e["announcedAt"], reverse=True)[:100]
    for e in current["events"]:
        if len(e["evidenceIds"]) > 5:
            promises = [x["id"] for x in current["evidence"] if x["id"] in e["evidenceIds"]
                        and classify(x["text"]) and classify(x["text"])[0] == "promise"]
            keep = e["evidenceIds"][:1] + promises[-1:] + e["evidenceIds"][-4:]
            unique = list(dict.fromkeys(keep))
            e["evidenceIds"] = unique[:2] + unique[2:][-3:]
    included = {eid for e in current["events"] for eid in e["evidenceIds"]}
    current["evidence"] = [e for e in current["evidence"] if e["id"] in included]
    current["events"].sort(key=lambda e: e["announcedAt"], reverse=True)
    for e in current["events"]:
        if e["type"] != "global-reset" or e["status"] not in ("watching", "promised"):
            continue
        sources = [x for x in current["evidence"] if x["id"] in e["evidenceIds"]]
        promises = [x for x in sources if classify(x["text"]) and classify(x["text"])[0] == "promise"]
        if promises:
            deadline = min(promise_deadline(x["text"], date(x["postedAt"])) for x in promises)
            e["expectedAt"] = stamp(min(deadline, date(e["expectedAt"]))) if e.get("expectedAt") else stamp(deadline)
            store.put("score:" + e["id"], list(classify(promises[-1]["text"])[1:]))
            e["scope"] = ("全体付费用户；不代表免费用户也适用" if any(re.search(r"all (?:our )?paid", x["text"], re.I) for x in promises)
                          else "原帖面向全体用户；具体套餐与到账状态仍以官方说明为准")
        else:
            e["status"] = "watching"
            hints = [classify(x["text"]) for x in sources if classify(x["text"]) and classify(x["text"])[0] == "hint"]
            store.put("score:" + e["id"], list(hints[-1][1:]) if hints else [30, 45])
            if sources and all(not classify(x["text"]) or classify(x["text"])[0] == "context" for x in sources):
                e["reviewRequired"] = True
    active = [e for e in current["events"] if e["type"] == "global-reset" and not e.get("reviewRequired") and e["status"] in ("watching", "promised")
              and date(e.get("expectedAt", e["announcedAt"])) +
              (timedelta(0) if e.get("expectedAt") else timedelta(hours=48)) > now]
    current["forecast"] = None
    if len(active) == 1:
        event = active[0]
        p24, p48 = store.get("score:" + event["id"], [0, 0])
        end = date(event["expectedAt"]) if event.get("expectedAt") else date(event["announcedAt"]) + timedelta(hours=48)
        current["forecast"] = {
            "id": "forecast-" + event["id"], "eventId": event["id"],
            "probability24h": p24, "probability48h": p48,
            "generatedAt": stamp(now), "validUntil": stamp(min(now + timedelta(hours=1), end)),
            "windowEndsAt": stamp(end), "status": event["status"],
            "summary": "信号评分，不是发生概率。85 分表示原帖同时满足明确承诺、Codex 重置、广泛适用范围和有界时间；尚无历史命中率。",
            "method": RULES_VERSION, "scoreType": "ordinal-rule-score", "calibrated": False, "evidenceIds": event["evidenceIds"],
        }
        point = {"at": stamp(now), "probability24h": p24, "probability48h": p48, "eventId": event["id"]}
        previous = current["history"][-1] if current["history"] else {}
        if any(previous.get(k) != point[k] for k in ("eventId", "probability24h", "probability48h")):
            current["history"].append(point)
    current["history"] = current["history"][-500:]
    current["checkedAt"] = stamp(now)
    store.put("snapshot", current)
    store.mark_seen(p["id"] for p in new)
    store.put("initialized", True)
    store.put("classificationRecoveryVersion", RULES_VERSION)
    store.put("announcementRecoveryVersion", ANNOUNCEMENT_RULES_VERSION)
    return current


def announcement_candidates(snapshot, now, *, allow_bootstrap=False):
    """Recent explicit announcements, revalidated independently of forecasting.

    Returns event/source pairs. Delivery channels keep their own durable ledger;
    first-run archives, stale snapshots and old announcements are never sent.
    """
    candidates = []
    try:
        if (snapshot.get("mode") != "live"
                or not timedelta(0) <= now - date(snapshot["checkedAt"]) <= timedelta(hours=1)):
            return []
        for event in snapshot["events"]:
            if (event.get("reviewRequired") or (event.get("notificationSuppressed") and not allow_bootstrap)
                    or event["type"] != "limited-reset" or event["status"] not in ("promised", "confirmed")
                    or not timedelta(0) <= now - date(event["announcedAt"]) < timedelta(hours=24)
                    or len(event["evidenceIds"]) != 1):
                continue
            sources = [source for source in snapshot["evidence"]
                       if source["id"] in event["evidenceIds"] and source["eventId"] == event["id"]]
            if len(sources) != 1:
                continue
            source = sources[0]
            match = re.fullmatch(r"https://x\.com/thsottiaux/status/(\d+)", source["url"])
            if (not match or source.get("author") != "@thsottiaux"
                    or source["id"] != "post-" + match[1] or event["id"] != "limited-" + match[1]
                    or date(source["postedAt"]) != date(event["announcedAt"])
                    or source["kind"] != "context" or classify(source["text"]) != ("context", 0, 0)):
                continue
            direct = _direct_announcement(source["text"])
            expected = direct or {
                "scope": _limited_scope(source["text"]),
                "status": "confirmed" if any(re.search(COMPLETED, c) for c in _clauses(source["text"])) else "promised",
            }
            if (event["scope"] != expected["scope"] or event["status"] != expected["status"]
                    or bool(event.get("announcement")) != bool(direct)):
                continue
            candidates.append({"event": event, "source": source})
        return candidates
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return []


def eligible(snapshot, now):
    try:
        f = snapshot.get("forecast")
        if snapshot.get("mode") != "live" or not f or f.get("method") != RULES_VERSION:
            return False
        if not (timedelta(0) <= now - date(snapshot["checkedAt"]) <= timedelta(hours=1)):
            return False
        if not (timedelta(0) <= now - date(f["generatedAt"]) <= timedelta(hours=1)):
            return False
        evidence = [e for e in snapshot["evidence"] if e["id"] in f["evidenceIds"] and e["eventId"] == f["eventId"]]
        evidence_ok = (len(evidence) == len(set(f["evidenceIds"])) == len(f["evidenceIds"]) > 0
                       and all(re.fullmatch(r"https://x\.com/thsottiaux/status/\d+", e["url"])
                               and date(e["postedAt"]) <= now for e in evidence))
        promises = [e for e in evidence if classify(e["text"]) and classify(e["text"])[0] == "promise"]
        score = f["probability48h"]
        return (evidence_ok and type(score) in (int, float) and 80 <= score <= 100
                and f["status"] == "promised" and bool(promises)
                and min(promise_deadline(e["text"], date(e["postedAt"])) for e in promises) > now
                and date(f["validUntil"]) > now and date(f["validUntil"]) <= date(f["generatedAt"]) + timedelta(hours=1)
                and date(f["windowEndsAt"]) > now
                and not any(classify(e["text"]) and classify(e["text"])[0] in ("correction", "confirmation") for e in evidence)
                and any(e["id"] == f["eventId"] and e["status"] == f["status"] and e["type"] == "global-reset" and not e.get("reviewRequired") for e in snapshot["events"]))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False
