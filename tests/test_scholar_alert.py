import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from feed.errors import ScholarIssue
from feed.schema import Paper
from feed.sources.base import get_source
from feed.sources.scholar_alert import ScholarAlert
from feed.store import PaperStore

SEEN = datetime(2026, 10, 31, 1, 30, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "scholar"


class FakeResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, items, *, seen_at):
        self.calls.append((items, seen_at))
        return SimpleNamespace(
            papers=[Paper(title=item.title, sources=["scholar_alert"], first_seen=seen_at,
                          last_updated=seen_at) for item in items],
            errors=[], counts={"title_only": len(items)})


@pytest.fixture
def client():
    def unexpected(request):
        raise AssertionError("validation tests must not request network metadata")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        yield client


def test_registration_and_synthetic_dispatch_validation(client, capsys):
    assert get_source("scholar_alert") is ScholarAlert
    resolver = FakeResolver()
    payload = json.loads((FIXTURES / "dispatch.json").read_text())
    source = ScholarAlert(payload=payload, client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert result.announcement_dates == (date(2026, 10, 31),)
    assert len(result.papers) == 3
    assert [error.code for error in result.errors] == ["invalid_item"]
    assert result.errors[0].item_index == 4
    assert source.stats == {"received": 5, "valid": 4, "deduplicated": 1,
                            "resolution_counts": {"arxiv_url": 0, "doi_url": 0, "arxiv_title": 0,
                                                  "openalex_title": 0, "title_only": 3}}
    items, observed = resolver.calls[0]
    assert observed == SEEN
    assert any(item.urls == ("https://arxiv.org/abs/2610.99901v2",) for item in items)
    assert any(item.title == "Synthetic Scholar-only document" and item.urls == () for item in items)
    assert all(paper.sources == ["scholar_alert"] and paper.announced is None and paper.listing_type is None
               for paper in result.papers)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", [
    None, [], "private payload", {}, {"version": 0, "items": []},
    {"version": True, "items": []}, {"version": 1.0, "items": []},
    {"version": "1", "items": []}, {"version": 1, "items": {}},
    {"version": 1, "items": [{"title": "Synthetic record"}] * 201},
])
def test_invalid_envelopes_are_counted_without_resolving_or_echoing(client, payload, capsys):
    resolver = FakeResolver()
    source = ScholarAlert(payload=payload, client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert result.papers == [] and len(result.errors) == 1
    assert result.errors[0].code == "invalid_item"
    assert resolver.calls == []
    assert "private" not in result.errors[0].model_dump_json()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("item", [
    None, "private item", [], {"title": None}, {"title": 123}, {"title": ""},
    {"title": "   "}, {"title": "x" * 1001}, {"title": "..."}, {"title": "Bad \ud800 title"},
    {"title": "Bad URL", "url": 123}, {"title": "Bad URL", "url": "ftp://example.org/paper"},
    {"title": "Bad URL", "url": "https://"},
    {"title": "Bad URL", "url": "https://example.org/" + "x" * 2000},
    {"title": "Bad URL", "url": "https://private:token@example.org/paper"},
    {"title": "Bad URL", "url": "https://example.org:bad/paper"},
    {"title": "Bad URL", "url": "https://example.org/\ud800"},
])
def test_invalid_items_skip_only_the_item(client, item):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "items": [item, {"title": "Valid synthetic document"}]},
                          client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert len(result.papers) == 1 and result.papers[0].title == "Valid synthetic document"
    assert len(result.errors) == 1 and result.errors[0].item_index == 0
    assert source.stats["received"] == 2 and source.stats["valid"] == 1
    assert len(resolver.calls[0][0]) == 1


@pytest.mark.parametrize("url", [
    "https://scholar.google.com/scholar?token=private-query",
    "https://scholar.google.co.uk/scholar?token=private-query",
    "https://google.com/search?token=private-query",
    "https://www.google.co.jp/search?token=private-query",
    "https://SCHOLAR.GOOGLE.COM:443/scholar?token=private-query",
])
def test_scholar_and_google_links_become_null(client, url):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "items": [{"title": "Synthetic title lookup", "url": url}]},
                          client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert resolver.calls[0][0][0].urls == ()
    assert result.errors == []
    assert "private-query" not in json.dumps(source.stats)


def test_dedup_retains_multiple_public_paths_and_is_input_order_independent(client):
    items = [{"title": "  Synthetic  paper  ", "url": "https://example.org/path?private=canary#fragment"},
             {"title": "SYNTHETIC PAPER", "url": "https://arxiv.org/abs/2610.99901v2"},
             {"title": "Synthetic paper", "url": "https://example.org/path?other=tracking"}]
    calls = []
    for batch in (items, list(reversed(items))):
        resolver = FakeResolver()
        source = ScholarAlert(payload={"version": 1, "items": batch}, client=client, resolver=resolver)
        source.fetch(seen_at=SEEN)
        assert source.stats["deduplicated"] == 2
        calls.append(resolver.calls[0][0])
    assert calls[0] == calls[1]
    assert calls[0][0].urls == ("https://arxiv.org/abs/2610.99901v2", "https://example.org/path")


def test_unicode_and_latex_chemical_titles_deduplicate(client):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "items": [
        {"title": "Synthetic MnBi₂Te₄ film"}, {"title": r"Synthetic MnBi$_2$Te$_4$ film"}]},
        client=client, resolver=resolver)
    source.fetch(seen_at=SEEN)
    assert source.stats["valid"] == 2 and source.stats["deduplicated"] == 1
    assert len(resolver.calls[0][0]) == 1


def test_payload_extras_and_url_tracking_are_not_retained(client, capsys):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "alert_name": "private-alert", "email": "private@example.org",
                                   "items": [{"title": "Synthetic public paper", "author_trigger": "private-author",
                                              "url": "https://example.org/paper?private-token=canary#private-fragment"}]},
                          client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    serialized = json.dumps(source.stats) + "".join(paper.model_dump_json() for paper in result.papers)
    serialized += "".join(error.model_dump_json() for error in result.errors)
    for private in ("private-alert", "private@example.org", "private-author", "private-token", "private-fragment"):
        assert private not in serialized
    assert resolver.calls[0][0][0].urls == ("https://example.org/paper",)
    assert capsys.readouterr() == ("", "")


def test_runtime_utc_date_overrides_optional_day_and_repeated_fetch_resets_counts(client):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "items": [{"title": "Synthetic weekend document"}]},
                          client=client, resolver=resolver)
    local_time = datetime(2026, 10, 30, 21, 30, tzinfo=timezone(timedelta(hours=-4)))
    first = source.fetch(seen_at=local_time, day=date(2026, 10, 29))
    assert first.announcement_dates == (date(2026, 10, 31),)
    assert first.papers[0].first_seen == SEEN
    again = source.fetch(seen_at=local_time)
    assert again == first and source.stats["received"] == source.stats["valid"] == 1


def test_unexpected_resolver_failure_retains_title_only_records(client, capsys):
    class Broken:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("private-token https://example.org/?query=private")

    source = ScholarAlert(payload={"version": 1, "items": [{"title": "First synthetic paper"},
                                                           {"title": "Second synthetic paper"}]},
                          client=client, resolver=Broken())
    result = source.fetch(seen_at=SEEN)
    assert len(result.papers) == 2 and all(paper.paper_id.startswith("title:") for paper in result.papers)
    assert result.errors[0].code == "resolution_failure"
    assert "private" not in result.errors[0].model_dump_json()
    assert source.stats["resolution_counts"]["title_only"] == 2
    assert capsys.readouterr() == ("", "")


def test_resolution_errors_and_counts_are_preserved(client):
    class Partial(FakeResolver):
        def resolve(self, items, *, seen_at):
            result = super().resolve(items, seen_at=seen_at)
            result.errors = [ScholarIssue(code="resolution_failure", stage="resolve",
                                          message="external lookup unavailable")]
            return result

    source = ScholarAlert(payload={"version": 1, "items": [{"title": "Synthetic fallback"}]},
                          client=client, resolver=Partial())
    result = source.fetch(seen_at=SEEN)
    assert len(result.papers) == 1 and result.errors[0].message == "external lookup unavailable"
    assert source.stats["resolution_counts"]["title_only"] == 1


def test_empty_batch_is_valid_and_does_not_resolve(client):
    resolver = FakeResolver()
    source = ScholarAlert(payload={"version": 1, "items": []}, client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert result.papers == result.errors == [] and resolver.calls == []


def test_maximum_batch_size_and_title_boundary_are_accepted(client):
    resolver = FakeResolver()
    items = [{"title": f"Synthetic document {number}"} for number in range(199)]
    items.append({"title": "x" * 1000})
    source = ScholarAlert(payload={"version": 1, "items": items}, client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert len(result.papers) == 200 and result.errors == []


def test_url_length_boundary_is_accepted(client):
    resolver = FakeResolver()
    prefix = "https://example.org/"
    url = prefix + "x" * (2000 - len(prefix))
    source = ScholarAlert(payload={"version": 1, "items": [{"title": "Synthetic long public path", "url": url}]},
                          client=client, resolver=resolver)
    result = source.fetch(seen_at=SEEN)
    assert len(result.papers) == 1 and result.errors == []
    assert resolver.calls[0][0][0].urls == (url,)


def test_default_resolver_replays_public_metadata_and_weekend_storage_is_idempotent(tmp_path):
    requests = []

    class Clock:
        value = 0.0

        def now(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    clock = Clock()

    def respond(request):
        requests.append(request)
        assert request.url.host in {"export.arxiv.org", "api.openalex.org"}
        assert "https://github.com/topomat12138/cmt-paper-feed" in request.headers["user-agent"]
        assert "@" not in request.headers["user-agent"]
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=b'<feed xmlns="http://www.w3.org/2005/Atom"/>')
        if request.url.path == "/works":
            return httpx.Response(200, json={"meta": {"count": 0}, "results": []})
        assert request.url.path == "/works/https://doi.org/10.1103/abcd-1234"
        return httpx.Response(200, json={"doi": "https://doi.org/10.1103/abcd-1234",
                                       "title": "Synthetic journal document",
                                       "authorships": [{"author": {"display_name": "Synthetic Author"}}],
                                       "abstract_inverted_index": {"A": [0], "synthetic": [1], "abstract": [2]},
                                       "primary_location": {"source": {"display_name": "Synthetic Journal"}}})

    payload = {"version": 1, "items": [
        {"title": "Synthetic journal document", "url": "https://link.aps.org/pdf/10.1103/abcd-1234?private=query"},
        {"title": "Synthetic unresolved document", "url": "https://scholar.google.com/scholar?private=query"},
    ]}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = ScholarAlert(payload=payload, client=client, sleep=clock.sleep, clock=clock.now)
        result = source.fetch(seen_at=SEEN)
        assert result.errors == [] and len(result.papers) == 2
        assert source.stats["resolution_counts"]["doi_url"] == 1
        assert source.stats["resolution_counts"]["title_only"] == 1
        store = PaperStore(tmp_path)
        first = store.upsert(result.papers, day=result.announcement_dates[0])
        assert len(first.new) == 2 and (tmp_path / "daily/2026-10-31.json").exists()
        before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        again = source.fetch(seen_at=SEEN + timedelta(minutes=5))
        store.upsert(again.papers, day=again.announcement_dates[0])
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert all("private" not in str(request.url) for request in requests)
