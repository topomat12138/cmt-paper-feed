import json
import os
import sqlite3
from datetime import date

import pytest

import feed.build_index as indexing
from feed.build_index import SearchIndexError, build_index, plain_query, search
from feed.store import PaperStore

DAY = date(2026, 10, 28)


def seed(tmp_path, paper):
    data = tmp_path / "data"
    record = paper(title="Yu-Shiba-Rusinov chain in p+ip Bi2Se3",
                   abstract="Shiba chain modes and the Møller limit in superconductors.",
                   authors=["Anders Møller", "Zoë Example", "René Example", "Fourth Author"])
    PaperStore(data).upsert([record], day=DAY)
    return data, record


def test_index_builds_metadata_and_rebuild_discards_old_tables(tmp_path, paper):
    data, record = seed(tmp_path, paper)
    out = tmp_path / "local.sqlite3"
    report = build_index(data, out)
    assert report == {"records": 1, "index": str(out)}
    with sqlite3.connect(out) as connection:
        row = connection.execute("SELECT paper_id, authors, announced, first_seen, primary_category, abs FROM papers").fetchone()
        assert row == (record.paper_id, "; ".join(record.authors), "2026-10-27", "2026-10-28T01:30:00+00:00",
                       "cond-mat.supr-con", record.links["abs"])
        assert "porter unicode61 remove_diacritics 2" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='papers_fts'").fetchone()[0]
        connection.execute("CREATE TABLE stale (value TEXT)")
    build_index(data, out)
    with sqlite3.connect(out) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='stale'").fetchall() == []
        assert connection.execute("SELECT count(*) FROM papers").fetchone()[0] == 1


@pytest.mark.parametrize("query", [
    "Shiba chain", "Yu-Shiba-Rusinov", "p+ip", "Bi2Se3", '"Shiba chain"',
    "(Shiba chain)", '("Yu-Shiba-Rusinov")', "Moller", "Møller", "Zoe", "Rene",
])
def test_plain_queries_handle_punctuation_and_diacritics(tmp_path, paper, query):
    data, record = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite"
    assert not index.exists()
    result = search(query, index=index, data_dir=data)
    assert index.exists()  # A missing local index is built on demand.
    assert result == [{"paper_id": record.paper_id, "title": record.title,
                       "authors": record.authors[:3], "announced": "2026-10-27", "abs": record.links["abs"]}]


def test_literal_fts_keywords_and_empty_punctuation_never_become_operators(tmp_path, paper):
    data, record = seed(tmp_path, paper)
    assert plain_query('"Shiba" OR (chain)') == '"Shiba" "OR" "chain"'
    assert search('"Shiba" OR (chain)', index=tmp_path / "search.sqlite", data_dir=data) == []
    assert search('() + - "', index=tmp_path / "search.sqlite", data_dir=data) == []


def test_raw_fts5_supports_operators_and_reports_bad_syntax(tmp_path, paper):
    data, record = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    assert search('missing OR "Shiba chain"', index=index, data_dir=data, raw=True)[0]["paper_id"] == record.paper_id
    assert search('authors:Moller', index=index, data_dir=data, raw=True)[0]["authors"][0] == "Anders Møller"
    with pytest.raises(SearchIndexError, match="--raw syntax"):
        search('"unclosed', index=index, data_dir=data, raw=True)


def test_bm25_ranks_best_match_first_and_limit_defaults_to_twenty(tmp_path, paper):
    data = tmp_path / "data"
    best = paper(arxiv_id="2610.99999", title="Shiba Shiba Shiba chain", abstract="Shiba chain.")
    others = [paper(arxiv_id=f"2610.{number:05}", title="Shiba chain", abstract="Filler material. " * 100)
              for number in range(25)]
    PaperStore(data).upsert([*others, best], day=DAY)
    index = tmp_path / "search.sqlite"
    default = search("Shiba", index=index, data_dir=data)
    assert len(default) == 20
    assert default[0]["paper_id"] == best.paper_id
    assert len(search("Shiba", index=index, data_dir=data, limit=3)) == 3
    assert len(search("Shiba", index=index, data_dir=data, limit=99)) == 26


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits_fail_before_creating_index(tmp_path, limit):
    with pytest.raises(SearchIndexError, match="positive integer"):
        search("Shiba", index=tmp_path / "search.sqlite", data_dir=tmp_path, limit=limit)
    assert not (tmp_path / "search.sqlite").exists()


@pytest.mark.parametrize("area", ["papers", "daily", "embeddings", ".git"])
def test_output_cannot_overwrite_source_data_or_git_metadata(tmp_path, paper, area):
    data, _ = seed(tmp_path, paper)
    path = data / area / "do-not-overwrite.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("keep me")
    with pytest.raises(SearchIndexError, match="outside stored"):
        build_index(data, path)
    assert path.read_text() == "keep me"


def test_rebuild_empty_data_and_unknown_fields(tmp_path):
    out = tmp_path / "search.sqlite3"
    assert build_index(tmp_path / "empty", out)["records"] == 0
    assert search("anything", index=out, data_dir=tmp_path / "empty") == []


def test_rebuild_uses_current_jsonl_records(tmp_path, paper):
    data, record = seed(tmp_path, paper)
    out = tmp_path / "search.sqlite"
    build_index(data, out)
    path = data / "papers/2026/2026-10.jsonl"
    updated = json.loads(path.read_text())
    updated["title"] = "A replacement title"
    path.write_text(json.dumps(updated) + "\n")
    build_index(data, out)
    assert search("replacement", index=out, data_dir=data)[0]["title"] == "A replacement title"
    assert search("Bi2Se3", index=out, data_dir=data) == []


@pytest.mark.parametrize("query", ["Majorana zero mode", "superconductor"])
def test_singular_queries_match_plural_text(tmp_path, paper, query):
    record = paper(title="Majorana zero modes in superconductors")
    data = tmp_path / "data"
    PaperStore(data).upsert([record], day=DAY)
    assert search(query, index=tmp_path / "index.sqlite3", data_dir=data)[0]["paper_id"] == record.paper_id


@pytest.mark.parametrize("first_seen", ["2026-10-28T00:00:00Z", "2026-11-01T00:00:00Z"])
def test_search_rebuilds_after_paper_added_in_existing_or_new_shard(tmp_path, paper, first_seen):
    data, _ = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    added = paper(arxiv_id="2610.99999", title="Newly added discovery", first_seen=first_seen,
                  last_updated=first_seen)
    PaperStore(data).upsert([added], day=DAY)
    assert search("newly added", index=index, data_dir=data)[0]["paper_id"] == added.paper_id


def test_search_detects_content_change_even_when_size_and_mtime_match(tmp_path, paper):
    data, record = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    path = data / "papers/2026/2026-10.jsonl"
    original = path.stat()
    before = path.read_bytes()
    after = before.replace(b"Bi2Se3", b"Nb2Se3")
    assert after != before and len(after) == len(before)
    path.write_bytes(after)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert search("Nb2Se3", index=index, data_dir=data)[0]["paper_id"] == record.paper_id
    assert search("Bi2Se3", index=index, data_dir=data) == []


def test_search_removes_deleted_papers_automatically(tmp_path, paper):
    data, _ = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    (data / "papers/2026/2026-10.jsonl").unlink()
    assert search("Shiba", index=index, data_dir=data) == []


def test_unchanged_paper_bytes_do_not_rebuild_index(tmp_path, paper, monkeypatch):
    data, _ = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    before = index.read_bytes()
    modified = index.stat().st_mtime_ns
    shard = data / "papers/2026/2026-10.jsonl"
    stat = shard.stat()
    os.utime(shard, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged source contents must not rebuild the index")

    monkeypatch.setattr(indexing, "build_index", forbidden)
    assert search("Shiba", index=index, data_dir=data)
    assert index.read_bytes() == before and index.stat().st_mtime_ns == modified


@pytest.mark.parametrize("legacy", ["missing_meta", "old_version"])
def test_search_refreshes_indexes_without_current_metadata(tmp_path, paper, legacy):
    data, _ = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    with sqlite3.connect(index) as connection:
        if legacy == "missing_meta":
            connection.execute("DROP TABLE meta")
        else:
            connection.execute("UPDATE meta SET value = 'old' WHERE key = 'version'")
        connection.execute("DELETE FROM papers_fts")
    assert search("Shiba", index=index, data_dir=data)


def test_mid_read_source_change_does_not_publish_incorrect_fingerprint(tmp_path, paper, monkeypatch):
    data, _ = seed(tmp_path, paper)
    index = tmp_path / "search.sqlite3"
    build_index(data, index)
    before = index.read_bytes()
    original = PaperStore.load

    def concurrent_update(self):
        papers = original(self)
        path = data / "papers/2026/2026-10.jsonl"
        path.write_bytes(path.read_bytes().replace(b"Bi2Se3", b"Nb2Se3"))
        return papers

    monkeypatch.setattr(PaperStore, "load", concurrent_update)
    with pytest.raises(SearchIndexError, match="changed while indexing"):
        build_index(data, index)
    assert index.read_bytes() == before


@pytest.mark.parametrize("operation", ["build", "search"])
def test_connection_failures_are_actionable_without_raw_error_text(tmp_path, monkeypatch, operation):
    out = tmp_path / "search.sqlite3"
    out.touch()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private filesystem detail")

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(SearchIndexError) as raised:
        if operation == "build":
            build_index(tmp_path / "data", out)
        else:
            search("Shiba", index=out, data_dir=tmp_path / "data")
    assert "SQLite index" in str(raised.value)
    assert "private filesystem detail" not in str(raised.value)
