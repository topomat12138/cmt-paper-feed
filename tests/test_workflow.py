import json
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

import feed.workflow as workflow
from feed.backfill import backfill
from feed.errors import SourceFailure
from feed.ingest import ingest_sources
from feed.sources.base import Source, SourceError, SourceResult
from feed.store import DailyManifest, PaperStore

DAY = date(2026, 10, 28)
SEEN = datetime(2026, 10, 28, 1, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], env=workflow.git_environment(),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remote.git"
    root.mkdir()
    git(root, "init", "--bare", "--initial-branch=main")
    code = tmp_path / "code-main"
    code.mkdir()
    git(code, "init", "--initial-branch=main")
    (code / "README.md").write_text("Code branch must remain unchanged.\n")
    git(code, "add", "README.md")
    git(code, "commit", "-m", "Initial code")
    git(code, "remote", "add", "origin", str(root))
    git(code, "push", "origin", "main")
    return root


def report(tmp_path, payload=None):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload or {"date": DAY.isoformat(), "fetched": 1, "new": 1, "updated": 0, "errors": []}))
    return path


def seeded_checkout(remote, tmp_path, paper):
    root = tmp_path / "seed"
    workflow.checkout_data(str(remote), root)
    PaperStore(root).upsert([paper(announced=DAY)], day=DAY)
    assert workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    return root


@pytest.mark.parametrize("kind", ["ingest", "backfill"])
def test_orphan_initialization_and_unchanged_rerun_create_no_commit(remote, tmp_path, paper, capsys, kind):
    main_before = git(remote, "rev-parse", "main")
    root = tmp_path / "data"
    workflow.checkout_data(str(remote), root)
    assert git(root, "symbolic-ref", "--short", "HEAD") == "data"
    assert not (root / "README.md").exists()
    if kind == "ingest":
        PaperStore(root).upsert([paper(announced=DAY)], day=DAY)
        payload = {"date": DAY.isoformat()}
    else:
        result = backfill(source_dir=ROOT / "tests/fixtures/backfill", data_dir=root)
        payload = result.model_dump(mode="json")
        assert not (root / "daily").exists()
    path = report(tmp_path, payload)
    assert workflow.publish_data(root, report_path=path, kind=kind)
    first = git(remote, "rev-parse", "refs/heads/data")
    assert git(remote, "rev-list", "--count", "refs/heads/data") == "1"
    # The first data commit is a root commit, unrelated to main.
    assert len(git(remote, "rev-list", "--parents", "-n", "1", first).split()) == 1
    if kind == "ingest":
        assert git(root, "log", "-1", "--format=%s") == "data: 2026-10-28"
    again = tmp_path / "again"
    workflow.checkout_data(str(remote), again)
    if kind == "ingest":
        PaperStore(again).upsert([paper(announced=DAY)], day=DAY)
    else:
        result = backfill(source_dir=ROOT / "tests/fixtures/backfill", data_dir=again)
        assert result.new == result.updated == 0
    assert not workflow.publish_data(again, report_path=path, kind=kind)
    assert git(remote, "rev-parse", "refs/heads/data") == first
    assert git(remote, "rev-list", "--count", "refs/heads/data") == "1"
    assert git(again, "status", "--porcelain") == ""
    assert git(remote, "rev-parse", "main") == main_before
    assert capsys.readouterr() == ("", "")


def test_real_non_fast_forward_push_pulls_rebases_and_preserves_both_writers(remote, tmp_path, paper, monkeypatch):
    seeded_checkout(remote, tmp_path, paper)
    writer = tmp_path / "writer"
    concurrent = tmp_path / "concurrent"
    workflow.checkout_data(str(remote), writer)
    workflow.checkout_data(str(remote), concurrent)
    PaperStore(writer).upsert([paper(arxiv_id="2610.90000", announced=date(2026, 10, 29))], day=date(2026, 10, 29))
    other_file = concurrent / "daily/2026-10-26.json"
    other_file.write_text('{"date":"2026-10-26","new":[],"updated":[],"errors":[]}\n')
    original = workflow.run_git
    pushes, pulls = [], []
    concurrent_sha = []

    def race(directory, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            if len(pushes) == 1:
                git(concurrent, "add", "daily")
                git(concurrent, "commit", "-m", "data: 2026-10-26")
                concurrent_sha.append(git(concurrent, "rev-parse", "HEAD"))
                git(concurrent, "push", "origin", "HEAD:data")
        if args[0] == "pull":
            pulls.append(args)
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(workflow, "run_git", race)
    assert workflow.publish_data(writer, report_path=report(tmp_path, {"date": "2026-10-29"}), kind="ingest")
    assert len(pushes) == 2
    assert pulls == [("pull", "--rebase", "origin", "data")]
    git(remote, "merge-base", "--is-ancestor", concurrent_sha[0], "refs/heads/data")
    assert (writer / "daily/2026-10-26.json").read_bytes() == other_file.read_bytes()
    assert "arxiv:2610.90000" in PaperStore(writer).load()
    assert git(remote, "rev-list", "--count", "refs/heads/data") == "3"


def test_push_retries_are_bounded_and_git_errors_never_escape(remote, tmp_path, paper, monkeypatch, capsys):
    root = seeded_checkout(remote, tmp_path, paper)
    old = git(remote, "rev-parse", "data")
    PaperStore(root).upsert([paper(arxiv_id="2610.90000", announced=DAY)], day=DAY)
    original = workflow.run_git
    pushes, pulls = [], []
    secret = "do-not-log-credential-or-identity"

    def reject(directory, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(args, 1, "", secret)
        if args[0] == "pull":
            pulls.append(args)
        return original(directory, *args, **kwargs)

    monkeypatch.setattr(workflow, "run_git", reject)
    with pytest.raises(workflow.WorkflowError, match="three attempts") as raised:
        workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    assert len(pushes) == 3 and len(pulls) == 2
    assert secret not in str(raised.value)
    assert git(remote, "rev-parse", "data") == old
    assert capsys.readouterr() == ("", "")


def test_content_rebase_conflict_aborts_without_overwriting_remote(remote, tmp_path, paper, monkeypatch):
    root = seeded_checkout(remote, tmp_path, paper)
    concurrent = tmp_path / "concurrent"
    workflow.checkout_data(str(remote), concurrent)
    PaperStore(root).upsert([paper(title="Writer title", last_updated="2026-10-29T00:00:00Z")], day=DAY)
    PaperStore(concurrent).upsert([paper(title="Concurrent title", last_updated="2026-10-29T00:00:00Z")], day=DAY)
    git(concurrent, "add", "papers")
    git(concurrent, "commit", "-m", "data: concurrent metadata")
    git(concurrent, "push", "origin", "HEAD:data")
    other = git(remote, "rev-parse", "data")
    with pytest.raises(workflow.WorkflowError, match="rebase failed"):
        workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    assert git(remote, "rev-parse", "data") == other
    assert not (root / ".git/rebase-merge").exists()
    assert not (root / ".git/rebase-apply").exists()


def test_legacy_clone_checks_out_only_raw_dates(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    git(legacy, "init", "--initial-branch=data")
    shutil.copytree(ROOT / "tests/fixtures/backfill/data", legacy / "data")
    for name in ("config/settings.yml", "feedback/events.jsonl", "data/2026-04-11_AI_enhanced_demo.jsonl", "data/2026-04-11.md"):
        path = legacy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not import")
    git(legacy, "add", ".")
    git(legacy, "commit", "-m", "Synthetic legacy snapshot")
    monkeypatch.setattr(workflow, "LEGACY_URL", str(legacy))
    clone = tmp_path / "legacy-clone"
    workflow.checkout_legacy(clone)
    assert sorted(p.name for p in (clone / "data").iterdir()) == ["2026-04-11.jsonl", "2026-05-01.jsonl", "2026-10-27.jsonl"]
    assert not (clone / "config").exists() and not (clone / "feedback").exists()


def test_publish_refuses_main_branch_and_ignores_untracked_non_data(remote, tmp_path, paper):
    root = seeded_checkout(remote, tmp_path, paper)
    (root / "unrelated.txt").write_text("not paper data")
    assert not workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    assert git(remote, "ls-tree", "--name-only", "data") == "daily\npapers"
    git(root, "switch", "-c", "main")
    previous = git(root, "rev-parse", "HEAD")
    with pytest.raises(workflow.WorkflowError, match="code branch"):
        workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    assert git(root, "rev-parse", "HEAD") == previous


def test_pre_staged_non_data_is_never_committed(remote, tmp_path, paper):
    root = seeded_checkout(remote, tmp_path, paper)
    previous = git(remote, "rev-parse", "data")
    (root / "unrelated.txt").write_text("not paper metadata")
    git(root, "add", "unrelated.txt")
    with pytest.raises(workflow.WorkflowError, match="non-data"):
        workflow.publish_data(root, report_path=report(tmp_path), kind="ingest")
    assert git(remote, "rev-parse", "data") == previous


def test_checkout_rejects_paths_inside_code_workspace(remote, tmp_path, monkeypatch):
    workspace = tmp_path / "code"
    workspace.mkdir()
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    with pytest.raises(workflow.WorkflowError, match="outside"):
        workflow.checkout_data(str(remote), workspace / "data")
    assert list(workspace.iterdir()) == []


def test_source_failure_still_publishes_successes_and_marks_run_failed(remote, tmp_path, paper):
    class Good(Source):
        def fetch(self, **kwargs):
            return SourceResult((DAY,), [paper(announced=DAY)])

    class Broken(Source):
        def fetch(self, **kwargs):
            raise SourceError("private response body must not reach daily or logs")

    root = tmp_path / "data"
    workflow.checkout_data(str(remote), root)
    reports = ingest_sources({"good": Good(), "broken": Broken()}, data_dir=root, seen_at=SEEN)
    run_failed = any(item["errors"] for item in reports)
    assert run_failed  # The workflow's final status step converts this to red.
    assert len(PaperStore(root).load()) == 1
    assert reports[0]["errors"][0]["code"] == "source_failure"
    assert "private response" not in json.dumps(reports)
    path = report(tmp_path, reports[0])
    assert workflow.publish_data(root, report_path=path, kind="ingest")
    daily = json.loads(git(remote, "show", "data:daily/2026-10-28.json"))
    assert daily["new"] == ["arxiv:2610.33463"]
    assert daily["errors"] == reports[0]["errors"]
    # Failed reruns with the same successes/errors cannot create another commit.
    original = git(remote, "rev-parse", "data")
    ingest_sources({"good": Good(), "broken": Broken()}, data_dir=root, seen_at=SEEN)
    assert not workflow.publish_data(root, report_path=path, kind="ingest")
    assert git(remote, "rev-parse", "data") == original


def test_daily_errors_include_conflicts_are_stable_and_clear_on_recovery(tmp_path, paper):
    store = PaperStore(tmp_path)
    existing = paper(doi="10.1234/original")
    store.upsert([existing], day=DAY)
    failure = SourceFailure(source="arxiv_listing", stage="listing", message="listing unavailable")
    conflict = paper(doi="10.1234/conflicting")
    store.upsert([conflict], day=DAY, source_errors=[failure, failure])
    path = tmp_path / "daily/2026-10-28.json"
    before = path.read_bytes()
    daily = DailyManifest.model_validate_json(before)
    assert {error.code for error in daily.errors} == {"source_failure", "identifier_conflict"}
    store.upsert([conflict], day=DAY, source_errors=[failure])
    assert path.read_bytes() == before
    recovered = store.upsert([existing], day=DAY)
    assert recovered.errors == []
    assert json.loads(path.read_bytes())["errors"] == []
    assert recovered.new == [existing.paper_id]


def test_legacy_daily_without_errors_remains_readable(tmp_path):
    path = tmp_path / "daily/2026-10-28.json"
    path.parent.mkdir()
    path.write_text('{"date":"2026-10-28","new":[],"updated":[]}')
    result = PaperStore(tmp_path).upsert([], day=DAY)
    assert result.errors == []
    assert set(json.loads(path.read_bytes())) == {"date", "new", "updated", "errors"}


def test_workflow_cli_does_not_echo_credential_bearing_errors(tmp_path, capsys):
    assert workflow.main(["checkout", "--directory", str(tmp_path / "data"),
                          "--repository-url", "https://identity:do-not-print@github.com/example/repo.git"]) == 1
    assert capsys.readouterr() == ("", "")


def test_token_stays_out_of_git_arguments_config_and_output(remote, tmp_path, paper, monkeypatch, capsys):
    token = "private-token-canary"
    root = tmp_path / "data"
    original = workflow.subprocess.run

    def inspect(args, **kwargs):
        assert token not in " ".join(args)
        assert kwargs["capture_output"]
        return original(args, **kwargs)

    monkeypatch.setattr(workflow.subprocess, "run", inspect)
    workflow.checkout_data(str(remote), root, token=token)
    PaperStore(root).upsert([paper(announced=DAY)], day=DAY)
    assert workflow.publish_data(root, report_path=report(tmp_path), kind="ingest", token=token)
    config = (root / ".git/config").read_text()
    assert token not in config and "AUTHORIZATION" not in config
    assert capsys.readouterr() == ("", "")


def test_workflows_share_lock_have_minimal_permissions_and_publish_before_failing():
    workflows = {name: yaml.load((ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader)
                 for name in ("backfill", "ingest")}
    assert set(workflows["backfill"]["on"]) == {"workflow_dispatch"}
    assert workflows["ingest"]["on"]["schedule"] == [{"cron": "30 1 * * *"}]
    assert "workflow_dispatch" in workflows["ingest"]["on"]
    assert workflows["ingest"]["concurrency"] == workflows["backfill"]["concurrency"] == {
        "group": "cmt-paper-feed-data", "cancel-in-progress": "false",
    }
    for name, data in workflows.items():
        assert data["permissions"] == {"contents": "write"}
        steps = data["jobs"][name]["steps"]
        checkout = steps[0]
        assert checkout["with"]["ref"] == "main"
        assert checkout["with"]["persist-credentials"] == "false"
        operation = next(step for step in steps if step.get("id") == name)
        assert operation["continue-on-error"] == "true"
        publish = next(step for step in steps if "feed.workflow publish" in step.get("run", ""))
        assert f"steps.{name}.outcome == 'failure'" in publish["if"]
        assert "!cancelled()" in publish["if"]
        assert steps.index(publish) < len(steps) - 1
        assert f"steps.{name}.outcome == 'failure'" in steps[-1]["if"]
        assert steps[-1]["run"] == "exit 1"
        commands = "\n".join(step.get("run", "") for step in steps)
        assert "$RUNNER_TEMP/feed-data" in commands
        assert "feed embed" not in commands
