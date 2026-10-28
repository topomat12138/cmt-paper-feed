"""arXiv's three daily listing sections plus batched Atom metadata.

The listing traversal is ported from daily-arXiv-ai-enhanced's arxiv spider;
Scrapy, the replacement cutoff, and the primary-category filter are removed.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from feed.schema import Paper
from feed.sources.base import Source, SourceError, register

ListingType = Literal["new", "cross", "replacement"]
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "cmt-paper-feed/0.1 (https://github.com/topomat12138/cmt-paper-feed)"
_PRIORITY = {"new": 0, "cross": 1, "replacement": 2}
_ID = re.compile(r"(?P<id>\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v(?P<version>[1-9]\d*))?")


@dataclass(frozen=True)
class ListingRef:
    arxiv_id: str
    version: int | None
    listing_type: ListingType

    @property
    def query_id(self) -> str:
        return self.arxiv_id + (f"v{self.version}" if self.version else "")


@dataclass(frozen=True)
class ListingPage:
    announced: date
    entries: tuple[ListingRef, ...]
    counts: dict[str, int]


def _identifier(value: str) -> tuple[str, int | None]:
    raw = urlsplit(value).path.removeprefix("/abs/") if "://" in value or value.startswith("/abs/") else value
    match = _ID.fullmatch(raw)
    if not match:
        raise SourceError("invalid arXiv identifier in source response")
    return match["id"].lower(), int(match["version"]) if match["version"] else None


def parse_listing(html: str) -> ListingPage:
    """Parse every section, checking counts to detect partial/error pages."""
    root = BeautifulSoup(html, "html.parser").select_one("#dlpage")
    if root is None:
        raise SourceError("arXiv listing container missing")
    heading = next((h.get_text(" ", strip=True) for h in root.find_all("h3")
                    if "listings for" in h.get_text()), "")
    try:
        announced = datetime.strptime(heading.split("listings for ", 1)[1], "%A, %d %B %Y").date()
    except (ValueError, IndexError) as error:
        raise SourceError("arXiv listing announcement date missing or invalid") from error
    total = re.search(r"Total of\s+(\d+)\s+entries", root.get_text(" ", strip=True))
    if not total:
        raise SourceError("arXiv listing total missing")
    entries = []
    expected = {}
    kind = None
    for node in root.find_all(["h3", "dt"]):
        if node.name == "h3":
            text = node.get_text(" ", strip=True)
            for prefix, candidate in (("New submissions", "new"), ("Cross submissions", "cross"), ("Replacement submissions", "replacement")):
                if text.startswith(prefix):
                    kind = candidate
                    count = re.search(r"showing\s+(\d+)\s+of\s+(\d+)\s+entries", text)
                    if not count or count[1] != count[2]:
                        raise SourceError("arXiv listing section is incomplete")
                    expected[kind] = int(count[2])
                    break
            continue
        anchor = node.select_one('a[title="Abstract"]')
        if kind is None or anchor is None or not anchor.get("href"):
            raise SourceError("arXiv listing entry is missing its section or identifier")
        identifier, version = _identifier(anchor["href"])
        entries.append(ListingRef(identifier, version, kind))
    counts = Counter(entry.listing_type for entry in entries)
    if len(entries) != int(total[1]) or any(counts[key] != count for key, count in expected.items()):
        raise SourceError("arXiv listing counts do not match its entries")
    return ListingPage(announced, tuple(entries), dict(counts))


def _text(parent: ET.Element, path: str, *, required: bool = False) -> str | None:
    node = parent.find(path, NS)
    value = " ".join("".join(node.itertext()).split()) if node is not None else ""
    if required and not value:
        raise SourceError("required arXiv API metadata missing")
    return value or None


def parse_metadata(xml: bytes | str, refs: dict[str, ListingRef], *, announced: date, seen_at: datetime) -> list[Paper]:
    """Parse Atom entries; require exactly the requested identifiers."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise SourceError("invalid arXiv API XML") from error
    if root.tag != f"{{{NS['atom']}}}feed":
        raise SourceError("arXiv API did not return an Atom feed")
    papers = {}
    for entry in root.findall("atom:entry", NS):
        identifier, version = _identifier(_text(entry, "atom:id", required=True))
        if version is None:
            raise SourceError("arXiv API entry version missing")
        if identifier not in refs or identifier in papers:
            raise SourceError("arXiv API returned unexpected or duplicate entries")
        ref = refs[identifier]
        if ref.version and version != ref.version:
            raise SourceError("arXiv API returned a different requested version")
        primary = entry.find("arxiv:primary_category", NS)
        categories = [node.get("term") for node in entry.findall("atom:category", NS)]
        if not categories or not all(categories):
            raise SourceError("arXiv API entry categories missing")
        links = {"abs": f"https://arxiv.org/abs/{identifier}"}
        if any(link.get("title") == "pdf" or link.get("type") == "application/pdf" for link in entry.findall("atom:link", NS)):
            links["pdf"] = f"https://arxiv.org/pdf/{identifier}"
        try:
            papers[identifier] = Paper(
                arxiv_id=identifier, arxiv_version=version,
                doi=_text(entry, "arxiv:doi"), journal_ref=_text(entry, "arxiv:journal_ref"),
                title=_text(entry, "atom:title", required=True),
                authors=[_text(author, "atom:name", required=True) for author in entry.findall("atom:author", NS)],
                abstract=_text(entry, "atom:summary", required=True),
                primary_category=primary.get("term") if primary is not None else (categories[0] if categories else None),
                categories=categories, comment=_text(entry, "arxiv:comment"),
                submitted=datetime.fromisoformat(_text(entry, "atom:published", required=True).replace("Z", "+00:00")).date(),
                announced=announced, listing_type=ref.listing_type,
                sources=["arxiv_listing"], links=links, first_seen=seen_at, last_updated=seen_at,
            )
        except (ValueError, KeyError) as error:
            raise SourceError("invalid arXiv API paper metadata") from error
    if papers.keys() != refs.keys():
        raise SourceError("arXiv API omitted requested entries")
    return [papers[key] for key in sorted(papers)]


@register("arxiv_listing")
class ArxivListing(Source):
    def __init__(self, *, categories: list[str], client: httpx.Client,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.categories = sorted(set(categories))
        self.client = client
        self.sleep, self.clock = sleep, clock
        self._last_request: float | None = None

    def _get(self, url: str, params: dict) -> httpx.Response:
        for attempt in range(3):
            if self._last_request is not None:
                delay = 3.0 - (self.clock() - self._last_request)
                if delay > 0:
                    self.sleep(delay)
            self._last_request = self.clock()
            try:
                response = self.client.get(url, params=params, headers={"User-Agent": USER_AGENT})
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    continue
                response.raise_for_status()
                return response
            except httpx.HTTPError as error:
                if attempt == 2:
                    raise SourceError("arXiv request failed after three attempts") from error
        raise AssertionError("unreachable")

    def fetch(self, *, day: date, seen_at: datetime) -> list[Paper]:
        refs: dict[str, ListingRef] = {}
        for category in self.categories:
            page = parse_listing(self._get(f"https://arxiv.org/list/{category}/new", {"show": 2000}).text)
            if day < page.announced:
                raise SourceError("requested date predates the current arXiv listing; historical replay is unavailable")
            if day > page.announced:
                continue  # Weekends/holidays still show the previous announcement.
            for ref in page.entries:
                old = refs.get(ref.arxiv_id)
                if old:
                    kind = min((old.listing_type, ref.listing_type), key=_PRIORITY.__getitem__)
                    version = max(old.version or 0, ref.version or 0) or None
                    ref = ListingRef(ref.arxiv_id, version, kind)
                refs[ref.arxiv_id] = ref
        result = []
        identifiers = sorted(refs)
        for start in range(0, len(identifiers), 100):
            batch = {key: refs[key] for key in identifiers[start:start + 100]}
            response = self._get(API_URL, {"id_list": ",".join(ref.query_id for ref in batch.values()), "max_results": len(batch)})
            result.extend(parse_metadata(response.content, batch, announced=day, seen_at=seen_at))
        return result
