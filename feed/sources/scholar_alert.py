"""Validate public Scholar dispatch items without reading or retaining email.

Only titles and sanitized HTTP(S) paths are passed to the metadata resolver.
Envelope extras, URL query strings, fragments and invalid values never enter
source diagnostics or reports.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx

from feed.errors import ScholarIssue
from feed.schema import Paper, normalize_title
from feed.sources.base import Source, SourceResult, register

_RESOLUTION_KINDS = ("arxiv_url", "doi_url", "arxiv_title", "openalex_title", "title_only")
_GOOGLE_HOST = re.compile(r"(?:scholar\.)?google\.[a-z0-9]+(?:\.[a-z0-9-]+)*", re.IGNORECASE)


def _public_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2000 or not re.match(r"https?://", value, re.IGNORECASE):
        raise ValueError("invalid URL")
    try:
        value.encode("utf-8")
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("invalid URL")
        if any(char.isspace() for char in parsed.netloc):
            raise ValueError("invalid URL")
        # Accessing port also validates bracketed hosts and numeric port syntax.
        parsed.port
    except ValueError:
        raise ValueError("invalid URL") from None
    host = parsed.hostname.lower().removeprefix("www.")
    if _GOOGLE_HOST.fullmatch(host):
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


@register("scholar_alert")
class ScholarAlert(Source):
    def __init__(self, *, payload: object, client: httpx.Client, resolver=None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.payload = payload
        if resolver is None:
            # Keep source registration independent from the resolver's arXiv
            # imports, so either module can be imported first.
            from feed.resolve import Resolver

            resolver = Resolver(client=client, sleep=sleep, clock=clock)
        self.resolver = resolver
        self.stats = self._empty_stats()

    @staticmethod
    def _empty_stats() -> dict:
        return {"received": 0, "valid": 0, "deduplicated": 0,
                "resolution_counts": {kind: 0 for kind in _RESOLUTION_KINDS}}

    def fetch(self, *, seen_at: datetime, day: date | None = None) -> SourceResult:
        from feed.resolve import ResolveItem

        if seen_at.tzinfo is None or seen_at.utcoffset() is None:
            raise ValueError("Scholar observation time must include a timezone")
        observed = seen_at.astimezone(timezone.utc)
        dates = (observed.date(),)
        self.stats = self._empty_stats()
        payload = self.payload
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            self.stats["received"] = len(payload["items"])
        if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                or payload.get("version") != 1 or not isinstance(payload.get("items"), list)
                or len(payload["items"]) > 200):
            return SourceResult(dates, [], [ScholarIssue(
                code="invalid_item", stage="payload",
                message="payload requires version 1 and an items list of at most 200 entries")])

        errors = []
        grouped = {}
        for number, item in enumerate(payload["items"]):
            try:
                if not isinstance(item, dict):
                    raise ValueError("invalid item")
                title = item.get("title")
                if not isinstance(title, str) or not 1 <= len(title) <= 1000 or not title.strip():
                    raise ValueError("invalid title")
                title = title.strip()
                title.encode("utf-8")
                key = normalize_title(title)
                if not key:
                    raise ValueError("title has no searchable text")
                url = _public_url(item.get("url"))
            except ValueError:
                errors.append(ScholarIssue(
                    code="invalid_item", stage="payload", item_index=number,
                    message="item requires a nonblank title of at most 1000 characters and a valid HTTP(S) URL or null"))
                continue
            self.stats["valid"] += 1
            group = grouped.setdefault(key, {"titles": set(), "urls": set()})
            group["titles"].add(title)
            if url is not None:
                group["urls"].add(url)
        self.stats["deduplicated"] = self.stats["valid"] - len(grouped)
        items = [ResolveItem(title=min(group["titles"], key=lambda title: (-len(title), title.casefold(), title)),
                             urls=tuple(sorted(group["urls"])))
                 for _, group in sorted(grouped.items())]
        if not items:
            return SourceResult(dates, [], errors)
        try:
            result = self.resolver.resolve(items, seen_at=observed)
        except Exception:
            # Resolver implementations isolate ordinary remote failures. Keep a
            # defensive title-only result if an unexpected backend error escapes.
            papers = [Paper(title=item.title, sources=["scholar_alert"], first_seen=observed,
                            last_updated=observed) for item in items]
            self.stats["resolution_counts"]["title_only"] = len(papers)
            errors.append(ScholarIssue(code="resolution_failure", stage="resolve",
                                       message="metadata resolution failed; retained title-only records"))
            return SourceResult(dates, papers, errors)
        for kind in _RESOLUTION_KINDS:
            count = result.counts.get(kind, 0)
            if type(count) is int and count >= 0:
                self.stats["resolution_counts"][kind] = count
        papers = [paper.model_copy(update={"sources": ["scholar_alert"], "announced": None,
                                           "listing_type": None}) for paper in result.papers]
        return SourceResult(dates, papers, [*errors, *result.errors])
