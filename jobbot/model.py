from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(value: str) -> str:
    p = urlsplit(value)
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        raise ValueError("Expected a public HTTP(S) link without credentials")
    # Retain meaningful query parameters, including vacancy IDs. No redirect fetching.
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/",
                       urlencode(query), ""))


@dataclass(frozen=True)
class Post:
    source: str
    post_id: int
    text: str
    original_url: str
    published_at: str | None = None
    vacancy_url: str | None = None
    links: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.source or type(self.post_id) is not int or self.post_id < 1:
            raise ValueError("Invalid source or post ID")
        if not isinstance(self.text, str) or len(self.text) > 100_000:
            raise ValueError("Invalid post text")
        canonical_url(self.original_url)
        if self.vacancy_url:
            canonical_url(self.vacancy_url)
        if self.published_at:
            dt = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("Publication date requires timezone")

    @property
    def key(self):
        # vacancy_url must identify this particular vacancy, never a generic careers page.
        return canonical_url(self.vacancy_url or self.original_url)

    @property
    def fingerprint(self):
        data = asdict(self)
        if not self.links:
            data.pop('links')  # Preserve fingerprints of older snapshots.
        return sha256(json.dumps(data, ensure_ascii=False,
                                 sort_keys=True).encode()).hexdigest()
