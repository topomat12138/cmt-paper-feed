import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from feed.backfill import BackfillError, backfill, legacy_paper, raw_files
from feed.cli import main
from feed.store import PaperStore

FIXTURES = Path(__file__).parent / "fixtures/backfill"
START = date(2026, 4, 11)
END = date(2026, 10, 27)


@pytest.fixture
def legacy(tmp_path):
    source = tmp_path / "legacy"
    shutil.copytree(FIXTURES, source)
    return source


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def first_record(source):
    return json.loads((source / "data/2026-04-11.jsonl").read_text().splitlines()[0])


def test_legacy_mapping_uses_file_midnight_and_only_allowed_fields(legacy):
    raw = first_record(legacy)
    raw.update(submitted="2020-01-01", first_seen="2099-01-01T00:00:00Z",
               last_updated="2099-01-01T00:00:00Z", sources=["other"],
               listing_type="replacement", irrelevant_extra="discard me")
    paper = legacy_paper(raw, file_date=START)
    assert paper.paper_id == "arxiv:2604.00001"
    assert paper.arxiv_version is None  # absent in legacy data; not invented
    assert paper.abstract == raw["summary"]
    assert paper.title == raw["title"] and paper.authors == raw["authors"]
    assert paper.primary_category == raw["categories"][0]
    assert paper.categories == sorted(raw["categories"])
    assert paper.links == {"abs": raw["abs"], "pdf": raw["pdf"]}
    assert paper.submitted is None and paper.listing_type is None
    assert paper.announced == START
    assert paper.sources == ["arxiv_listing"]
    assert paper.first_seen == paper.last_updated == datetime(2026, 4, 11, tzinfo=timezone.utc)
    assert "irrelevant_extra" not in paper.model_dump()


@pytest.mark.parametrize("identifier", ["2604.00001v2", "https://arxiv.org/abs/2604.00001v2"])
def test_versioned_legacy_id_uses_existing_canonicalization(legacy, identifier):
    raw = first_record(legacy)
    raw["id"] = identifier
    paper = legacy_paper(raw, file_date=START)
    assert paper.arxiv_id == "2604.00001"
    assert paper.arxiv_version == 2


def test_backfill_shards_by_historical_first_seen_and_writes_no_daily(legacy, tmp_path):
    output = tmp_path / "output"
    result = backfill(source_dir=legacy, data_dir=output)
    assert result.model_dump(mode="json") == {
        "files": 3, "input_rows": 6, "unique_arxiv_ids": 4,
        "first_file_date": "2026-04-11", "last_file_date": "2026-10-27",
        "new": 4, "updated": 0, "stored": 4, "errors": [],
    }
    assert set(snapshot(output)) == {
        "papers/2026/2026-04.jsonl", "papers/2026/2026-05.jsonl", "papers/2026/2026-10.jsonl",
    }
    records = PaperStore(output).load()
    assert len(records) == 4
    repeated = records["arxiv:2604.00001"]
    assert repeated.first_seen == datetime(2026, 4, 11, tzinfo=timezone.utc)
    assert repeated.last_updated == datetime(2026, 10, 27, tzinfo=timezone.utc)
    assert repeated.announced == END
    assert repeated.abstract == "More complete historical abstract."
    for paper in records.values():
        assert paper.first_seen.time().isoformat() == "00:00:00"
        assert paper.last_updated.time().isoformat() == "00:00:00"
        assert paper.submitted is None and paper.listing_type is None
        assert paper.sources == ["arxiv_listing"]
    april = [json.loads(line)["paper_id"] for line in (output / "papers/2026/2026-04.jsonl").read_text().splitlines()]
    assert april == ["arxiv:2604.00001", "arxiv:2604.00002"]
    assert not (output / "daily").exists()


def test_backfill_replay_preserves_bytes_mtimes_and_record_count(legacy, tmp_path):
    output = tmp_path / "output"
    backfill(source_dir=legacy, data_dir=output)
    before = snapshot(output)
    mtimes = {path: path.stat().st_mtime_ns for path in output.rglob("*") if path.is_file()}
    result = backfill(source_dir=legacy, data_dir=output)
    assert result.new == result.updated == 0
    assert result.stored == result.unique_arxiv_ids == 4
    assert snapshot(output) == before
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes


def test_only_exact_raw_filenames_in_range_are_read(legacy, tmp_path, monkeypatch):
    ignored = [
        "data/2026-04-11_AI_enhanced_example.jsonl", "data/2026-04-11.md",
        "data/2026-04-10.jsonl", "data/2026-10-28.jsonl",
        "data/nested/2026-04-11.jsonl", "config/2026-04-11.jsonl", "feedback/2026-04-11.jsonl",
    ]
    for name in ignored:
        path = legacy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not JSON and must never be opened")
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert path not in {legacy / name for name in ignored}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    result = backfill(source_dir=legacy, data_dir=tmp_path / "output")
    assert result.files == 3 and result.input_rows == 6 and result.stored == 4


def test_date_range_selects_only_requested_files(legacy, tmp_path):
    result = backfill(source_dir=legacy, data_dir=tmp_path / "output", start=date(2026, 5, 1), end=date(2026, 5, 1))
    assert result.files == 1
    assert result.input_rows == result.stored == 2
    assert result.first_file_date == result.last_file_date == date(2026, 5, 1)
    assert set(snapshot(tmp_path / "output")) == {"papers/2026/2026-05.jsonl"}


def test_backfill_preserves_richer_live_metadata_and_every_daily_byte(legacy, tmp_path, paper):
    output = tmp_path / "output"
    store = PaperStore(output)
    existing = paper(arxiv_id="2604.00001", arxiv_version=3, title="Current richer title",
                     abstract="Current richer abstract", sources=["scholar_alert"], submitted="2026-04-01")
    store.upsert([existing], day=date(2026, 10, 28))
    # This file deliberately cannot be parsed; historical writes must not even
    # read it, and must never touch the live daily manifest either.
    (output / "daily/2026-10-27.json").write_bytes(b"leave these daily bytes alone")
    before_daily = snapshot(output / "daily")
    result = backfill(source_dir=legacy, data_dir=output)
    merged = store.load()[existing.paper_id]
    assert result.new == 3 and result.updated == 1
    assert merged.first_seen == datetime(2026, 4, 11, tzinfo=timezone.utc)
    assert merged.last_updated == existing.last_updated
    assert merged.arxiv_version == 3 and merged.title == existing.title
    assert merged.abstract == existing.abstract and merged.submitted == existing.submitted
    assert merged.sources == ["arxiv_listing", "scholar_alert"]
    assert snapshot(output / "daily") == before_daily


@pytest.mark.parametrize("bad", ['{"title":"missing id"}', "not JSON", "[]", '{"id":"invalid","title":"bad"}'])
def test_invalid_row_fails_before_any_partial_write(legacy, tmp_path, bad):
    (legacy / "data/2026-10-27.jsonl").write_text(bad + "\n")
    output = tmp_path / "output"
    with pytest.raises(BackfillError, match="data/2026-10-27.jsonl:1"):
        backfill(source_dir=legacy, data_dir=output)
    assert snapshot(output) == {}


@pytest.mark.parametrize("mode", ["reversed", "missing", "empty", "symlink"])
def test_bad_source_selection_fails_without_writes(legacy, tmp_path, mode):
    output = tmp_path / "output"
    start, end = START, END
    if mode == "reversed":
        start, end = END, START
    elif mode == "missing":
        legacy = tmp_path / "missing"
    elif mode == "empty":
        start = end = date(2025, 1, 1)
    else:
        path = legacy / "data/2026-04-11.jsonl"
        path.unlink()
        path.symlink_to(legacy / "data/2026-05-01.jsonl")
    with pytest.raises(BackfillError):
        backfill(source_dir=legacy, data_dir=output, start=start, end=end)
    assert snapshot(output) == {}


def test_backfill_cli_is_offline_and_does_not_load_ingest_configuration(legacy, tmp_path, monkeypatch, capsys):
    import feed.cli as cli

    def reject(*args, **kwargs):
        raise AssertionError("backfill must not construct an HTTP client, load configuration, or read the clock")

    class NoClock:
        now = reject

    monkeypatch.setattr(cli.httpx, "Client", reject)
    monkeypatch.setattr(cli, "load_config", reject)
    monkeypatch.setattr(cli, "datetime", NoClock)
    output = tmp_path / "output"
    args = ["backfill", "--source-dir", str(legacy), "--data-dir", str(output)]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["input_rows"] == 6 and report["stored"] == report["unique_arxiv_ids"] == 4
    assert not (output / "daily").exists()
    before = snapshot(output)
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["new"] == report["updated"] == 0
    assert snapshot(output) == before


def test_backfill_cli_failure_does_not_log_row_contents(legacy, tmp_path, capsys):
    (legacy / "data/2026-10-27.jsonl").write_text('{"marker":"do not print this row"}\n')
    output = tmp_path / "output"
    assert main(["backfill", "--source-dir", str(legacy), "--data-dir", str(output)]) == 1
    captured = capsys.readouterr()
    assert "backfill failed" in captured.err
    assert "do not print this row" not in captured.err
    assert captured.out == "" and snapshot(output) == {}
