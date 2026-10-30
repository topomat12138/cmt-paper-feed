import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import feed.cli as cli
from feed.errors import ScholarIssue, SourceFailure
from feed.ingest import ingest_sources
from feed.schema import Paper
from feed.sources import ScholarAlert, Source, SourceResult
from feed.sources.arxiv_listing import NS
from feed.store import DailyManifest, PaperStore
from feed.workflow import checkout_data, git_environment, publish_data

SEEN = datetime(2026, 10, 31, 1, 30, tzinfo=timezone.utc)
ARXIV = "2610.99901"
TITLE = "Synthetic resolved arXiv document"
DOI = "10.1103/abcd-1234"
ITEMS = [{"title": TITLE, "url": f"https://arxiv.org/pdf/{ARXIV}v2.pdf?private-query=canary"},
         {"title": "Synthetic resolved journal document", "url": f"https://link.aps.org/pdf/{DOI}?private=canary"}]


def atom(identifier=ARXIV, title=TITLE, doi=None):
    feed = ET.Element(f"{{{NS['atom']}}}feed")
    entry = ET.SubElement(feed, f"{{{NS['atom']}}}entry")
    for key, value in {"id": f"http://arxiv.org/abs/{identifier}v2", "title": title,
                       "summary": "A synthetic public abstract.", "published": "2026-10-29T00:00:00Z"}.items():
        ET.SubElement(entry, f"{{{NS['atom']}}}{key}").text = value
    author = ET.SubElement(entry, f"{{{NS['atom']}}}author")
    ET.SubElement(author, f"{{{NS['atom']}}}name").text = "Synthetic Author"
    ET.SubElement(entry, f"{{{NS['atom']}}}category", {"term": "cond-mat.supr-con"})
    if doi:
        ET.SubElement(entry, f"{{{NS['arxiv']}}}doi").text = doi
    return ET.tostring(feed)


def respond(request):
    if request.url.host == "export.arxiv.org":
        return httpx.Response(200, content=atom())
    assert request.url.host == "api.openalex.org"
    assert request.url.path == f"/works/https://doi.org/{DOI}"
    return httpx.Response(200, json={"doi": "https://doi.org/" + DOI, "title": ITEMS[1]["title"],
                                   "authorships": [{"author": {"display_name": "Synthetic Journal Author"}}],
                                   "abstract_inverted_index": {"Journal": [0], "abstract": [1]},
                                   "primary_location": {"source": {"display_name": "Synthetic Journal"}}})


@pytest.fixture
def runtime(monkeypatch):
    class Clock:
        value = 0.0

        def clock(self):
            return self.value

        def sleep(self, seconds):
            self.value += seconds

    class Now(datetime):
        value = SEEN

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value

    clock, calls = Clock(), []
    handler = {"reply": respond}
    original = httpx.Client

    def transport(request):
        calls.append(request)
        assert request.url.host in {"export.arxiv.org", "api.openalex.org"}
        assert "authorization" not in request.headers
        assert "@" not in request.headers["user-agent"]
        return handler["reply"](request)

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr(cli, "datetime", Now)
    monkeypatch.setattr(cli, "ScholarAlert", lambda **kwargs: ScholarAlert(
        **kwargs, sleep=clock.sleep, clock=clock.clock))
    return Now, handler, calls


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def assert_only_counts(value):
    if isinstance(value, dict):
        for count in value.values():
            assert_only_counts(count)
    else:
        assert type(value) is int and value >= 0


@pytest.mark.parametrize("mode", ["repository_dispatch", "workflow_dispatch", "items-json", "payload"])
def test_all_input_modes_write_weekend_records_and_report_only_counts(tmp_path, runtime, capsys, mode):
    envelope = {"version": 1, "items": ITEMS}
    root = tmp_path / "data"
    if mode == "items-json":
        args = ["--items-json", json.dumps(ITEMS)]
    else:
        value = ({"client_payload": envelope, "sender": {"email": "private@example.org"}}
                 if mode == "repository_dispatch" else
                 {"inputs": {"items_json": json.dumps(ITEMS)}} if mode == "workflow_dispatch" else envelope)
        path = tmp_path / "event.json"
        path.write_text(json.dumps(value))
        args = ["--payload", str(path)]
    assert cli.main(["scholar", "--data-dir", str(root), *args]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["new"] == 2 and report["updated"] == 0
    assert report["resolution_counts"] == {"arxiv_url": 1, "doi_url": 1, "arxiv_title": 0,
                                            "openalex_title": 0, "title_only": 0, "doi_title_link": 0}
    assert report["error_counts"] == {} and report["received"] == report["valid"] == 2
    assert_only_counts(report)
    papers = PaperStore(root).load()
    assert set(papers) == {f"arxiv:{ARXIV}", "doi:" + DOI}
    assert all(p.sources == ["scholar_alert"] and p.announced is None and p.listing_type is None for p in papers.values())
    assert all(p.first_seen == p.last_updated == SEEN for p in papers.values())
    daily = DailyManifest.model_validate_json((root / "daily/2026-10-31.json").read_bytes())
    assert set(daily.new) == set(papers) and daily.errors == []
    for private in ("private-query", "canary", "private@example.org"):
        assert private not in captured.out + captured.err + str(snapshot(root))


def test_same_payload_replays_do_not_change_data_even_on_next_utc_day(tmp_path, runtime, capsys):
    now, _, _ = runtime
    root = tmp_path / "data"
    args = ["scholar", "--data-dir", str(root), "--items-json", json.dumps(ITEMS)]
    assert cli.main(args) == 0
    capsys.readouterr()
    before = snapshot(root)
    for delta in (timedelta(minutes=5), timedelta(days=1)):
        now.value = SEEN + delta
        assert cli.main(args) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["new"] == report["updated"] == 0
        assert snapshot(root) == before
    assert not (root / "daily/2026-11-01.json").exists()


@pytest.mark.parametrize("earlier_day", [False, True])
def test_arxiv_listing_merge_adds_source_and_preserves_daily_changes(tmp_path, runtime, capsys, earlier_day):
    root = tmp_path / "data"
    store = PaperStore(root)
    seed_day = SEEN.date() - timedelta(days=1) if earlier_day else SEEN.date()
    existing = Paper(arxiv_id=ARXIV, arxiv_version=2, title=TITLE, abstract="A synthetic public abstract.",
                     authors=["Synthetic Author"], categories=["cond-mat.supr-con"],
                     primary_category="cond-mat.supr-con", submitted=date(2026, 10, 29),
                     first_seen=SEEN - timedelta(hours=3), sources=["arxiv_listing"],
                     announced=date(2026, 10, 30), listing_type="new", links={"abs": f"https://arxiv.org/abs/{ARXIV}"})
    store.upsert([existing], day=seed_day)
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", json.dumps(ITEMS)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["new"] == report["updated"] == 1
    merged = store.load()[existing.paper_id]
    assert merged.sources == ["arxiv_listing", "scholar_alert"]
    assert merged.announced == existing.announced and merged.listing_type == "new"
    assert merged.first_seen == existing.first_seen
    daily = DailyManifest.model_validate_json((root / "daily/2026-10-31.json").read_bytes())
    assert "doi:" + DOI in daily.new
    assert existing.paper_id in (daily.updated if earlier_day else daily.new)


def test_invalid_items_and_remote_failures_do_not_lose_successful_records(tmp_path, runtime, capsys):
    _, handler, _ = runtime
    handler["reply"] = lambda request: (httpx.Response(200, content=atom()) if request.url.host == "export.arxiv.org"
                                         else httpx.Response(503, text="private-token https://site/?private=query"))
    root = tmp_path / "data"
    payload = {"version": 1, "items": [*ITEMS, {"title": "", "url": "https://private@example.org/"}]}
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", json.dumps(payload)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["new"] == 2 and report["valid"] == 2
    assert report["error_counts"] == {"invalid_item": 1, "resolution_failure": 1}
    assert report["resolution_counts"]["arxiv_url"] == report["resolution_counts"]["doi_url"] == 1
    assert_only_counts(report)
    assert len(PaperStore(root).load()) == 2
    daily = DailyManifest.model_validate_json((root / "daily/2026-10-31.json").read_bytes())
    assert {error.code for error in daily.errors} == {"invalid_item", "resolution_failure"}
    for private in ("private-token", "private@example.org", "private=query"):
        assert private not in captured.out + captured.err + str(snapshot(root))


def test_sources_preserve_each_others_errors_and_clear_only_their_own(tmp_path, runtime, capsys):
    root = tmp_path / "data"
    store = PaperStore(root)
    failure = SourceFailure(source="arxiv_listing", stage="listing", message="listing unavailable")
    store.upsert([], day=SEEN.date(), source_errors=[failure], error_sources={"arxiv_listing"})
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", '[{"title":""}]']) == 1
    capsys.readouterr()
    daily_path = root / "daily/2026-10-31.json"
    assert {error.code for error in DailyManifest.model_validate_json(daily_path.read_bytes()).errors} == {
        "source_failure", "invalid_item"}

    class Good(Source):
        def fetch(self, **kwargs):
            return SourceResult((SEEN.date(),), [])

    report = ingest_sources({"arxiv_listing": Good()}, data_dir=root, seen_at=SEEN)
    assert report[0]["errors"] == []
    assert [error.code for error in DailyManifest.model_validate_json(daily_path.read_bytes()).errors] == ["invalid_item"]
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", "[]"]) == 0
    assert json.loads(capsys.readouterr().out)["error_counts"] == {}
    assert DailyManifest.model_validate_json(daily_path.read_bytes()).errors == []


def test_foreign_source_failure_does_not_mark_scholar_command_failed(tmp_path, runtime, capsys):
    root = tmp_path / "data"
    failure = SourceFailure(source="arxiv_listing", stage="listing", message="listing unavailable")
    PaperStore(root).upsert([], day=SEEN.date(), source_errors=[failure], error_sources={"arxiv_listing"})
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", "[]"]) == 0
    assert json.loads(capsys.readouterr().out)["error_counts"] == {}
    assert len(DailyManifest.model_validate_json((root / "daily/2026-10-31.json").read_bytes()).errors) == 1


def test_title_identifier_upgrade_reports_an_update_for_this_invocation(tmp_path, runtime, capsys):
    root = tmp_path / "data"
    old = Paper(title=TITLE, sources=["scholar_alert"], first_seen=SEEN - timedelta(days=1))
    PaperStore(root).upsert([old], day=SEEN.date() - timedelta(days=1))
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", json.dumps(ITEMS[:1])]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["new"] == 0 and report["updated"] == 1
    assert set(PaperStore(root).load()) == {f"arxiv:{ARXIV}"}


@pytest.mark.parametrize("contents", ["not JSON", "{\"version\":2,\"items\":[]}", "[]" * 1_100_000])
def test_invalid_payload_has_counted_diagnostic_without_echo(tmp_path, runtime, capsys, contents):
    path = tmp_path / "event.json"
    path.write_text(contents)
    root = tmp_path / "data"
    assert cli.main(["scholar", "--data-dir", str(root), "--payload", str(path)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["error_counts"] == {"invalid_item": 1}
    assert report["new"] == report["updated"] == 0
    assert_only_counts(report)
    assert "not JSON" not in captured.out + captured.err


def test_storage_failure_never_emits_a_publishable_partial_report(tmp_path, runtime, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("private@example.org private-storage-detail")

    monkeypatch.setattr(PaperStore, "upsert", fail)
    assert cli.main(["scholar", "--data-dir", str(tmp_path), "--items-json", json.dumps(ITEMS)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "scholar failed" in captured.err
    assert "private" not in captured.err


def test_cli_to_real_git_publication_replay_creates_no_commit(tmp_path, runtime, capsys):
    def git(root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                                env=git_environment())
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=main")
    code = tmp_path / "code"
    code.mkdir()
    git(code, "init", "--initial-branch=main")
    (code / "README.md").write_text("Public code stays unchanged.\n")
    git(code, "add", "README.md")
    git(code, "commit", "-m", "Initial code")
    git(code, "remote", "add", "origin", str(remote))
    git(code, "push", "origin", "main")
    main_before = git(remote, "rev-parse", "main")
    root = tmp_path / "data"
    checkout_data(str(remote), root)
    args = ["scholar", "--data-dir", str(root), "--items-json", json.dumps(ITEMS)]
    assert cli.main(args) == 0
    report = tmp_path / "report.json"
    report.write_text(capsys.readouterr().out)
    assert publish_data(root, report_path=report, kind="scholar")
    first = git(remote, "rev-parse", "data")
    assert git(remote, "log", "data", "-1", "--format=%s") == "data: scholar 2/0"
    assert git(remote, "ls-tree", "data", "--name-only") == "daily\npapers"
    assert cli.main(args) == 0
    report.write_text(capsys.readouterr().out)
    assert json.loads(report.read_text())["new"] == 0
    assert not publish_data(root, report_path=report, kind="scholar")
    assert git(remote, "rev-parse", "data") == first
    assert git(remote, "rev-list", "--count", "data") == "1"
    assert git(remote, "rev-parse", "main") == main_before


def test_scholar_keeps_existing_arxiv_conflicts_until_arxiv_recovers(tmp_path, runtime, capsys):
    root = tmp_path / "data"
    store = PaperStore(root)
    existing = Paper(arxiv_id=ARXIV, title=TITLE, doi="10.1234/original",
                     sources=["arxiv_listing"], first_seen=SEEN)
    store.upsert([existing], day=SEEN.date())
    store.upsert([existing.model_copy(update={"doi": "10.1234/conflicting"})], day=SEEN.date(),
                 error_sources={"arxiv_listing"})
    assert cli.main(["scholar", "--data-dir", str(root), "--items-json", "[]"]) == 0
    assert json.loads(capsys.readouterr().out)["error_counts"] == {}
    path = root / "daily/2026-10-31.json"
    assert DailyManifest.model_validate_json(path.read_bytes()).errors[0].code == "identifier_conflict"
    store.upsert([], day=SEEN.date(), error_sources={"arxiv_listing"})
    assert DailyManifest.model_validate_json(path.read_bytes()).errors == []


@pytest.mark.parametrize("same_author", [False, True])
def test_journal_doi_links_only_a_unique_same_title_same_surname_arxiv(tmp_path, runtime, capsys, same_author):
    root = tmp_path / "data"
    store = PaperStore(root)
    existing = Paper(arxiv_id=ARXIV, title=ITEMS[1]["title"].upper(),
                     authors=["Another Author" if same_author else "Different Person"],
                     sources=["arxiv_listing"], first_seen=SEEN - timedelta(days=1),
                     announced=date(2026, 10, 29), listing_type="new")
    store.upsert([existing], day=SEEN.date() - timedelta(days=1))
    args = ["scholar", "--data-dir", str(root), "--items-json", json.dumps(ITEMS[1:])]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert_only_counts(report)
    assert report["resolution_counts"]["doi_title_link"] == int(same_author)
    assert report["resolution_counts"]["doi_url"] == int(not same_author)
    assert report["new"] == int(not same_author) and report["updated"] == int(same_author)
    papers = store.load()
    if same_author:
        assert set(papers) == {existing.paper_id}
        merged = papers[existing.paper_id]
        assert merged.doi == DOI and merged.sources == ["arxiv_listing", "scholar_alert"]
        assert merged.first_seen == existing.first_seen and merged.announced == existing.announced
        assert merged.links["doi"] == "https://doi.org/" + DOI
    else:
        assert set(papers) == {existing.paper_id, "doi:" + DOI}
        assert papers[existing.paper_id].doi is None
    before = snapshot(root)
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["new"] == report["updated"] == 0 and snapshot(root) == before


@pytest.mark.parametrize("case,expected_status", [
    ("known_doi", 0), ("recovered_title_lookup", 0), ("failed_title_only", 1), ("unmatched_title", 0),
])
def test_recoverable_diagnostics_do_not_fail_identified_records(tmp_path, runtime, capsys, case, expected_status):
    _, handler, _ = runtime
    title = ITEMS[1]["title"]

    def respond_to_case(request):
        if request.url.host == "export.arxiv.org":
            return (httpx.Response(200, content=f'<feed xmlns="{NS["atom"]}"/>')
                    if case == "unmatched_title" else httpx.Response(503))
        if case == "recovered_title_lookup":
            metadata = json.loads(respond(httpx.Request("GET", f"https://api.openalex.org/works/https://doi.org/{DOI}")).content)
            return httpx.Response(200, json={"meta": {"count": 3000}, "results": [metadata]})
        if case == "unmatched_title":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404 if case == "known_doi" else 503)

    handler["reply"] = respond_to_case
    item = ITEMS[1] if case == "known_doi" else {"title": title}
    assert cli.main(["scholar", "--data-dir", str(tmp_path), "--items-json", json.dumps([item])]) == expected_status
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == int(case == "failed_title_only")
    assert report["error_counts"] == ({} if case == "unmatched_title" else
                                      {"resolution_failure": 2 if case == "failed_title_only" else 1})
    paper = next(iter(PaperStore(tmp_path).load().values()))
    assert paper.paper_id.startswith("title:" if case in {"failed_title_only", "unmatched_title"} else "doi:")
    daily = DailyManifest.model_validate_json((tmp_path / "daily/2026-10-31.json").read_bytes())
    assert len(daily.errors) == sum(report["error_counts"].values())


def test_current_scholar_store_conflict_still_fails_without_overwriting_record(tmp_path, runtime, capsys):
    existing = Paper(arxiv_id=ARXIV, title=TITLE, doi="10.1234/existing", sources=["arxiv_listing"], first_seen=SEEN)
    store = PaperStore(tmp_path)
    store.upsert([existing], day=SEEN.date())
    runtime[1]["reply"] = lambda request: httpx.Response(200, content=atom(doi="10.1234/conflicting"))
    assert cli.main(["scholar", "--data-dir", str(tmp_path), "--items-json", json.dumps(ITEMS[:1])]) == 1
    assert json.loads(capsys.readouterr().out)["error_counts"] == {"identifier_conflict": 1}
    assert store.load() == {existing.paper_id: existing}
