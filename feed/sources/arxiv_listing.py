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
from typing import Iterable, Literal, TypeVar
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from feed.schema import Paper
from feed.errors import SourceFailure
from feed.sources.base import Source, SourceError, SourceResult, register

ListingType = Literal["new", "cross", "replacement"]
NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "cmt-paper-feed/0.1 (https://github.com/topomat12138/cmt-paper-feed)"
_PRIORITY = {"new": 0, "cross": 1, "replacement": 2}
_PAST_DAY = re.compile(r"^[A-Z][a-z]{2}, (?P<day>\d{1,2} [A-Z][a-z]{2} \d{4}) \(showing (?P<shown>\d+) of (?P<total>\d+) entries\s*\)$")
# arXiv's CDN keeps /list pages for up to a day, so a page cached just before
# an announcement is served long after it. A per-run query value is a cache miss.
CACHE_PARAM = "_"
RETRY_PAUSE = 60.0
_ID = re.compile(r"(?P<id>\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v(?P<version>[1-9]\d*))?")
_T = TypeVar("_T")


class IncompleteMetadataError(SourceError):
    """A valid Atom feed omitted requested entries and may succeed on retry."""


@dataclass(frozen=True)
class ListingRef:
    arxiv_id: str
    version: int | None
    listing_type: ListingType | None

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


def parse_pastweek(html: str) -> dict[date, tuple[ListingRef, ...]]:
    """Parse the past-week page: new and cross-listed entries per announcement day.

    The page omits replacements. Every day section and the page total must be
    complete, the same fail-closed checks as the daily page.
    """
    root = BeautifulSoup(html, "html.parser").select_one("#dlpage")
    if root is None:
        raise SourceError("arXiv past-week container missing")
    total = re.search(r"Total of\s+(\d+)\s+entries", root.get_text(" ", strip=True))
    if not total:
        raise SourceError("arXiv past-week total missing")
    days: dict[date, list[ListingRef]] = {}
    expected: dict[date, int] = {}
    current = None
    count = 0
    for node in root.find_all(["h3", "dt"]):
        if node.name == "h3":
            match = _PAST_DAY.match(" ".join(node.get_text(" ", strip=True).split()))
            if not match:
                raise SourceError("arXiv past-week day heading is invalid")
            if match["shown"] != match["total"]:
                raise SourceError("arXiv past-week day is incomplete")
            try:
                current = datetime.strptime(match["day"], "%d %b %Y").date()
            except ValueError as error:
                raise SourceError("arXiv past-week day heading is invalid") from error
            if current in days:
                raise SourceError("arXiv past-week repeats a day")
            days[current], expected[current] = [], int(match["total"])
            continue
        anchor = node.select_one('a[title="Abstract"]')
        if current is None or anchor is None or not anchor.get("href"):
            raise SourceError("arXiv past-week entry is missing its day or identifier")
        identifier, version = _identifier(anchor["href"])
        kind = "cross" if "cross-list from" in node.get_text(" ", strip=True) else "new"
        days[current].append(ListingRef(identifier, version, kind))
        count += 1
    if count != int(total[1]) or any(len(days[key]) != value for key, value in expected.items()):
        raise SourceError("arXiv past-week counts do not match its entries")
    return {key: tuple(value) for key, value in days.items()}


def failure_reason(error: BaseException) -> str:
    """A short public reason: fixed source messages or HTTP status, never response text."""
    cause = error.__cause__ if isinstance(error, SourceError) and error.__cause__ is not None else error
    if isinstance(cause, httpx.HTTPStatusError):
        return f"HTTP {cause.response.status_code}"
    if isinstance(cause, httpx.TimeoutException):
        return "timeout"
    if isinstance(cause, httpx.HTTPError):
        return "network error"
    if isinstance(error, SourceError):
        return str(error)
    return type(error).__name__


def _text(parent: ET.Element, path: str, *, required: bool = False) -> str | None:
    node = parent.find(path, NS)
    value = " ".join("".join(node.itertext()).split()) if node is not None else ""
    if required and not value:
        raise SourceError("required arXiv API metadata missing")
    return value or None


def parse_metadata(xml: bytes | str, refs: dict[str, ListingRef], *, announced: date | None, seen_at: datetime) -> list[Paper]:
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
        raise IncompleteMetadataError("arXiv API omitted requested entries")
    return [papers[key] for key in sorted(papers)]


@register("arxiv_listing")
class ArxivListing(Source):
    def __init__(self, *, categories: list[str], client: httpx.Client,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 catch_up: bool = False, known_ids: Iterable[str] = (),
                 cache_token: Callable[[], str] | None = None):
        """catch_up also reads each category's past-week page and returns the
        new/cross entries of earlier announcements that are not in known_ids
        (unversioned arXiv IDs already stored), so a day missed by a stale or
        failed daily page is recovered on a later run. Replacements are not on
        that page and are not recovered."""
        self.categories = sorted(set(categories))
        self.client = client
        self.sleep, self.clock = sleep, clock
        self.catch_up = catch_up
        self.known_ids = frozenset(known_ids)
        self.cache_token = cache_token or (lambda: str(int(time.time())))
        self._last_request: float | None = None

    def _get(self, url: str, params: dict, parse: Callable[[httpx.Response], _T]) -> _T:
        for attempt in range(3):
            if self._last_request is not None:
                delay = 3.0 - (self.clock() - self._last_request)
                if delay > 0:
                    self.sleep(delay)
            self._last_request = self.clock()
            try:
                response = self.client.get(url, params=params, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
                return parse(response)
            except SourceError:
                # Includes incomplete Atom feeds and listing pages that fail
                # validation, which a CDN or upstream hiccup can produce.
                if attempt == 2:
                    raise
            except httpx.HTTPError as error:
                if attempt == 2:
                    raise SourceError("arXiv request failed after three attempts") from error
        raise AssertionError("unreachable")

    def _page(self, category: str, kind: str, parse: Callable[[str], _T]) -> _T:
        return self._get(f"https://arxiv.org/list/{category}/{kind}",
                         {"show": 2000, CACHE_PARAM: self.cache_token()}, lambda response: parse(response.text))

    def _pages(self, kind: str, parse: Callable[[str], _T]) -> tuple[dict[str, _T], dict[str, BaseException]]:
        """Fetch every category, then retry the failed ones once after a pause."""
        pages, failed = {}, {}
        for category in self.categories:
            try:
                pages[category] = self._page(category, kind, parse)
            except Exception as error:
                failed[category] = error
        if failed:
            self.sleep(RETRY_PAUSE)
            for category in sorted(failed):
                try:
                    pages[category] = self._page(category, kind, parse)
                    del failed[category]
                except Exception as error:
                    failed[category] = error
        return pages, failed

    def fetch(self, *, seen_at: datetime, day: date | None = None) -> SourceResult:
        refs: dict[str, ListingRef] = {}
        dates_by_id: dict[str, date] = {}
        announcement_dates = set()
        errors = []

        def add(ref: ListingRef, announced: date) -> None:
            old = refs.get(ref.arxiv_id)
            if old and announced < dates_by_id[ref.arxiv_id]:
                return
            if old and announced == dates_by_id[ref.arxiv_id]:
                kind = min((old.listing_type, ref.listing_type), key=_PRIORITY.__getitem__)
                version = max(old.version or 0, ref.version or 0) or None
                ref = ListingRef(ref.arxiv_id, version, kind)
            refs[ref.arxiv_id] = ref
            dates_by_id[ref.arxiv_id] = announced

        pages, failed = self._pages("new", parse_listing)
        for category in self.categories:
            page = pages.get(category)
            if page is not None and day is not None and day != page.announced:
                failed[category] = SourceError("requested date does not match the current arXiv announcement")
                page = None
            if page is None:
                errors.append(SourceFailure(
                    source="arxiv_listing", stage="listing", category=category, announced=day,
                    message=f"arXiv listing fetch or validation failed ({failure_reason(failed[category])})"))
                continue
            announcement_dates.add(page.announced)
            for ref in page.entries:
                add(ref, page.announced)
        if self.catch_up and day is None:
            # An explicit date is a manual check of one announcement; only the
            # default run recovers earlier days.
            current = set(refs)
            past, past_failed = self._pages("pastweek", parse_pastweek)
            for category in self.categories:
                if category in past_failed:
                    errors.append(SourceFailure(
                        source="arxiv_listing", stage="listing", category=category,
                        message=f"arXiv past-week listing fetch or validation failed ({failure_reason(past_failed[category])})"))
                    continue
                for announced, entries in sorted(past[category].items()):
                    missing = [ref for ref in entries if ref.arxiv_id not in self.known_ids and ref.arxiv_id not in current]
                    for ref in missing:
                        add(ref, announced)
                    if missing:
                        announcement_dates.add(announced)
        result = []
        for announced in sorted(announcement_dates):
            identifiers = sorted(key for key in refs if dates_by_id[key] == announced)
            for start in range(0, len(identifiers), 100):
                batch = {key: refs[key] for key in identifiers[start:start + 100]}
                try:
                    result.extend(self._get(
                        API_URL, {"id_list": ",".join(ref.query_id for ref in batch.values()), "max_results": len(batch)},
                        lambda response: parse_metadata(response.content, batch, announced=announced, seen_at=seen_at),
                    ))
                except Exception:
                    errors.append(SourceFailure(
                        source="arxiv_listing", stage="metadata", announced=announced,
                        paper_ids=[f"arxiv:{key}" for key in batch],
                        message="arXiv metadata batch fetch or validation failed",
                    ))
        return SourceResult(tuple(sorted(announcement_dates)), result, errors)
