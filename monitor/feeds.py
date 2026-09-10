import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

UTC = timezone.utc
LIMIT = 1_500_000


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def date(value):
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        d = parsedate_to_datetime(value)
    if d.tzinfo is None:
        raise ValueError("Date needs timezone")
    return d.astimezone(UTC)


class PostText(HTMLParser):
    """Quoted posts are not the target author's own promise."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stack = [], []

    @property
    def quote(self):
        return any(blocked for _, blocked in self.stack)

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "") or ""
        blocked = tag in ("blockquote", "script", "style") or bool(
            set(classes.lower().split()) & {"quote", "quote-text", "quoted-tweet", "quote-tweet", "quoted-post"})
        if tag not in ("br", "img", "hr", "input", "meta", "link", "source", "wbr"):
            self.stack.append((tag, blocked))
        if tag in ("p", "br", "div") and not self.quote:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in ("p", "div") and not self.quote:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.quote:
            self.parts.append(data)


def parse_feed(body, now, mirror_host=""):
    if len(body) > LIMIT or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("Unsafe or oversized XML")
    root = ET.fromstring(body)
    local = lambda tag: tag.rsplit("}", 1)[-1]
    if local(root.tag) not in ("rss", "feed"):
        raise ValueError("Expected RSS/Atom, not an HTML challenge")
    posts = {}
    for item in root.iter():
        if local(item.tag) not in ("item", "entry"):
            continue
        fields = {local(x.tag): x.text or "" for x in item}
        for x in item:
            if local(x.tag) in ("content", "description", "summary") and len(x):
                fields[local(x.tag)] = re.sub(r"<(/?)(?:ns\d+|html|xhtml):", r"<\1",
                    (x.text or "") + "".join(ET.tostring(c, encoding="unicode") for c in x))
        links = [x.attrib.get("href") or x.text or "" for x in item
                 if local(x.tag) == "link" and x.attrib.get("rel", "alternate") == "alternate"]
        link = links[0] if links else fields.get("guid", "")
        u = urlsplit(link)
        match = re.fullmatch(r"/thsottiaux/status/(\d+)(?:/)?", u.path, re.I)
        if u.scheme != "https" or u.username or u.password or not match:
            continue
        if u.hostname not in {"x.com", "twitter.com", "fxtwitter.com", "fixupx.com", mirror_host}:
            continue
        title = fields.get("title", "")
        if re.match(r"^(RT |RT by |Retweeted)", title, re.I):
            continue
        creator = fields.get("creator", "")
        if "@" in creator and not re.search(r"@thsottiaux\b", creator, re.I):
            continue
        try:
            posted = date(fields.get("pubDate") or fields.get("published") or fields.get("updated", ""))
        except (ValueError, TypeError, OverflowError):
            continue
        if posted > now:
            continue
        parser = PostText()
        parser.feed(fields.get("description") or fields.get("content") or fields.get("summary") or title)
        text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:12000]
        if text:
            pid = match[1]
            posts[pid] = {"id": pid, "text": text, "postedAt": stamp(posted),
                          "url": f"https://x.com/thsottiaux/status/{pid}"}
    return sorted(posts.values(), key=lambda p: (p["postedAt"], int(p["id"])))


def fetch_feeds(urls, now, minimum_posted_at=None):
    """Bounded failover; never bypass authentication, CAPTCHA, or access controls."""
    failures, successes = [], []
    for url in urls[:3]:
        try:
            u = urlsplit(url)
            if u.scheme != "https" or not u.hostname or u.username or u.password:
                raise ValueError("Only public HTTPS feed URLs")
            request = urllib.request.Request(url, headers={
                "User-Agent": "TiboResetObservatory/0.1 (public RSS; 15 minute polling)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(request, timeout=12) as response:
                if urlsplit(response.url).scheme != "https":
                    raise ValueError("Insecure redirect")
                age = int(response.headers.get("Age", "0"))
                response_date = response.headers.get("Date")
                if age > 3600 or (response_date and (now - date(response_date)).total_seconds() > 3600):
                    raise ValueError("Stale mirror response")
                body = response.read(LIMIT + 1)
            posts = parse_feed(body, now, u.hostname)
            # Empty feeds can be a broken/changed mirror. Do not mark them fresh.
            if not posts:
                raise ValueError("No recognizable target posts")
            if minimum_posted_at and date(posts[-1]["postedAt"]) < date(minimum_posted_at):
                raise ValueError("Timeline regressed behind the last successful poll")
            successes.append((posts, u.hostname))
        except Exception as exc:
            # No URLs with secrets, response bodies, or tokens in logs.
            failures.append({"host": urlsplit(url).hostname or "invalid", "reason": type(exc).__name__})
    if not successes:
        raise FeedUnavailable(failures)
    merged = {}
    for posts, host in successes:
        for post in posts:
            previous = merged.get(post["id"])
            if previous and (previous["text"], previous["postedAt"]) != (post["text"], post["postedAt"]):
                raise FeedUnavailable(failures + [{"host": host, "reason": "ConflictingPost"}])
            merged[post["id"]] = post
    return sorted(merged.values(), key=lambda p: (p["postedAt"], int(p["id"]))), ", ".join(h for _, h in successes), failures


class FeedUnavailable(Exception):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("All configured feeds unavailable")
