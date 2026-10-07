"""Bounded public-page research. No credentials, forms, JS, proxies or private IPs."""
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin, parse_qsl, unquote


class ResearchUnavailable(Exception):
    pass


def public_target(url):
    p = urlsplit(url)
    if (len(url) > 2000 or p.scheme not in {'http', 'https'} or not p.hostname
            or p.username or p.password or p.port not in {None, 80, 443}):
        raise ResearchUnavailable('unsafe_url')
    host = p.hostname.encode('idna').decode('ascii')
    if host.lower() in {'localhost', 'localhost.localdomain'} or '.' not in host:
        raise ResearchUnavailable('private_host')
    action = unquote(p.path + '?' + p.query).lower()
    if re.search(r'(?:delete|logout|unsubscribe|accept.invite|reset.password|verify.email|/joinchat|/\+)', action):
        raise ResearchUnavailable('action_link')
    if any(re.search(r'token|password|secret|authorization|signature|email', k, re.I)
           for k, _ in parse_qsl(p.query)):
        raise ResearchUnavailable('private_link')
    port = p.port or (443 if p.scheme == 'https' else 80)
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ResearchUnavailable('private_address')
    return p, host, port, addresses[0]


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = []
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','noscript','svg','form','nav','footer','header'}:
            self.skip.append(tag)
        elif not self.skip and tag in {'p','div','br','li','h1','h2','h3'}:
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if self.skip and tag == self.skip[-1]: self.skip.pop()
        elif not self.skip and tag in {'p','div','li','h1','h2','h3'}: self.parts.append('\n')
    def handle_data(self, text):
        if not self.skip: self.parts.append(text)
    def text(self):
        return '\n'.join(s for line in ''.join(self.parts).splitlines()
                         if (s := re.sub(r'\s+', ' ', line).strip()))[:9000]


def fetch_page(url, timeout=8, deadline=None):
    """Pin each connection to a checked IP, including every redirect (no DNS rebinding)."""
    deadline = deadline or time.monotonic() + timeout
    for _ in range(4):
        p, host, port, address = public_target(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise ResearchUnavailable('deadline')
        conn = http.client.HTTPConnection(host, port, timeout=min(timeout, remaining))
        try:
            sock = socket.create_connection((address, port), timeout=min(timeout, remaining))
            if p.scheme == 'https':
                try: sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                except Exception:
                    sock.close()
                    raise
            conn.sock = sock
            conn.request('GET', p.path + ('?' + p.query if p.query else '') or '/',
                         headers={'User-Agent':'PersonalJobMonitor/0.2', 'Accept':'text/html,text/plain',
                                  'Accept-Encoding':'identity'})
            response = conn.getresponse()
            if response.status in {301,302,303,307,308}:
                url = urljoin(url, response.getheader('Location', ''))
                continue
            content_type = response.getheader('Content-Type', '').lower()
            if response.status != 200 or not any(t in content_type for t in ('text/html','text/plain')):
                raise ResearchUnavailable('unavailable')
            data = bytearray()
            while len(data) <= 350_000:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise ResearchUnavailable('deadline')
                # A wall deadline plus per-read timeout bounds slow responses.
                if conn.sock: conn.sock.settimeout(min(timeout, remaining))
                chunk = response.read1(min(16384, 350_001 - len(data)))
                if not chunk: break
                data.extend(chunk)
            if len(data) > 350_000: raise ResearchUnavailable('oversize')
            charset = re.search(r'charset=([\w-]+)', content_type)
            html = data.decode(charset[1] if charset else 'utf-8', errors='replace')
            parser = PageText()
            parser.feed(html)
            return {'url':url, 'text':parser.text()}
        finally:
            conn.close()
    raise ResearchUnavailable('redirect_limit')


def research(post):
    candidates = [post.vacancy_url] if post.vacancy_url else []
    candidates += list(post.links) + re.findall(r'https?://[^\s<>"\)]+', post.text)
    seen, pages = set(), []
    deadline = time.monotonic() + 24
    attempted = 0
    for url in candidates:
        if not url or url in seen: continue
        seen.add(url)
        try:
            host = (urlsplit(url).hostname or '').lower()
            if host in {'t.me','telegram.me','telegram.org','api.telegram.org'}: continue
            if time.monotonic() >= deadline or attempted >= 3: break
            attempted += 1
            page = fetch_page(url, deadline=deadline)
            if len(page['text']) >= 80: pages.append(page)
        except (OSError, ValueError, http.client.HTTPException, ResearchUnavailable, LookupError):
            continue
    return pages
