"""Public Telegram preview only. No login, subscription, proxy or restriction bypass."""
from html.parser import HTMLParser
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener, Request
from .model import Post, canonical_url


class SourceError(Exception):
    def __init__(self, kind, retry_after=0):
        super().__init__(kind)
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PreviewParser(HTMLParser):
    def __init__(self, channel):
        super().__init__(convert_charrefs=True)
        self.channel = channel.lower()
        self.posts = []
        self.stack = []
        self.current = None
        self.text_depth = None
        self.closed_html = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag in {"br", "hr"}:
            if self.current and self.text_depth is not None:
                self.current["parts"].append("\n")
            return
        if tag in {"meta", "link", "img", "input", "source", "wbr", "area", "base", "embed"}:
            return
        self.stack.append(tag)
        if "tgme_widget_message" in classes and "data-post" in attrs:
            match = re.fullmatch(r"([A-Za-z0-9_]+)/([0-9]+)", attrs["data-post"])
            if not match or match[1].lower() != self.channel:
                raise SourceError("unexpected_post_identity")
            self.current = dict(depth=len(self.stack), post_id=int(match[2]),
                                parts=[], links=[], published=None)
        if self.current:
            if "tgme_widget_message_text" in classes:
                self.text_depth = len(self.stack)
            if tag == "time" and "datetime" in attrs:
                self.current["published"] = attrs["datetime"]
            if tag == "a" and self.text_depth is not None:
                href = attrs.get("href", "")
                if href.startswith(("https://", "http://")):
                    self.current["links"].append(href)

    def handle_data(self, data):
        if self.current and self.text_depth is not None:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag == "html":
            self.closed_html = True
        if not self.stack or tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.text_depth is not None and depth <= self.text_depth:
            self.text_depth = None
        if self.current and depth <= self.current["depth"]:
            p = self.current
            text = "".join(p["parts"]).strip()
            # Only a unique link with a vacancy-specific path is used for deduplication.
            # Generic company/social/contact URLs are never treated as vacancy identity.
            links = {canonical_url(u) for u in p["links"] if re.search(
                r"https?://(?:[^/]+)/(?:jobs?/[^?#]+|vacancy/\d+|[^?#]*?/job/[^?#]+)", u)}
            self.posts.append(Post(self.channel, p["post_id"], text,
                                   f"https://t.me/{self.channel}/{p['post_id']}",
                                   p["published"], next(iter(links)) if len(links) == 1 else None,
                                   tuple(dict.fromkeys(p['links']))[:12]))
            self.current = None
            self.text_depth = None
        del self.stack[depth - 1:]


def parse_preview(channel, html):
    parser = PreviewParser(channel)
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, KeyError):
        raise SourceError("invalid_page") from None
    if not parser.closed_html or not parser.posts or parser.current:
        raise SourceError("unavailable_or_changed_page")
    if len({p.post_id for p in parser.posts}) != len(parser.posts):
        raise SourceError("duplicate_post_ids")
    return parser.posts


def fetch(channel):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", channel):
        raise SourceError("invalid_channel")
    req = Request(f"https://t.me/s/{channel}", headers={
        "User-Agent": "PersonalJobMonitor/0.1", "Accept": "text/html"})
    try:
        with build_opener(NoRedirect).open(req, timeout=20) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise SourceError("oversize_page")
            html = raw.decode("utf-8")
    except HTTPError as error:
        retry = error.headers.get("Retry-After", "0")
        raise SourceError(f"http_{error.code}", int(retry) if retry.isdigit() else 0) from None
    except (URLError, TimeoutError, OSError, UnicodeError):
        raise SourceError("network_or_encoding_error") from None
    return parse_preview(channel, html)
