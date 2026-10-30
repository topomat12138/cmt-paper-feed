"""Resolve public Scholar titles and links without fetching arbitrary URLs.

Only the arXiv API and OpenAlex API are requested. Original URLs, response
bodies, email metadata and credentials never enter records or diagnostics.
"""

from __future__ import annotations

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

import httpx

from feed.errors import ScholarIssue
from feed.schema import Paper, normalize_title
from feed.sources.arxiv_listing import (
    API_URL, NS, USER_AGENT, ArxivListing, IncompleteMetadataError,
    ListingRef, _identifier, parse_metadata,
)
from feed.sources.base import SourceError

OPENALEX_URL = "https://api.openalex.org/works"
_ARXIV_PATH = re.compile(
    r"/(?:abs|pdf)/(?P<id>\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})"
    r"(?:v(?P<version>[1-9]\d*))?(?:\.pdf)?/?", re.IGNORECASE,
)
_DOI_PATH = re.compile(r"(?:^|/)(10\.\d{4,9}/[^\s?#]+)", re.IGNORECASE)
_DOI_TAIL = re.compile(r"(?:/(?:abstract|full|pdf|epdf|fulltext)|\.pdf)/?$", re.IGNORECASE)
_OPEN_SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}totalResults"
_ROUTES = ("arxiv_url", "doi_url", "arxiv_title", "openalex_title", "title_only")


@dataclass(frozen=True)
class ResolveItem:
    title: str
    urls: tuple[str, ...] = ()


@dataclass
class ResolutionResult:
    papers: list[Paper]
    errors: list[ScholarIssue]
    counts: dict[str, int]
    failed: int = 0
    routes: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    item: ResolveItem
    index: int
    route: str
    arxiv_id: str
    version: int | None
    doi: str | None = None


def extract_arxiv(url: str | None) -> tuple[str, int | None] | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname not in {
            "arxiv.org", "www.arxiv.org", "export.arxiv.org",
        }:
            return None
        match = _ARXIV_PATH.fullmatch(unquote(parsed.path))
    except ValueError:
        return None
    if not match:
        return None
    return match["id"].lower(), int(match["version"]) if match["version"] else None


def extract_doi(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        # Encoded query/fragment delimiters are tracking too, including on
        # Nature's DOI-less /articles/<slug> paths.
        path = unquote(parsed.path).split("?", 1)[0].split("#", 1)[0]
        if parsed.hostname == "nature.com" or parsed.hostname.endswith(".nature.com"):
            slug = re.fullmatch(r"/articles/([^/]+)(?:/(?:abstract|full|pdf))?/?", path, re.IGNORECASE)
            if slug:
                doi = "10.1038/" + re.sub(r"\.pdf$", "", slug[1], flags=re.IGNORECASE).lower()
                return doi if re.fullmatch(r"10\.1038/\S+", doi) else None
        match = _DOI_PATH.search(path)
        if not match:
            return None
        doi = match[1].rstrip("/")
        while _DOI_TAIL.search(doi):
            doi = _DOI_TAIL.sub("", doi)
        return doi.lower() if re.fullmatch(r"10\.\d{4,9}/\S+", doi) else None
    except ValueError:
        return None


def _title_target(title: str) -> tuple[str, bool] | None:
    text = title.strip()
    truncated = text.endswith(("…", "..."))
    prefix = text[:-1] if text.endswith("…") else text[:-3] if truncated else text
    if truncated and len(prefix.strip()) < 60:
        return None
    normalized = normalize_title(prefix)
    return (normalized, truncated) if normalized else None


def _matches_title(candidate: str, target: tuple[str, bool]) -> bool:
    candidate = normalize_title(candidate)
    value, prefix = target
    return candidate.startswith(value) if prefix else candidate == value


def _atom_feed(xml: bytes | str) -> ET.Element:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError("invalid arXiv title response") from None
    if root.tag != f"{{{NS['atom']}}}feed":
        raise SourceError("invalid arXiv title response")
    return root


def _arxiv_title_match(xml: bytes, target: tuple[str, bool]) -> tuple[str, int | None] | None:
    root = _atom_feed(xml)
    entries = root.findall("atom:entry", NS)
    total = root.findtext(_OPEN_SEARCH)
    if total is not None and int(total) < len(entries):
        raise SourceError("invalid arXiv title result count")
    if (total is not None and int(total) > len(entries)) or (total is None and len(entries) >= 100):
        # A truncated page cannot prove a candidate unique. Fail closed.
        return None
    matches = {}
    for entry in entries:
        title = entry.findtext("atom:title", namespaces=NS)
        identifier = entry.findtext("atom:id", namespaces=NS)
        if not isinstance(title, str) or not _matches_title(title, target):
            continue
        if not isinstance(identifier, str):
            raise SourceError("arXiv title match lacks an identifier")
        key, version = _identifier(identifier)
        matches[key] = max(matches.get(key) or 0, version or 0) or None
    return next(iter(matches.items())) if len(matches) == 1 else None


def reconstruct_abstract(inverted: object) -> str | None:
    if inverted is None or inverted == {}:
        return None
    if not isinstance(inverted, dict):
        raise SourceError("invalid OpenAlex abstract")
    positions = {}
    for word, offsets in inverted.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            raise SourceError("invalid OpenAlex abstract")
        for position in offsets:
            if type(position) is not int or not 0 <= position < 100_000 or position in positions:
                raise SourceError("invalid OpenAlex abstract")
            positions[position] = word
    return " ".join(positions[position] for position in sorted(positions)) or None


def _work_doi(work: dict) -> str | None:
    doi = work.get("doi")
    if doi is None:
        return None
    if not isinstance(doi, str):
        raise SourceError("invalid OpenAlex DOI")
    canonical = extract_doi(doi if "://" in doi else "https://doi.org/" + doi)
    if canonical is None:
        raise SourceError("invalid OpenAlex DOI")
    return canonical


def _work_arxiv_ids(work: dict) -> dict[str, int | None]:
    locations = work.get("locations") or []
    if not isinstance(locations, list):
        raise SourceError("invalid OpenAlex locations")
    locations = [*locations, work.get("primary_location"), work.get("best_oa_location")]
    matches = {}
    for location in locations:
        if not isinstance(location, dict):
            continue
        for field_name in ("landing_page_url", "pdf_url"):
            match = extract_arxiv(location.get(field_name))
            if match:
                identifier, version = match
                matches[identifier] = max(matches.get(identifier) or 0, version or 0) or None
    return matches


def _work_arxiv(work: dict) -> tuple[str, int | None] | None:
    matches = _work_arxiv_ids(work)
    return next(iter(matches.items())) if len(matches) == 1 else None


def _merge_title_works(works: list[dict], *, prefix: bool) -> dict | None:
    if not works:
        return None
    if prefix and len({normalize_title(work.get("title") or work.get("display_name")) for work in works}) != 1:
        return None
    arxiv_ids, dois = {}, set()
    for work in works:
        for identifier, version in _work_arxiv_ids(work).items():
            arxiv_ids[identifier] = max(arxiv_ids.get(identifier) or 0, version or 0) or None
        if (doi := _work_doi(work)) and not doi.startswith("10.48550/arxiv."):
            dois.add(doi)
    arxiv = next(iter(arxiv_ids.items())) if len(arxiv_ids) == 1 else None
    doi = next(iter(dois)) if len(dois) == 1 else None
    if arxiv is None and doi is None:
        return None
    # Prefer the journal work's metadata; only unambiguous identities survive
    # the union. In particular, do not copy a representative's ambiguous URL.
    preferred = [work for work in works if doi and _work_doi(work) == doi]
    ordered = [*preferred, *[work for work in works if work not in preferred]]
    merged = dict(ordered[0])
    for name in ("authorships", "abstract_inverted_index", "primary_location"):
        merged[name] = next((work[name] for work in ordered if work.get(name)), None)
    merged["doi"] = "https://doi.org/" + doi if doi else None
    merged["locations"] = [{"landing_page_url": "https://arxiv.org/abs/" + arxiv[0]
                             + (f"v{arxiv[1]}" if arxiv[1] else "")}] if arxiv else []
    for name in ("primary_location", "best_oa_location"):
        location = merged.get(name)
        merged[name] = ({key: value for key, value in location.items()
                         if key not in {"landing_page_url", "pdf_url"}} if isinstance(location, dict) else None)
    return merged


def _author_surnames(authors: list[str]) -> set[str]:
    surnames = set()
    for author in authors:
        name = unicodedata.normalize("NFKC", author).strip()
        if not name:
            continue
        family = name.split(",", 1)[0] if "," in name else name.split()[-1]
        family = normalize_title(family)
        if len(family) > 1:
            surnames.add(family)
    return surnames


def link_doi_papers(papers: list[Paper], existing: Iterable[Paper]) -> tuple[list[Paper], set[int]]:
    """Scholar-only conservative bridge from a journal DOI to a stored arXiv.

    Build the title/surname index once. Strong DOI matches take precedence and
    competing incoming DOIs cannot claim the same DOI-less arXiv record.
    """
    index, known_dois = {}, set()
    for paper in existing:
        if paper.doi:
            known_dois.add(paper.doi)
        elif paper.arxiv_id:
            index.setdefault(normalize_title(paper.title), []).append((paper, _author_surnames(paper.authors)))
    proposals, target_dois = {}, {}
    for number, paper in enumerate(papers):
        if paper.arxiv_id or not paper.doi or paper.doi in known_dois:
            continue
        surnames = _author_surnames(paper.authors)
        candidates = [candidate for candidate, names in index.get(normalize_title(paper.title), [])
                      if surnames & names]
        if len(candidates) == 1:
            target = candidates[0]
            proposals[number] = target
            target_dois.setdefault(target.paper_id, set()).add(paper.doi)
    linked, result = set(), list(papers)
    for number, target in proposals.items():
        if len(target_dois[target.paper_id]) == 1:
            values = papers[number].model_dump()
            values.update(paper_id="", arxiv_id=target.arxiv_id)
            result[number] = Paper.model_validate(values)
            linked.add(number)
    return result, linked


def _openalex_paper(work: dict, *, doi: str, seen_at: datetime) -> Paper:
    title = work.get("title") or work.get("display_name")
    if not isinstance(title, str) or not title.strip():
        raise SourceError("OpenAlex work title missing")
    if (found := _work_doi(work)) is not None and found != doi:
        raise SourceError("OpenAlex returned a different DOI")
    authorships = work.get("authorships") or []
    if not isinstance(authorships, list):
        raise SourceError("invalid OpenAlex authors")
    authors = []
    for authorship in authorships:
        if not isinstance(authorship, dict):
            raise SourceError("invalid OpenAlex authors")
        author = authorship.get("author") or {}
        name = author.get("display_name") if isinstance(author, dict) else None
        if isinstance(name, str) and name.strip():
            authors.append(name)
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {} if isinstance(primary, dict) else {}
    journal = source.get("display_name") if isinstance(source, dict) else None
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    for value in (title, doi, *authors, abstract, journal):
        if isinstance(value, str):
            value.encode("utf-8")  # Reject malformed remote text before any store write.
    return Paper(title=title, doi=doi, authors=authors, abstract=abstract,
                 journal_ref=journal if isinstance(journal, str) else None,
                 links={"doi": "https://doi.org/" + doi}, sources=["scholar_alert"],
                 announced=None, listing_type=None, first_seen=seen_at, last_updated=seen_at)


class Resolver:
    def __init__(self, client: httpx.Client, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.sleep, self.clock = sleep, clock
        self.arxiv = ArxivListing(categories=[], client=client, sleep=sleep, clock=clock)
        self._openalex_last: float | None = None
        self._works: dict[str, dict | None] = {}

    def _openalex(self, url: str, params: dict | None = None) -> dict | None:
        for attempt in range(3):
            if self._openalex_last is not None:
                delay = 0.1 - (self.clock() - self._openalex_last)
                if delay > 0:
                    self.sleep(delay)
            self._openalex_last = self.clock()
            try:
                response = self.client.get(url, params=params, headers={"User-Agent": USER_AGENT})
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise SourceError("invalid OpenAlex response")
                return data
            except httpx.HTTPError:
                if attempt == 2:
                    raise SourceError("OpenAlex request failed after three attempts") from None
                self.sleep(3.0)
        raise AssertionError("unreachable")

    def _doi_work(self, doi: str) -> dict | None:
        if doi not in self._works:
            self._works[doi] = self._openalex(OPENALEX_URL + "/https://doi.org/" + quote(doi, safe="/"))
        work = self._works[doi]
        if work is not None and (found := _work_doi(work)) is not None and found != doi:
            raise SourceError("OpenAlex returned a different DOI")
        return work

    def _openalex_title(self, target: tuple[str, bool]) -> dict | None:
        data = self._openalex(OPENALEX_URL, {"filter": "title.search:" + target[0], "per_page": 50})
        if data is None:
            return None
        works = data.get("results")
        if not isinstance(works, list) or any(not isinstance(work, dict) for work in works):
            raise SourceError("invalid OpenAlex search response")
        matches = []
        for work in works[:50]:
            title = work.get("title") or work.get("display_name")
            if isinstance(title, str) and _matches_title(title, target):
                matches.append(work)
        return _merge_title_works(matches, prefix=target[1])

    def _arxiv_batch(self, refs: dict[str, ListingRef], seen_at: datetime) -> dict[str, Paper]:
        last_xml = None

        def parse(response):
            nonlocal last_xml
            last_xml = response.content
            try:
                return parse_metadata(last_xml, refs, announced=None, seen_at=seen_at)
            except SourceError as error:
                # Retry partial/malformed item metadata within the existing
                # three-attempt budget, then salvage independently below.
                raise IncompleteMetadataError("arXiv batch metadata incomplete") from error

        try:
            papers = self.arxiv._get(API_URL,
                                     {"id_list": ",".join(ref.query_id for ref in refs.values()),
                                      "max_results": len(refs)}, parse)
        except SourceError:
            papers = []
            if last_xml is not None:
                try:
                    root = _atom_feed(last_xml)
                    entries = {}
                    for entry in root.findall("atom:entry", NS):
                        try:
                            identifier, _ = _identifier(entry.findtext("atom:id", namespaces=NS) or "")
                            entries.setdefault(identifier, []).append(entry)
                        except SourceError:
                            continue
                    for identifier, candidates in entries.items():
                        if identifier not in refs or len(candidates) != 1:
                            continue
                        try:
                            one = ET.Element(f"{{{NS['atom']}}}feed")
                            one.append(candidates[0])
                            papers.extend(parse_metadata(ET.tostring(one), {identifier: refs[identifier]},
                                                         announced=None, seen_at=seen_at))
                        except (SourceError, ValueError, TypeError):
                            continue
                except SourceError:
                    pass
        return {paper.arxiv_id: paper.model_copy(update={"sources": ["scholar_alert"]}) for paper in papers}

    def resolve(self, items: list[ResolveItem], *, seen_at: datetime) -> ResolutionResult:
        papers, errors, pending = {}, [], []
        routes = {}

        def issue(index, stage, message):
            errors.append(ScholarIssue(code="resolution_failure", stage=stage, item_index=index, message=message))

        def fallback(index, item, doi=None, route="doi_url"):
            try:
                papers[index] = Paper(title=item.title, doi=doi,
                                      links={"doi": "https://doi.org/" + doi} if doi else {},
                                      sources=["scholar_alert"],
                                      first_seen=seen_at, last_updated=seen_at)
                routes[index] = route if doi else "title_only"
            except ValueError:
                errors.append(ScholarIssue(code="invalid_item", stage="payload", item_index=index,
                                           message="title cannot form a paper identity"))

        for index, item in enumerate(items):
            known_doi, known_route = None, "doi_url"
            try:
                arxiv_matches = [match for url in item.urls if (match := extract_arxiv(url))]
                if arxiv_matches:
                    identifiers = {match[0] for match in arxiv_matches}
                    if len(identifiers) != 1:
                        raise SourceError("ambiguous arXiv links")
                    identifier = next(iter(identifiers))
                    version = max(match[1] or 0 for match in arxiv_matches) or None
                    pending.append(_Pending(item, index, "arxiv_url", identifier, version))
                    continue
                dois = {doi for url in item.urls if (doi := extract_doi(url))}
                if len(dois) > 1:
                    raise SourceError("ambiguous DOI links")
                if dois:
                    doi = known_doi = next(iter(dois))
                    work = self._doi_work(doi)
                    if work is None:
                        raise SourceError("OpenAlex DOI metadata unavailable")
                    arxiv_match = _work_arxiv(work)
                    if arxiv_match:
                        pending.append(_Pending(item, index, "doi_url", *arxiv_match, doi))
                    else:
                        papers[index] = _openalex_paper(work, doi=doi, seen_at=seen_at)
                        routes[index] = "doi_url"
                    continue

                target = _title_target(item.title)
                if target is None:
                    fallback(index, item)
                    continue
                try:
                    arxiv_match = self.arxiv._get(API_URL,
                                                  {"search_query": f'ti:"{target[0]}"', "max_results": 100},
                                                  lambda response: _arxiv_title_match(response.content, target))
                except (SourceError, ValueError):
                    arxiv_match = None
                    issue(index, "resolve", "arXiv title lookup failed; continuing with remaining resolution steps")
                if arxiv_match:
                    pending.append(_Pending(item, index, "arxiv_title", *arxiv_match))
                    continue
                work = self._openalex_title(target)
                if work is not None:
                    arxiv_match = _work_arxiv(work)
                    doi = known_doi = _work_doi(work)
                    known_route = "openalex_title"
                    if arxiv_match:
                        pending.append(_Pending(item, index, "openalex_title", *arxiv_match, doi))
                        continue
                    if doi:
                        papers[index] = _openalex_paper(work, doi=doi, seen_at=seen_at)
                        routes[index] = "openalex_title"
                        continue
                fallback(index, item)
            except Exception:
                issue(index, "resolve", "paper metadata resolution failed; retained the known DOI" if known_doi
                      else "paper resolution failed; retained the public title only")
                fallback(index, item, known_doi, known_route)

        by_id = {}
        for request in pending:
            previous = by_id.get(request.arxiv_id)
            version = max(request.version or 0, previous.version if previous and previous.version else 0) or None
            by_id[request.arxiv_id] = ListingRef(request.arxiv_id, version, None)
        resolved = {}
        identifiers = sorted(by_id)
        for start in range(0, len(identifiers), 100):
            refs = {key: by_id[key] for key in identifiers[start:start + 100]}
            try:
                resolved.update(self._arxiv_batch(refs, seen_at))
            except Exception:
                # An invalid response can affect this request only; subsequent
                # batches and already resolved DOI records still continue.
                pass
        for request in pending:
            paper = resolved.get(request.arxiv_id)
            if paper is None:
                issue(request.index, "metadata", "arXiv metadata unavailable; retained the known DOI" if request.doi
                      else "arXiv metadata unavailable; retained the public title only")
                fallback(request.index, request.item, request.doi, request.route)
                continue
            if request.doi and not paper.doi:
                paper = paper.model_copy(update={"doi": request.doi})
            papers[request.index] = paper
            routes[request.index] = request.route
        counts = Counter(routes.values())
        failures = {error.item_index for error in errors if error.code == "resolution_failure"}
        return ResolutionResult([papers[index] for index in sorted(papers)], errors,
                                {route: counts[route] for route in _ROUTES},
                                failed=sum(routes[index] == "title_only" and index in failures for index in papers),
                                routes=[routes[index] for index in sorted(papers)])
