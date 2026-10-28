import json
from datetime import date

import pytest

from feed.schema import Paper
from feed.store import PaperStore, merge_papers

DAY = date(2026, 10, 28)
NEXT = date(2026, 10, 29)
LATER = "2026-10-29T01:30:00Z"


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_batch_replay_is_byte_identical_including_daily(tmp_path, paper):
    store = PaperStore(tmp_path)
    batch = [paper(), paper(arxiv_id="2610.10000"), paper(arxiv_id="2609.10001", first_seen="2026-09-30T01:30:00Z")]
    store.upsert(batch, day=DAY)
    before = snapshot(tmp_path)
    mtimes = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    manifest = PaperStore(tmp_path).upsert(reversed(batch), day=DAY)
    assert snapshot(tmp_path) == before
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes
    assert manifest.new == sorted(p.paper_id for p in batch)
    assert manifest.updated == []
    assert len(store.load()) == 3


@pytest.mark.parametrize("field,empty", [
    ("arxiv_id", None), ("doi", None), ("journal_ref", None),
    ("authors", []), ("abstract", None), ("abstract", "  "),
    ("primary_category", None), ("categories", []), ("comment", ""),
    ("submitted", None), ("announced", None), ("listing_type", None),
    ("sources", []), ("links", {}),
])
def test_empty_fields_cannot_erase_populated_fields(paper, field, empty):
    existing = paper(doi="10.1234/test", journal_ref="Example Journal")
    changes = {field: empty, "last_updated": LATER}
    if field == "arxiv_id":
        changes["arxiv_version"] = None
    incoming = Paper.model_validate({**existing.model_dump(exclude={"paper_id"}), **changes})
    merged = merge_papers(existing, incoming)
    assert getattr(merged, field) == getattr(existing, field)
    assert merged == existing


def test_sources_categories_and_link_keys_are_unioned(paper):
    existing = paper(links={"abs": "https://arxiv.org/abs/2610.33463", "pdf": "https://arxiv.org/pdf/2610.33463"})
    incoming = paper(sources=["scholar_alert"], categories=["cond-mat.mtrl-sci"], links={"doi": "https://doi.org/10.1234/test", "abs": ""}, last_updated=LATER)
    merged = merge_papers(existing, incoming)
    assert merged.sources == ["arxiv_listing", "scholar_alert"]
    assert merged.categories == ["cond-mat.mtrl-sci", "cond-mat.supr-con"]
    assert merged.links == {**existing.links, **incoming.links}


def test_version_max_first_seen_and_newer_metadata(paper):
    existing = paper(arxiv_version=2)
    incoming = paper(arxiv_version=4, title="Revised synthetic title", first_seen=LATER, last_updated=LATER, listing_type="replacement")
    merged = merge_papers(existing, incoming)
    assert merged.arxiv_version == 4
    assert merged.first_seen == existing.first_seen
    assert merged.last_updated == incoming.last_updated
    assert merged.title == incoming.title
    assert merged.listing_type == "replacement"
    stale = paper(arxiv_version=1, title="Old title", last_updated="2026-10-30T01:30:00Z")
    assert merge_papers(merged, stale) == merged


def test_stale_metadata_only_fills_gaps(paper):
    existing = paper(arxiv_version=3, abstract=None, last_updated=LATER)
    incoming = paper(arxiv_version=1, title="Stale title", abstract="Missing abstract", journal_ref="Example Journal")
    merged = merge_papers(existing, incoming)
    assert merged.title == existing.title
    assert merged.abstract == "Missing abstract"
    assert merged.journal_ref == "Example Journal"
    assert merged.arxiv_version == 3


def test_observation_timestamps_alone_do_not_change_bytes(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([paper()], day=DAY)
    before = snapshot(tmp_path)
    store.upsert([paper(first_seen=LATER, last_updated=LATER)], day=DAY)
    assert snapshot(tmp_path) == before


def test_duplicates_in_batch_merge_once_and_replay(tmp_path, paper):
    store = PaperStore(tmp_path)
    batch = [paper(arxiv_version=3, title="Revised title", last_updated=LATER), paper(), paper(sources=["scholar_alert"])]
    manifest = store.upsert(batch, day=DAY)
    assert manifest.new == ["arxiv:2610.33463"]
    assert len(store.load()) == 1
    assert store.load()["arxiv:2610.33463"].arxiv_version == 3
    before = snapshot(tmp_path)
    store.upsert(batch, day=DAY)
    assert snapshot(tmp_path) == before


def test_daily_accumulates_updates_but_new_and_updated_are_disjoint(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper()
    store.upsert([first], day=DAY)
    changed = paper(abstract="Revised abstract", last_updated=LATER)
    same_day = store.upsert([changed], day=DAY)
    assert same_day.new == [first.paper_id]
    assert same_day.updated == []
    next_day = store.upsert([paper(comment="More pages", last_updated=LATER), paper(arxiv_id="2610.10000")], day=NEXT)
    assert next_day.new == ["arxiv:2610.10000"]
    assert next_day.updated == [first.paper_id]
    before = snapshot(tmp_path)
    assert store.upsert([], day=NEXT) == next_day
    assert snapshot(tmp_path) == before


def test_update_stays_in_first_seen_month_and_daily_lists_current_id(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([paper(first_seen="2026-09-30T23:59:00Z")], day=DAY)
    manifest = store.upsert([paper(arxiv_version=2, first_seen="2026-11-01T00:00:00Z", last_updated="2026-11-01T00:00:00Z")], day=date(2026, 11, 1))
    assert (tmp_path / "papers/2026/2026-09.jsonl").is_file()
    assert not (tmp_path / "papers/2026/2026-11.jsonl").exists()
    assert manifest.new == []
    assert manifest.updated == ["arxiv:2610.33463"]


def test_year_boundary_and_utc_shards(tmp_path, paper):
    store = PaperStore(tmp_path)
    record = paper(first_seen="2026-12-31T23:30:00-02:00", last_updated=None)
    store.upsert([record], day=date(2027, 1, 1))
    assert (tmp_path / "papers/2027/2027-01.jsonl").is_file()
    assert store.load()[record.paper_id] == record


def test_jsonl_sorting_utf8_and_newlines(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([paper(title="合成标题"), paper(arxiv_id="2610.10000")], day=DAY)
    content = (tmp_path / "papers/2026/2026-10.jsonl").read_bytes()
    rows = [json.loads(line) for line in content.splitlines()]
    assert [row["paper_id"] for row in rows] == sorted(row["paper_id"] for row in rows)
    assert "合成标题".encode() in content
    assert content.endswith(b"\n")
    daily = json.loads((tmp_path / "daily/2026-10-28.json").read_bytes())
    assert daily == {"date": "2026-10-28", "new": ["arxiv:2610.10000", "arxiv:2610.33463"], "updated": []}


def test_empty_batch_creates_empty_daily_and_replays(tmp_path):
    store = PaperStore(tmp_path)
    result = store.upsert([], day=DAY)
    assert result.model_dump(mode="json") == {"date": "2026-10-28", "new": [], "updated": []}
    before = snapshot(tmp_path)
    store.upsert([], day=DAY)
    assert snapshot(tmp_path) == before


def test_title_to_doi_to_arxiv_upgrade_reuses_one_record(tmp_path, paper):
    store = PaperStore(tmp_path)
    title_only = paper(arxiv_id=None, arxiv_version=None, title=r"A \textbf{synthetic} paper!")
    with_doi = paper(arxiv_id=None, arxiv_version=None, doi="10.1234/PAPER", first_seen=LATER, last_updated=LATER)
    with_arxiv = paper(doi="10.1234/paper", first_seen=LATER, last_updated=LATER)
    store.upsert([title_only], day=DAY)
    daily = store.upsert([with_doi, with_arxiv], day=NEXT)
    assert list(store.load()) == [with_arxiv.paper_id]
    assert store.load()[with_arxiv.paper_id].first_seen == title_only.first_seen
    assert daily.new == []
    assert daily.updated == [with_arxiv.paper_id]
    before = snapshot(tmp_path)
    store.upsert([title_only, with_doi, with_arxiv], day=NEXT)
    assert snapshot(tmp_path) == before


def test_same_day_promotion_rewrites_manifest_without_dangling_ids(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper(arxiv_id=None, arxiv_version=None)
    store.upsert([first], day=DAY)
    result = store.upsert([paper()], day=DAY)
    assert result.new == ["arxiv:2610.33463"]
    assert result.updated == []


def test_promotion_collapses_existing_target_and_removes_empty_shard(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper(arxiv_id=None, arxiv_version=None, first_seen="2026-09-01T00:00:00Z")
    store.upsert([first], day=DAY)
    # A changed title does not match until the resolver supplies the old title.
    store.upsert([paper(title="A revised title")], day=DAY)
    result = store.upsert([paper()], day=NEXT)
    assert len(store.load()) == 1
    assert result.updated == ["arxiv:2610.33463"]
    assert store.load()["arxiv:2610.33463"].first_seen == first.first_seen
    assert not (tmp_path / "papers/2026/2026-10.jsonl").exists()


def test_distinct_identified_papers_with_same_title_are_not_collapsed(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([paper(), paper(arxiv_id="2610.10000")], day=DAY)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="ambiguous"):
        store.upsert([paper(arxiv_id=None, arxiv_version=None)], day=NEXT)
    assert snapshot(tmp_path) == before
    assert len(store.load()) == 2


def test_conflicting_doi_identity_is_rejected_before_writes(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([paper(doi="10.1234/test")], day=DAY)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="conflicting arxiv_id"):
        store.upsert([paper(arxiv_id="2610.10000", doi="10.1234/test")], day=NEXT)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("corruption", ["invalid json", "wrong shard", "duplicate"])
def test_corrupt_store_is_rejected_without_overwriting(tmp_path, paper, corruption):
    store = PaperStore(tmp_path)
    store.upsert([paper()], day=DAY)
    path = tmp_path / "papers/2026/2026-10.jsonl"
    if corruption == "invalid json":
        path.write_text("{invalid}\n")
    elif corruption == "wrong shard":
        path.rename(path.with_name("2026-09.jsonl"))
    else:
        path.write_bytes(path.read_bytes() * 2)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="invalid paper at"):
        store.upsert([paper()], day=NEXT)
    assert snapshot(tmp_path) == before


def test_manifest_date_mismatch_fails_before_writes(tmp_path, paper):
    store = PaperStore(tmp_path)
    store.upsert([], day=DAY)
    manifest = tmp_path / "daily/2026-10-28.json"
    manifest.write_text('{"date":"2026-10-27","new":[],"updated":[]}')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="date does not match"):
        store.upsert([paper()], day=DAY)
    assert snapshot(tmp_path) == before
