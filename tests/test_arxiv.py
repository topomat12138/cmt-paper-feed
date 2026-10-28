import copy
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from feed.cli import main
from feed.config import load_config
from feed.sources import ArxivListing, SourceError, get_source
from feed.sources.arxiv_listing import ListingRef, NS, parse_listing, parse_metadata
from feed.store import PaperStore

FIXTURES = Path(__file__).parent / "fixtures/arxiv/2026-10-28"
DAY = date(2026, 10, 28)
SEEN = datetime(2026, 10, 28, 1, 30, tzinfo=timezone.utc)
CATEGORIES = ["cond-mat.mes-hall", "cond-mat.supr-con"]
CONFIG = Path(__file__).parents[1] / "config/sources.yml"


class Clock:
    now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


def recording(name):
    return (FIXTURES / name).read_bytes()


def replay(request):
    if request.url.host == "arxiv.org":
        category = request.url.path.split("/")[2]
        return httpx.Response(200, content=recording(f"{category}.html"))
    assert request.url.host == "export.arxiv.org"
    return httpx.Response(200, content=recording("api-000.xml"))


def source(handler=replay, categories=CATEGORIES):
    clock = Clock()
    calls = []

    def transport(request):
        calls.append((clock.now, request))
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    return ArxivListing(categories=categories, client=client, clock=clock.clock, sleep=clock.sleep), client, calls


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_default_categories_and_registration():
    assert load_config(CONFIG).sources.arxiv_listing.categories == CATEGORIES
    assert get_source("arxiv_listing") is ArxivListing


@pytest.mark.parametrize("content", [
    "sources: {arxiv_listing: {categories: []}}",
    "sources: {arxiv_listing: {categories: [../bad]}}",
    "sources: {arxiv_listing: {categories: [cond-mat.mes-hall], weights: [1]}}",
])
def test_configuration_allows_only_source_and_categories(tmp_path, content):
    path = tmp_path / "sources.yml"
    path.write_text(content)
    with pytest.raises(ValidationError):
        load_config(path)


@pytest.mark.parametrize("category,counts", [
    (CATEGORIES[0], {"new": 15, "cross": 12, "replacement": 11}),
    (CATEGORIES[1], {"new": 9, "cross": 2, "replacement": 6}),
])
def test_recorded_listing_keeps_all_three_sections(category, counts):
    page = parse_listing(recording(f"{category}.html").decode())
    assert page.announced == DAY
    assert page.counts == counts
    assert len(page.entries) == sum(counts.values())
    assert len({entry.arxiv_id for entry in page.entries}) == len(page.entries)


def test_recorded_source_batches_deduplicates_and_maps_contract(tmp_path):
    adapter, client, calls = source()
    with client:
        papers = adapter.fetch(day=DAY, seen_at=SEEN)
        assert len(papers) == 53
        expected = json.loads(recording("recording.json"))["ids"]
        assert [p.arxiv_id for p in papers] == expected
        assert {p.listing_type for p in papers} == {"new", "cross", "replacement"}
        assert all(p.sources == ["arxiv_listing"] and p.announced == DAY for p in papers)
        assert all(p.submitted and p.title and p.abstract and p.authors and p.primary_category for p in papers)
        assert all(p.arxiv_version >= 1 and p.links["pdf"].startswith("https://arxiv.org/pdf/") for p in papers)
        assert any(p.doi for p in papers) and any(p.journal_ref for p in papers)
        assert any(p.arxiv_version > 1 and p.listing_type == "replacement" for p in papers)
        assert any(p.listing_type == "cross" and p.primary_category not in CATEGORIES for p in papers)
        mapped = next(p for p in papers if p.arxiv_id == "2610.36856")
        assert mapped.submitted == date(2026, 10, 27)
        assert mapped.doi == "10.1021/acsnano.5c06397"
        assert mapped.journal_ref == "ACS Nano 19, 33789 (2025)"
        assert mapped.authors[0] == "Yinping Pan"
        assert len(calls) == 3  # two listings plus one metadata batch, not 53 calls
        assert calls[2][1].url.params["id_list"].split(",") == expected
        assert int(calls[2][1].url.params["max_results"]) == 53
        assert all(right[0] - left[0] >= 3 for left, right in zip(calls, calls[1:]))
        assert all("cmt-paper-feed" in req.headers["User-Agent"] for _, req in calls)
        store = PaperStore(tmp_path)
        result = store.upsert(papers, day=DAY)
        assert len(result.new) == 53 and not result.errors
        before = snapshot(tmp_path)
        store.upsert(adapter.fetch(day=DAY, seen_at=SEEN + timedelta(hours=1)), day=DAY)
        assert snapshot(tmp_path) == before


def test_cross_category_listing_type_is_deterministic():
    pages = [parse_listing(recording(f"{category}.html").decode()) for category in CATEGORIES]
    appearances = {}
    for page in pages:
        for ref in page.entries:
            appearances.setdefault(ref.arxiv_id, set()).add(ref.listing_type)
    assert sum(len(p.entries) for p in pages) == 55
    assert len(appearances) == 53
    adapter, client, _ = source(categories=list(reversed(CATEGORIES)))
    with client:
        result = {p.arxiv_id: p for p in adapter.fetch(day=DAY, seen_at=SEEN)}
    priority = ["new", "cross", "replacement"]
    for key, kinds in appearances.items():
        assert result[key].listing_type == next(kind for kind in priority if kind in kinds)


def test_source_reuses_store_merge_rules(tmp_path, paper):
    adapter, client, _ = source()
    with client:
        batch = adapter.fetch(day=DAY, seen_at=SEEN)
    target = batch[0]
    existing = paper(arxiv_id=target.arxiv_id, arxiv_version=99, title="More recent metadata",
                     sources=["scholar_alert"], first_seen="2026-09-01T00:00:00Z")
    store = PaperStore(tmp_path)
    store.upsert([existing], day=date(2026, 10, 27))
    store.upsert(batch, day=DAY)
    merged = store.load()[existing.paper_id]
    assert merged.arxiv_version == 99
    assert merged.title == existing.title
    assert merged.first_seen == existing.first_seen
    assert merged.sources == ["arxiv_listing", "scholar_alert"]
    assert len(store.load()) == 53


def test_weekend_with_previous_listing_writes_empty_daily(tmp_path):
    adapter, client, calls = source()
    with client:
        papers = adapter.fetch(day=date(2026, 10, 31), seen_at=SEEN)
    assert papers == []
    assert len(calls) == 2  # no metadata API request
    PaperStore(tmp_path).upsert(papers, day=date(2026, 10, 31))
    assert json.loads((tmp_path / "daily/2026-10-31.json").read_bytes()) == {
        "date": "2026-10-31", "new": [], "updated": [],
    }


def test_old_date_is_not_silently_replaced_by_todays_papers():
    adapter, client, _ = source()
    with client, pytest.raises(SourceError, match="predates"):
        adapter.fetch(day=date(2026, 10, 27), seen_at=SEEN)


@pytest.mark.parametrize("damage", ["container", "date", "entry", "count", "partial"])
def test_bad_or_incomplete_listing_fails_closed(damage):
    html = recording(f"{CATEGORIES[0]}.html").decode()
    if damage == "container":
        html = "<html>upstream unavailable</html>"
    elif damage == "date":
        html = html.replace("Wednesday, 28 October 2026", "invalid date")
    elif damage == "entry":
        html = html.replace('title="Abstract"', 'title="Missing"', 1)
    elif damage == "count":
        html = html.replace("Total of 38 entries", "Total of 39 entries")
    else:
        html = html.replace("showing 15 of 15", "showing 10 of 15")
    with pytest.raises(SourceError):
        parse_listing(html)


def api_fixture():
    root = ET.fromstring(recording("api-000.xml"))
    refs = {}
    for category in CATEGORIES:
        refs.update({r.arxiv_id: r for r in parse_listing(recording(f"{category}.html").decode()).entries})
    return root, refs


@pytest.mark.parametrize("damage", ["invalid", "missing", "duplicate", "unexpected", "title", "published", "version", "category", "unversioned"])
def test_bad_or_incomplete_api_feed_is_not_saved(damage):
    root, refs = api_fixture()
    entry = root.find("atom:entry", NS)
    if damage == "missing":
        root.remove(entry)
    elif damage == "duplicate":
        root.append(copy.deepcopy(entry))
    elif damage == "unexpected":
        entry.find("atom:id", NS).text = "http://arxiv.org/abs/2610.99999v1"
    elif damage in {"title", "published"}:
        entry.remove(entry.find(f"atom:{damage}", NS))
    elif damage == "version":
        key = next(iter(refs))
        refs[key] = ListingRef(key, 99, "replacement")
    elif damage == "category":
        entry.find("atom:category", NS).attrib.pop("term")
    elif damage == "unversioned":
        entry.find("atom:id", NS).text = entry.find("atom:id", NS).text.rsplit("v", 1)[0]
    xml = b"<invalid" if damage == "invalid" else ET.tostring(root)
    with pytest.raises(SourceError):
        parse_metadata(xml, refs, announced=DAY, seen_at=SEEN)


def synthetic_listing(ids):
    soup = BeautifulSoup(recording(f"{CATEGORIES[0]}.html"), "html.parser")
    root = soup.select_one("#dlpage")
    for dl in root.find_all("dl"):
        dl.decompose()
    for paging in root.select(".paging"):
        paging.string = f"Total of {len(ids)} entries"
    dl = soup.new_tag("dl")
    heading = soup.new_tag("h3")
    heading.string = f"New submissions (showing {len(ids)} of {len(ids)} entries)"
    dl.append(heading)
    for key in ids:
        dt = soup.new_tag("dt")
        anchor = soup.new_tag("a", href=f"/abs/{key}", title="Abstract")
        dt.append(anchor)
        dl.append(dt)
    root.append(dl)
    return str(soup)


def test_metadata_batches_never_exceed_100_and_are_rate_limited():
    ids = [f"2610.{i:05d}" for i in range(201)]
    template, _ = api_fixture()
    original = template.find("atom:entry", NS)

    def handler(request):
        if request.url.host == "arxiv.org":
            return httpx.Response(200, text=synthetic_listing(ids))
        batch = request.url.params["id_list"].split(",")
        feed = ET.Element(f"{{{NS['atom']}}}feed")
        for key in batch:
            entry = copy.deepcopy(original)
            entry.find("atom:id", NS).text = f"http://arxiv.org/abs/{key}v1"
            feed.append(entry)
        return httpx.Response(200, content=ET.tostring(feed))

    adapter, client, calls = source(handler, categories=CATEGORIES[:1])
    with client:
        papers = adapter.fetch(day=DAY, seen_at=SEEN)
    assert len(papers) == 201
    assert [len(req.url.params["id_list"].split(",")) for _, req in calls[1:]] == [100, 100, 1]
    assert all(b[0] - a[0] >= 3 for a, b in zip(calls, calls[1:]))


def test_explicit_empty_listing_does_not_request_metadata():
    adapter, client, calls = source(lambda request: httpx.Response(200, text=synthetic_listing([])))
    with client:
        assert adapter.fetch(day=DAY, seen_at=SEEN) == []
    assert len(calls) == 2


def test_transient_error_retries_respect_spacing():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429) if attempts == 1 else replay(request)

    adapter, client, calls = source(handler)
    with client:
        assert len(adapter.fetch(day=DAY, seen_at=SEEN)) == 53
    assert len(calls) == 4
    assert all(b[0] - a[0] >= 3 for a, b in zip(calls, calls[1:]))


@pytest.mark.parametrize("failure", [None, "metadata", "network"])
def test_cli_ingest_replays_recordings_without_network(tmp_path, monkeypatch, capsys, failure):
    import feed.cli as cli
    original_client = httpx.Client
    clock = Clock()

    def handler(request):
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        if failure == "metadata" and request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=b"<invalid")
        return replay(request)

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cli, "get_source", lambda name: lambda **kwargs: ArxivListing(**kwargs, sleep=clock.sleep, clock=clock.clock))
    args = ["ingest", "--config", str(CONFIG), "--data-dir", str(tmp_path), "--date", DAY.isoformat()]
    assert main(args) == (1 if failure else 0)
    if failure:
        assert snapshot(tmp_path) == {}
        assert "ingest failed" in capsys.readouterr().err
    else:
        assert json.loads(capsys.readouterr().out)["fetched"] == 53
        before = snapshot(tmp_path)
        assert main(args) == 0
        assert snapshot(tmp_path) == before
