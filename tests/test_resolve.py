import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
import pytest

from feed.resolve import (
    OPENALEX_URL, ResolveItem, Resolver, extract_arxiv, extract_doi, reconstruct_abstract,
)
from feed.sources.arxiv_listing import API_URL, NS, USER_AGENT

SEEN = datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


def atom(entries=(), *, total=None):
    root = ET.Element(f"{{{NS['atom']}}}feed")
    if total is not None:
        ET.SubElement(root, "{http://a9.com/-/spec/opensearch/1.1/}totalResults").text = str(total)
    for item in entries:
        entry = ET.SubElement(root, f"{{{NS['atom']}}}entry")
        for field, value in {
            "id": "http://arxiv.org/abs/" + item["id"],
            "title": item.get("title", "A synthetic resolved paper"),
            "summary": item.get("abstract", "An artificial abstract for offline tests."),
            "published": "2026-10-27T00:00:00Z",
        }.items():
            ET.SubElement(entry, f"{{{NS['atom']}}}{field}").text = value
        author = ET.SubElement(entry, f"{{{NS['atom']}}}author")
        ET.SubElement(author, f"{{{NS['atom']}}}name").text = "Synthetic Author"
        ET.SubElement(entry, f"{{{NS['atom']}}}category", {"term": "cond-mat.supr-con"})
        ET.SubElement(entry, f"{{{NS['arxiv']}}}primary_category", {"term": "cond-mat.supr-con"})
        ET.SubElement(entry, f"{{{NS['atom']}}}link", {"title": "pdf", "type": "application/pdf"})
        if item.get("doi"):
            ET.SubElement(entry, f"{{{NS['arxiv']}}}doi").text = item["doi"]
    return ET.tostring(root)


def work(*, title="An artificial journal paper", doi="10.1234/example", identifier="W1", locations=None):
    return {
        "id": "https://openalex.org/" + identifier,
        "title": title,
        "doi": "https://doi.org/" + doi if doi else None,
        "authorships": [{"author": {"display_name": "First Example"}},
                       {"author": {"display_name": "Second Example"}}],
        "abstract_inverted_index": {"An": [0], "artificial": [1], "abstract.": [2]},
        "primary_location": {"source": {"display_name": "Synthetic Journal"}},
        "locations": locations or [],
        # Unused fields must never be copied into Paper records.
        "keywords": [{"display_name": "not paper metadata"}],
    }


def resolve(handler, items):
    clock, calls = Clock(), []

    def transport(request):
        calls.append((clock.now, request))
        assert request.url.host in {"export.arxiv.org", "api.openalex.org"}
        assert request.headers["User-Agent"] == USER_AGENT
        assert "@" not in request.headers["User-Agent"]
        assert "authorization" not in request.headers
        assert "api_key" not in request.url.params and "mailto" not in request.url.params
        return handler(request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = Resolver(client=client, sleep=clock.sleep, clock=clock.clock).resolve(items, seen_at=SEEN)
    return result, calls


@pytest.mark.parametrize("url,expected", [
    ("https://arxiv.org/abs/2610.12345", ("2610.12345", None)),
    ("https://arxiv.org/pdf/2610.12345", ("2610.12345", None)),
    ("http://arxiv.org/pdf/2610.12345v3.pdf?download=1#section", ("2610.12345", 3)),
    ("https://arxiv.org/abs/cond-mat/0601001v2", ("cond-mat/0601001", 2)),
    ("https://arxiv.org/pdf/cond-mat/0601001.pdf", ("cond-mat/0601001", None)),
    ("https://arxiv.org.evil.example/abs/2610.12345", None),
    ("https://example.org/abs/2610.12345", None),
    ("https://arxiv.org/abs/2610.12345v0", None),
    ("https://arxiv.org/abs/2610.12345/extra", None),
])
def test_arxiv_url_variants(url, expected):
    assert extract_arxiv(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://doi.org/10.1234/Example?private=discard#fragment", "10.1234/example"),
    ("https://link.aps.org/pdf/10.1103/l4wt-87js", "10.1103/l4wt-87js"),
    ("https://link.aps.org/doi/10.1103/a1bc-23de", "10.1103/a1bc-23de"),
    ("https://journals.aps.org/prb/abstract/10.1103/PhysRevB.123.456789", "10.1103/physrevb.123.456789"),
    ("https://onlinelibrary.wiley.com/doi/abs/10.1002/pssb.70346", "10.1002/pssb.70346"),
    ("https://onlinelibrary.wiley.com/doi/full/10.1002/example.12345", "10.1002/example.12345"),
    ("https://onlinelibrary.wiley.com/doi/pdf/10.1002/example.12345", "10.1002/example.12345"),
    ("https://iopscience.iop.org/article/10.1088/1361-648X/ab1234/pdf", "10.1088/1361-648x/ab1234"),
    ("https://www.nature.com/articles/s41567-026-01234-5", "10.1038/s41567-026-01234-5"),
    ("https://www.nature.com/articles/nphys1234.pdf?tracking=discard", "10.1038/nphys1234"),
    ("https://www.nature.com/articles/nphys1234%3Fprivate-token%3Dcanary", "10.1038/nphys1234"),
    ("https://www.nature.com/articles/nphys1234%23private-fragment", "10.1038/nphys1234"),
    ("https://example.org/10.1234/demo/full.pdf", "10.1234/demo"),
    ("https://example.org/article/10.1234%2Fdemo/abstract", "10.1234/demo"),
    ("https://www.sciencedirect.com/science/article/pii/S0577907326003874", None),
    ("https://example.org/article?doi=10.1234/not-from-path", None),
])
def test_publisher_url_doi_extraction(url, expected):
    assert extract_doi(url) == expected


def test_direct_arxiv_is_batched_without_title_or_openalex_lookup():
    items = [ResolveItem("One synthetic title", ("https://arxiv.org/abs/2610.10001v2?token=discard",)),
             ResolveItem("Another synthetic title", ("https://arxiv.org/pdf/cond-mat/0601001.pdf",))]

    def handler(request):
        assert request.url.host == "export.arxiv.org"
        assert "search_query" not in request.url.params
        assert request.url.params["id_list"] == "2610.10001v2,cond-mat/0601001"
        return httpx.Response(200, content=atom([{"id": "2610.10001v2"}, {"id": "cond-mat/0601001v1"}]))

    result, calls = resolve(handler, items)
    assert len(calls) == 1 and len(result.papers) == 2
    assert result.counts["arxiv_url"] == 2 and result.errors == []
    for paper in result.papers:
        assert paper.sources == ["scholar_alert"]
        assert paper.announced is paper.listing_type is None
        assert paper.authors == ["Synthetic Author"] and paper.abstract
        assert "?" not in json.dumps(paper.links)


def test_unique_arxiv_title_phrase_match_then_metadata_request():
    title = "An exact artificial title"

    def handler(request):
        assert request.url.host == "export.arxiv.org"
        if "search_query" in request.url.params:
            assert request.url.params["search_query"] == 'ti:"an exact artificial title"'
            return httpx.Response(200, content=atom([{"id": "2610.10001v2", "title": title.upper()},
                                                    {"id": "2610.10002v1", "title": "Another paper"}]))
        return httpx.Response(200, content=atom([{"id": "2610.10001v2", "title": title}]))

    result, calls = resolve(handler, [ResolveItem(title)])
    assert result.papers[0].paper_id == "arxiv:2610.10001"
    assert result.counts["arxiv_title"] == 1
    assert len(calls) == 2 and calls[1][0] - calls[0][0] >= 3


def test_unicode_subscript_matches_arxiv_latex_title():
    title = "Magnetic order in MnBi₂Te₄ thin films"
    latex = r"Magnetic order in MnBi$_2$Te$_4$ thin films"

    def handler(request):
        return httpx.Response(200, content=atom([{"id": "2610.10001v1", "title": latex}]))

    result, calls = resolve(handler, [ResolveItem(title)])
    assert result.papers[0].paper_id == "arxiv:2610.10001"
    assert result.counts["arxiv_title"] == 1


def test_multiple_title_candidates_are_not_guessed():
    title = "A synthetic ambiguous title"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom([{"id": "2610.10001v1", "title": title},
                                                    {"id": "2610.10002v1", "title": title}]))
        return httpx.Response(200, json={"meta": {"count": 2}, "results": [
            work(title=title, identifier="W1"), work(title=title, identifier="W2", doi="10.1234/other"),
        ]})

    result, calls = resolve(handler, [ResolveItem(title)])
    assert result.papers[0].paper_id.startswith("title:")
    assert result.counts["title_only"] == 1
    assert all("id_list" not in request.url.params for _, request in calls)


@pytest.mark.parametrize("ending", ["…", "..."])
def test_long_truncated_title_accepts_unique_prefix(ending):
    prefix = "An artificial long title describing superconducting transport in engineered multilayer materials"
    full = prefix + " with controlled interfaces"

    def handler(request):
        return httpx.Response(200, content=atom([{"id": "2610.10001v1", "title": full}]))

    result, calls = resolve(handler, [ResolveItem(prefix + ending)])
    assert result.papers[0].title == full
    assert result.counts["arxiv_title"] == 1 and len(calls) == 2


def test_short_truncated_title_never_triggers_unsafe_prefix_lookup():
    def forbidden(request):
        raise AssertionError("a short truncated title must not be looked up")

    result, calls = resolve(forbidden, [ResolveItem("Short ambiguous title…")])
    assert result.papers[0].paper_id.startswith("title:")
    assert result.counts["title_only"] == 1 and calls == []


def test_truncated_prefix_requires_a_unique_candidate():
    prefix = "An artificial long title describing superconducting transport in engineered multilayer materials"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom([{"id": "2610.10001v1", "title": prefix + " one"},
                                                    {"id": "2610.10002v1", "title": prefix + " two"}]))
        return httpx.Response(200, json={"meta": {"count": 0}, "results": []})

    result, _ = resolve(handler, [ResolveItem(prefix + "...")])
    assert result.counts["title_only"] == 1


def test_openalex_exact_match_builds_allowlisted_doi_metadata():
    title = "An artificial journal paper"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom())
        assert request.url.path == "/works"
        assert request.url.params["search"] == title.lower()
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json={"meta": {"count": 1}, "results": [work(title=title)]})

    result, calls = resolve(handler, [ResolveItem(title, ("https://www.sciencedirect.com/science/article/pii/S0577907326003874",))])
    paper = result.papers[0]
    assert paper.paper_id == "doi:10.1234/example"
    assert paper.authors == ["First Example", "Second Example"]
    assert paper.abstract == "An artificial abstract."
    assert paper.journal_ref == "Synthetic Journal"
    assert paper.links == {"doi": "https://doi.org/10.1234/example"}
    assert paper.sources == ["scholar_alert"] and paper.announced is paper.listing_type is None
    assert result.counts["openalex_title"] == 1 and len(calls) == 2
    assert "keywords" not in paper.model_dump()


def test_doi_url_uses_openalex_without_title_queries():
    def handler(request):
        assert request.url.host == "api.openalex.org"
        assert request.url.path == "/works/https://doi.org/10.1103/l4wt-87js"
        return httpx.Response(200, json=work(doi="10.1103/l4wt-87js"))

    result, calls = resolve(handler, [ResolveItem("A supplied public title", ("https://link.aps.org/pdf/10.1103/l4wt-87js",))])
    assert result.papers[0].paper_id == "doi:10.1103/l4wt-87js"
    assert result.counts["doi_url"] == 1 and len(calls) == 1


def test_openalex_location_arxiv_is_fetched_and_known_doi_is_retained():
    title = "An artificial title with an arXiv location"

    def handler(request):
        if "search_query" in request.url.params:
            return httpx.Response(200, content=atom())
        if request.url.host == "api.openalex.org":
            matched = work(title=title, locations=[{"landing_page_url": "https://arxiv.org/abs/2610.10001v2?private=discard"}])
            return httpx.Response(200, json={"meta": {"count": 1}, "results": [matched]})
        return httpx.Response(200, content=atom([{"id": "2610.10001v2", "title": title}]))

    result, _ = resolve(handler, [ResolveItem(title)])
    assert result.papers[0].paper_id == "arxiv:2610.10001"
    assert result.papers[0].doi == "10.1234/example"
    assert result.counts["openalex_title"] == 1


def test_incomplete_search_pages_cannot_establish_unique_title_match():
    title = "An artificial title"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom([{"id": "2610.10001v1", "title": title}], total=2))
        return httpx.Response(200, json={"meta": {"count": 2}, "results": [work(title=title)]})

    result, calls = resolve(handler, [ResolveItem(title)])
    assert result.counts["title_only"] == 1
    assert all("id_list" not in request.url.params for _, request in calls)


def test_arxiv_partial_feed_retries_then_salvages_valid_individual_entries():
    items = [ResolveItem("The valid paper", ("https://arxiv.org/abs/2610.10001",)),
             ResolveItem("The missing abstract paper", ("https://arxiv.org/abs/2610.10002",))]

    def handler(request):
        return httpx.Response(200, content=atom([{"id": "2610.10001v1"}, {"id": "2610.10002v1", "abstract": ""}]))

    result, calls = resolve(handler, items)
    assert len(calls) == 3
    assert [timestamp for timestamp, _ in calls] == [0, 3, 6]
    assert result.papers[0].paper_id == "arxiv:2610.10001"
    assert result.papers[1].paper_id.startswith("title:")
    assert result.counts["arxiv_url"] == result.counts["title_only"] == 1
    assert result.errors[0].stage == "metadata" and result.errors[0].item_index == 1


def test_arxiv_missing_entry_retries_with_shared_three_second_budget_then_recovers():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, text="private response content")
        if len(attempts) == 2:
            return httpx.Response(200, content=atom())
        return httpx.Response(200, content=atom([{"id": "2610.10001v1"}]))

    result, calls = resolve(handler, [ResolveItem("A supplied title", ("https://arxiv.org/abs/2610.10001",))])
    assert result.papers[0].paper_id == "arxiv:2610.10001"
    assert result.errors == [] and [timestamp for timestamp, _ in calls] == [0, 3, 6]


def test_arxiv_batches_are_at_most_one_hundred_and_share_rate_limit():
    items = [ResolveItem(f"Synthetic paper {number}", (f"https://arxiv.org/abs/2610.{number:05}",))
             for number in range(200)]

    def handler(request):
        ids = request.url.params["id_list"].split(",")
        assert len(ids) <= 100
        return httpx.Response(200, content=atom([{"id": identifier + "v1"} for identifier in ids]))

    result, calls = resolve(handler, items)
    assert result.counts["arxiv_url"] == len(result.papers) == 200
    assert len(calls) == 2 and calls[1][0] - calls[0][0] >= 3


def test_failed_remote_item_falls_back_without_losing_other_papers_or_leaking_data(capsys):
    def handler(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(500, text="secret-token private@example.org https://site/?tracking=private")
        return httpx.Response(200, content=atom([{"id": "2610.10001v1"}]))

    result, calls = resolve(handler, [
        ResolveItem("A public journal title", ("https://publisher.example/10.1234/unavailable?tracking=secret",)),
        ResolveItem("A public arXiv title", ("https://arxiv.org/abs/2610.10001",)),
    ])
    assert len(result.papers) == 2 and result.papers[0].paper_id.startswith("title:")
    assert result.papers[1].paper_id == "arxiv:2610.10001"
    assert len([request for _, request in calls if request.url.host == "api.openalex.org"]) == 3
    serialized = json.dumps([paper.model_dump(mode="json") for paper in result.papers])
    serialized += json.dumps([error.model_dump(mode="json") for error in result.errors])
    for private in ("secret-token", "private@example.org", "tracking", "secret"):
        assert private not in serialized
    assert capsys.readouterr() == ("", "")


def test_openalex_must_return_the_requested_doi():
    result, _ = resolve(lambda request: httpx.Response(200, json=work(doi="10.1234/wrong")),
                        [ResolveItem("A public title", ("https://doi.org/10.1234/requested",))])
    assert result.papers[0].paper_id.startswith("title:")
    assert result.errors[0].code == "resolution_failure"


def test_reconstructed_abstract_uses_positions_and_rejects_unsafe_index():
    assert reconstruct_abstract({"word": [1, 3], "First": [0], "repeated": [2]}) == "First word repeated word"
    assert reconstruct_abstract(None) is None
    with pytest.raises(Exception, match="invalid OpenAlex abstract"):
        reconstruct_abstract({"bad": [False]})


@pytest.mark.parametrize("candidates", [1, 2])
def test_openalex_truncated_prefix_requires_unique_match(candidates):
    prefix = "A synthetic extended title describing electronic properties of engineered multilayer structures"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=atom())
        works = [work(title=prefix + f" example {number}", identifier=f"W{number}", doi=f"10.1234/demo{number}")
                 for number in range(candidates)]
        return httpx.Response(200, json={"meta": {"count": candidates}, "results": works})

    result, _ = resolve(handler, [ResolveItem(prefix + "…")])
    assert result.counts["openalex_title" if candidates == 1 else "title_only"] == 1


def test_arxiv_title_failure_can_still_resolve_using_openalex():
    title = "Synthetic fallback journal title"

    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(503, text="private remote text")
        return httpx.Response(200, json={"meta": {"count": 1}, "results": [work(title=title)]})

    result, calls = resolve(handler, [ResolveItem(title)])
    assert result.papers[0].paper_id == "doi:10.1234/example"
    assert result.counts["openalex_title"] == 1 and result.errors[0].code == "resolution_failure"
    assert len([request for _, request in calls if request.url.host == "export.arxiv.org"]) == 3


def test_duplicate_metadata_entry_is_not_guessed_and_other_entries_survive():
    items = [ResolveItem("Ambiguous metadata", ("https://arxiv.org/abs/2610.10001",)),
             ResolveItem("Valid metadata", ("https://arxiv.org/abs/2610.10002",))]
    entries = [{"id": "2610.10001v1", "title": "First returned title"},
               {"id": "2610.10001v1", "title": "Conflicting returned title"},
               {"id": "2610.10002v1", "title": "Valid metadata"}]
    result, calls = resolve(lambda request: httpx.Response(200, content=atom(entries)), items)
    assert len(calls) == 3
    assert result.papers[0].paper_id.startswith("title:")
    assert result.papers[1].paper_id == "arxiv:2610.10002"


def test_unserializable_openalex_metadata_falls_back_without_losing_other_items():
    def handler(request):
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, content=json.dumps(work(title="Bad \ud800 remote title")).encode())
        return httpx.Response(200, content=atom([{"id": "2610.10001v1"}]))

    result, _ = resolve(handler, [ResolveItem("A valid supplied title", ("https://doi.org/10.1234/example",)),
                                  ResolveItem("Another valid item", ("https://arxiv.org/abs/2610.10001",))])
    assert result.papers[0].paper_id.startswith("title:")
    assert result.papers[1].paper_id == "arxiv:2610.10001"
    assert result.errors[0].code == "resolution_failure"
