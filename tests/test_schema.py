import hashlib

import pytest
from pydantic import ValidationError

from feed.schema import Paper, normalize_title, title_match_key


def test_contract_round_trip(paper):
    record = paper(doi="10.1234/EXAMPLE", journal_ref="Example Journal 1 (2026)")
    assert Paper.model_validate_json(record.model_dump_json()) == record
    assert set(record.model_dump()) == {
        "paper_id", "arxiv_id", "arxiv_version", "doi", "journal_ref", "title",
        "authors", "abstract", "primary_category", "categories", "comment",
        "submitted", "announced", "listing_type", "sources", "links",
        "first_seen", "last_updated",
    }
    assert record.paper_id == "arxiv:2610.33463"
    assert record.doi == "10.1234/example"


@pytest.mark.parametrize("identifier,canonical,version", [
    ("2610.33463v3", "2610.33463", 3),
    ("arXiv:0704.0001v2", "0704.0001", 2),
    ("cond-mat/9501234v5", "cond-mat/9501234", 5),
    ("math.GT/0309136", "math.gt/0309136", 1),
])
def test_arxiv_identity_drops_version(paper, identifier, canonical, version):
    record = paper(arxiv_id=identifier)
    assert record.arxiv_id == canonical
    assert record.paper_id == f"arxiv:{canonical}"
    assert record.arxiv_version == version


def test_arxiv_version_uses_larger_explicit_value(paper):
    assert paper(arxiv_id="2610.33463v2", arxiv_version=4).arxiv_version == 4


@pytest.mark.parametrize("doi", ["10.1234/AbC", " DOI:10.1234/AbC ", "https://doi.org/10.1234/AbC"])
def test_doi_identity_lowercase(paper, doi):
    record = paper(arxiv_id=None, arxiv_version=None, doi=doi)
    assert record.doi == "10.1234/abc"
    assert record.paper_id == "doi:10.1234/abc"


@pytest.mark.parametrize("title,expected", [
    ("  ＢＣＳ   Theory： A—Test!  ", "bcs theory a test"),
    (r"The $\mathrm{BCS}$ \textbf{State}: A-Test", "the bcs state a test"),
    ("A\tB\nC", "a b c"),
    ("Pairing, “Theory” + Transport", "pairing theory transport"),
    ("MnBi₂Te₄", "mnbi2te4"),
    (r"MnBi$_2$Te$_4$", "mnbi2te4"),
    (r"MnBi$_{2}$Te$_{4}$", "mnbi2te4"),
])
def test_title_normalization(title, expected):
    assert normalize_title(title) == expected


def test_title_identity_hashes_normalized_utf8(paper):
    first = paper(arxiv_id=None, arxiv_version=None, title=r" ＢＣＳ $\mathrm{Theory}$! ")
    second = paper(arxiv_id=None, arxiv_version=None, title="bcs theory")
    expected = hashlib.sha1(b"bcs theory").hexdigest()[:12]
    assert first.paper_id == second.paper_id == f"title:{expected}"


@pytest.mark.parametrize("spaced", [
    "Ag $ *3 $ LiRh $ 2 $ O $ 6$",
    "Ag\t$ _3 $\nLiRh\u00a0$ 2 $\u202fO\u2003$ 6$",
])
def test_comparison_key_ignores_formula_whitespace_without_changing_ids(paper, spaced):
    compact = "Ag$3$LiRh$2$O$6$"
    assert normalize_title(spaced) == "ag 3 lirh 2 o 6"
    assert normalize_title(compact) == "ag3lirh2o6"
    assert title_match_key(spaced) == title_match_key(compact) == "ag3lirh2o6"
    first = paper(arxiv_id=None, arxiv_version=None, title=spaced)
    second = paper(arxiv_id=None, arxiv_version=None, title=compact)
    # Existing title IDs still hash the whitespace-preserving normalized text.
    assert first.paper_id == "title:" + hashlib.sha1(b"ag 3 lirh 2 o 6").hexdigest()[:12]
    assert second.paper_id == "title:" + hashlib.sha1(b"ag3lirh2o6").hexdigest()[:12]
    assert first.paper_id != second.paper_id


@pytest.mark.parametrize("kind", ["new", "cross", "replacement", None])
def test_listing_types(paper, kind):
    assert paper(listing_type=kind).listing_type == kind


@pytest.mark.parametrize("overrides", [
    {"paper_id": "doi:10.1234/wrong"},
    {"arxiv_id": "not-an-id"},
    {"arxiv_version": 0},
    {"arxiv_id": None},
    {"doi": "not-a-doi"},
    {"listing_type": "other"},
    {"first_seen": "2026-10-28T01:30:00"},
    {"last_updated": "2026-10-27T01:30:00Z"},
    {"title": "   "},
    {"arxiv_id": None, "arxiv_version": None, "title": "$?!"},
    {"unknown_field": "not in the public contract"},
])
def test_invalid_contract_is_rejected(paper, overrides):
    with pytest.raises(ValidationError):
        paper(**overrides)


def test_sets_blank_values_and_utc(paper):
    record = paper(
        sources=["scholar_alert", "arxiv_listing", "scholar_alert"],
        categories=["b", "a", "b"], authors=["A", "B", "A"],
        abstract=" \n ", links={"abs": "", "pdf": " https://example.org/paper "},
        first_seen="2026-10-27T21:30:00-04:00", last_updated=None,
    )
    assert record.sources == ["arxiv_listing", "scholar_alert"]
    assert record.categories == ["a", "b"]
    assert record.authors == ["A", "B"]
    assert record.abstract is None
    assert record.links == {"pdf": "https://example.org/paper"}
    assert record.model_dump(mode="json")["first_seen"] == "2026-10-28T01:30:00Z"
    assert record.last_updated == record.first_seen


def test_whitespace_identifiers_are_missing(paper):
    record = paper(arxiv_id=" \n", arxiv_version=None, doi=" \t")
    assert record.arxiv_id is None
    assert record.doi is None
    assert record.paper_id.startswith("title:")
