import json
from datetime import date

import pytest

from feed.schema import Paper
import feed.store as store_module
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


def test_first_seen_uses_earlier_value_in_either_merge_order(paper):
    recent = paper()
    historical = paper(first_seen="2026-04-11T01:30:00Z", last_updated="2026-04-11T01:30:00Z")
    for existing, incoming in ((recent, historical), (historical, recent)):
        merged = merge_papers(existing, incoming)
        assert merged.first_seen == historical.first_seen
        assert merged.last_updated == existing.last_updated


@pytest.mark.parametrize("earlier", ["2026-04-11T01:30:00Z", "2025-12-31T23:30:00Z"])
def test_historical_sighting_moves_shard_and_replays(tmp_path, paper, earlier):
    store = PaperStore(tmp_path)
    record = paper()
    store.upsert([record], day=DAY)
    original_daily = (tmp_path / "daily/2026-10-28.json").read_bytes()
    historical = paper(first_seen=earlier, last_updated=earlier)
    result = store.upsert([historical], day=NEXT)
    assert result.new == []
    assert result.updated == [record.paper_id]
    assert result.errors == []
    assert store.load()[record.paper_id].first_seen == historical.first_seen
    assert not (tmp_path / "papers/2026/2026-10.jsonl").exists()
    moved = tmp_path / "papers" / earlier[:4] / f"{earlier[:7]}.jsonl"
    assert moved.is_file()
    assert (tmp_path / "daily/2026-10-28.json").read_bytes() == original_daily
    before = snapshot(tmp_path)
    assert store.upsert([record, historical], day=NEXT) == result
    assert snapshot(tmp_path) == before


def test_historical_move_merges_into_existing_shard(tmp_path, paper):
    store = PaperStore(tmp_path)
    other = paper(arxiv_id="2604.10000", first_seen="2026-04-11T00:00:00Z")
    store.upsert([paper(), other], day=DAY)
    store.upsert([paper(first_seen="2026-04-12T00:00:00Z", last_updated=None)], day=NEXT)
    path = tmp_path / "papers/2026/2026-04.jsonl"
    ids = [json.loads(line)["paper_id"] for line in path.read_bytes().splitlines()]
    assert ids == [other.paper_id, "arxiv:2610.33463"]
    assert len(store.load()) == 2


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
    assert daily == {"date": "2026-10-28", "new": ["arxiv:2610.10000", "arxiv:2610.33463"], "updated": [], "errors": []}


def test_empty_batch_creates_empty_daily_and_replays(tmp_path):
    store = PaperStore(tmp_path)
    result = store.upsert([], day=DAY)
    assert result.model_dump(mode="json") == {"date": "2026-10-28", "new": [], "updated": [], "errors": []}
    assert json.loads((tmp_path / "daily/2026-10-28.json").read_bytes()) == {
        "date": "2026-10-28", "new": [], "updated": [], "errors": [],
    }
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


@pytest.mark.parametrize("identity", ["arxiv", "doi"])
def test_title_only_input_merges_into_unique_identified_paper(tmp_path, paper, identity):
    store = PaperStore(tmp_path)
    existing = paper() if identity == "arxiv" else paper(arxiv_id=None, arxiv_version=None, doi="10.1234/test")
    store.upsert([existing], day=DAY)
    unresolved = paper(
        arxiv_id=None, arxiv_version=None, title=r"Ａ \textbf{synthetic} paper!",
        sources=["scholar_alert"], authors=[], abstract=None,
        first_seen=LATER, last_updated=LATER,
    )
    result = store.upsert([unresolved], day=NEXT)
    assert result.new == []
    assert result.updated == [existing.paper_id]
    assert result.errors == []
    assert list(store.load()) == [existing.paper_id]
    merged = store.load()[existing.paper_id]
    assert merged.sources == ["arxiv_listing", "scholar_alert"]
    assert merged.abstract == existing.abstract
    assert merged.arxiv_id == existing.arxiv_id
    assert merged.doi == existing.doi
    before = snapshot(tmp_path)
    # Rebuild the all-record title index from disk and replay the weak input.
    assert PaperStore(tmp_path).upsert([unresolved], day=NEXT) == result
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("identities", ["arxiv/arxiv", "arxiv/doi", "doi/doi"])
def test_ambiguous_title_skips_input_and_keeps_identified_papers_separate(tmp_path, paper, identities):
    store = PaperStore(tmp_path)
    kinds = identities.split("/")
    first = paper() if kinds[0] == "arxiv" else paper(arxiv_id=None, arxiv_version=None, doi="10.1234/first")
    second = paper(arxiv_id="2610.10000") if kinds[1] == "arxiv" else paper(arxiv_id=None, arxiv_version=None, doi="10.1234/second")
    store.upsert([first, second], day=DAY)
    assert len(store.load()) == 2  # Identified inputs do not match by title.
    unresolved = paper(arxiv_id=None, arxiv_version=None, sources=["scholar_alert"])
    valid = paper(arxiv_id="2610.90000", title="An unrelated valid paper", first_seen=LATER, last_updated=LATER)
    result = store.upsert([unresolved, valid], day=NEXT)
    assert result.new == [valid.paper_id]
    assert result.updated == []
    assert len(result.errors) == 1
    assert result.errors[0].model_dump(mode="json") == {
        "code": "ambiguous_title", "paper_id": unresolved.paper_id,
        "matched_ids": sorted([first.paper_id, second.paper_id]), "field": "title",
        "message": "multiple identified papers share the normalized title",
        "sources": ["scholar_alert"],
    }
    assert store.load() == {p.paper_id: p for p in (first, second, valid)}
    before = snapshot(tmp_path)
    assert store.upsert([unresolved, valid], day=NEXT) == result
    assert snapshot(tmp_path) == before


def test_title_index_tracks_identified_title_changes_within_batch(tmp_path, paper):
    store = PaperStore(tmp_path)
    original = paper(title="Old synthetic title")
    store.upsert([original], day=DAY)
    revised = paper(
        arxiv_version=2, title="Revised synthetic title", first_seen=LATER, last_updated=LATER,
    )
    old_title = paper(
        arxiv_id=None, arxiv_version=None, title=original.title,
        first_seen="2026-10-29T02:00:00Z", last_updated=None,
    )
    new_title = paper(
        arxiv_id=None, arxiv_version=None, title=revised.title, sources=["scholar_alert"],
        first_seen="2026-10-29T03:00:00Z", last_updated=None,
    )
    result = store.upsert([new_title, old_title, revised], day=NEXT)
    assert result.errors == []
    assert result.new == [old_title.paper_id]
    assert result.updated == [original.paper_id]
    assert set(store.load()) == {original.paper_id, old_title.paper_id}
    assert store.load()[original.paper_id].sources == ["arxiv_listing", "scholar_alert"]


@pytest.mark.parametrize("field", ["doi", "arxiv_id"])
def test_identifier_conflict_skips_one_record_and_continues(tmp_path, paper, field):
    store = PaperStore(tmp_path)
    existing = paper(doi="10.1234/existing")
    store.upsert([existing], day=DAY)
    changes = {"doi": "10.1234/conflict"} if field == "doi" else {"arxiv_id": "2610.40000"}
    conflict = paper(doi="10.1234/existing", sources=["scholar_alert"]) if field == "arxiv_id" else existing
    conflict = Paper.model_validate({**conflict.model_dump(exclude={"paper_id"}), **changes})
    valid_before = paper(arxiv_id="2610.10000", title="A valid paper before conflict")
    valid_after = paper(arxiv_id="2610.90000", title="A valid paper after conflict")
    batch = [valid_before, conflict, valid_after]
    result = store.upsert(batch, day=NEXT)
    assert store.load()[existing.paper_id] == existing
    assert set(store.load()) == {existing.paper_id, valid_before.paper_id, valid_after.paper_id}
    assert result.new == sorted([valid_before.paper_id, valid_after.paper_id])
    assert result.updated == []
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.model_dump(mode="json") == {
        "code": "identifier_conflict", "paper_id": conflict.paper_id,
        "matched_ids": [existing.paper_id], "field": field,
        "message": f"conflicting {field} for a merged paper", "sources": conflict.sources,
    }
    # The persisted errors use the existing conflict shape, and replaying the
    # same failed input remains stable.
    daily = json.loads((tmp_path / "daily/2026-10-29.json").read_bytes())
    assert daily["errors"] == [error.model_dump(mode="json")]
    assert daily["new"] == result.new
    before = snapshot(tmp_path)
    assert store.upsert(reversed(batch), day=NEXT) == result
    assert snapshot(tmp_path) == before


def test_conflicting_candidate_group_does_not_mutate_records_or_indexes(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper(doi="10.1234/first")
    second = paper(arxiv_id="2610.50000", doi="10.1234/second", abstract=None)
    store.upsert([first, second], day=DAY)
    conflict = paper(doi=second.doi)  # paper_id matches first, DOI matches second
    valid = paper(arxiv_id=None, arxiv_version=None, doi=second.doi, abstract="Fill the gap")
    result = store.upsert([conflict, valid], day=NEXT)
    assert len(result.errors) == 1
    assert result.errors[0].matched_ids == sorted([first.paper_id, second.paper_id])
    assert store.load()[first.paper_id] == first
    assert store.load()[second.paper_id].abstract == "Fill the gap"
    assert result.new == []
    assert result.updated == [second.paper_id]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_title_and_doi_promotions_and_diagnostics_are_replay_stable(tmp_path, paper, ambiguous):
    store = PaperStore(tmp_path)
    unresolved = paper(arxiv_id=None, arxiv_version=None)
    with_doi = paper(arxiv_id=None, arxiv_version=None, doi="10.1234/test", first_seen="2026-10-28T02:00:00Z", last_updated=None)
    with_arxiv = paper(doi=with_doi.doi, first_seen="2026-10-28T03:00:00Z", last_updated=None)
    same_title_other_id = paper(arxiv_id="2610.50000", first_seen="2026-10-28T04:00:00Z", last_updated=None)
    batch = [with_arxiv, with_doi, unresolved] + ([same_title_other_id] if ambiguous else [])
    result = store.upsert(batch, day=DAY)
    assert len(result.errors) == int(ambiguous)
    expected_ids = {with_arxiv.paper_id} | ({same_title_other_id.paper_id} if ambiguous else set())
    assert set(store.load()) == expected_ids
    assert store.load()[with_arxiv.paper_id].first_seen == (with_doi.first_seen if ambiguous else unresolved.first_seen)
    before = snapshot(tmp_path)
    replay = store.upsert(reversed(batch), day=DAY)
    assert replay == result
    if ambiguous:
        assert replay.errors[0].code == "ambiguous_title"
    assert snapshot(tmp_path) == before


def test_title_normalization_work_is_linear(tmp_path, paper, monkeypatch):
    store = PaperStore(tmp_path)
    known = [paper(arxiv_id=f"2610.{i:05d}", title=f"Known {i}") for i in range(200)]
    unresolved = [paper(arxiv_id=None, arxiv_version=None, title=f"Pending {i}") for i in range(20)]
    store.upsert([*known, *unresolved], day=DAY)
    calls = 0
    match_key = store_module.title_match_key

    def counted(title):
        nonlocal calls
        calls += 1
        return match_key(title)

    monkeypatch.setattr(store_module, "title_match_key", counted)
    new = [paper(arxiv_id=f"2610.{600 + i:05d}", title=f"New {i}") for i in range(100)]
    resolved = [paper(arxiv_id=f"2610.{1000 + i:05d}", title=f"Pending {i}") for i in range(20)]
    result = store.upsert([*new, *resolved], day=NEXT)
    assert result.errors == []
    assert result.new == sorted(p.paper_id for p in new)
    assert result.updated == sorted(p.paper_id for p in resolved)
    assert len(store.load()) == 320
    # Count operations rather than impose a machine-dependent time limit.
    assert len(known) + len(unresolved) <= calls <= len(known) + len(unresolved) + 2 * (len(new) + len(resolved))


@pytest.mark.parametrize("order", ["weak_first", "identified_first", "same_batch"])
def test_whitespace_title_index_promotes_or_merges_one_record_and_replays(tmp_path, paper, order):
    store = PaperStore(tmp_path)
    identified = paper(title="Synthetic magnetism in Ag$3$LiRh$2$O$6$", first_seen=LATER, last_updated=LATER)
    weak = paper(arxiv_id=None, arxiv_version=None,
                 title="Synthetic magnetism in Ag $ *3 $ LiRh $ 2 $ O $ 6$", sources=["scholar_alert"])
    if order == "same_batch":
        result = store.upsert([weak, identified], day=NEXT)
    else:
        first, second = (weak, identified) if order == "weak_first" else (identified, weak)
        store.upsert([first], day=DAY)
        result = store.upsert([second], day=NEXT)
        assert result.new == [] and result.updated == [identified.paper_id]
    assert result.errors == []
    merged = store.load()
    assert set(merged) == {identified.paper_id}
    assert merged[identified.paper_id].sources == ["arxiv_listing", "scholar_alert"]
    assert merged[identified.paper_id].first_seen == weak.first_seen
    before = snapshot(tmp_path)
    # Reload the compact title index from disk before replaying the variants.
    PaperStore(tmp_path).upsert([identified, weak], day=NEXT)
    assert snapshot(tmp_path) == before


def test_whitespace_title_collisions_keep_identified_records_separate_and_reject_weak_input(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper(title="Synthetic Ag$3$LiRh$2$O$6$ paper")
    second = paper(arxiv_id="2610.99999", title="Synthetic Ag $ 3 $ LiRh $ 2 $ O $ 6$ paper")
    store.upsert([first, second], day=DAY)
    assert store.load() == {first.paper_id: first, second.paper_id: second}
    weak = paper(arxiv_id=None, arxiv_version=None, title=first.title, sources=["scholar_alert"])
    result = store.upsert([weak], day=NEXT)
    assert result.new == result.updated == []
    assert result.errors[0].code == "ambiguous_title"
    assert result.errors[0].matched_ids == sorted([first.paper_id, second.paper_id])
    assert store.load() == {first.paper_id: first, second.paper_id: second}


def test_whitespace_only_weak_title_variants_merge_and_replay_without_alias_cycles(tmp_path, paper):
    store = PaperStore(tmp_path)
    first = paper(arxiv_id=None, arxiv_version=None, title="Synthetic Ag$3$LiRh$2$O$6$ paper")
    second = paper(arxiv_id=None, arxiv_version=None, title="Synthetic Ag $ 3 $ LiRh $ 2 $ O $ 6$ paper")
    assert first.paper_id != second.paper_id  # Identity generation is unchanged.
    result = store.upsert([first, second], day=DAY)
    assert len(result.new) == len(store.load()) == 1 and result.errors == []
    before = snapshot(tmp_path)
    assert store.upsert([second, first], day=DAY) == result
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
