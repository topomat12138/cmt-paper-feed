import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

import feed.cli as cli
import feed.embed as embedding
from feed.config import DEFAULT_MODEL, DEFAULT_REVISION, EmbeddingOptions, load_config
from feed.store import PaperStore

ROOT = Path(__file__).parents[1]


def test_default_embedding_config_is_pinned_and_document_side():
    options = load_config(ROOT / "config/sources.yml").embedding
    assert options == EmbeddingOptions()
    assert options.model == DEFAULT_MODEL and options.revision == DEFAULT_REVISION
    assert options.identifier == f"{DEFAULT_MODEL}@{DEFAULT_REVISION}|task=retrieval|prompt=document"


@pytest.mark.parametrize("settings", [
    {"revision": "main"}, {"model": "https://identity:secret@host/model"},
    {"model": "model\nprivate-output=bad"}, {"task": "retrieval\nsecret"},
    {"prompt_name": "document|query"}, {"prompt_name": "query"},
    {"batch_size": 0}, {"private_interests": ["forbidden"]},
])
def test_embedding_settings_reject_mutable_revisions_credentials_and_private_fields(settings):
    with pytest.raises(ValidationError):
        EmbeddingOptions(**settings)


def test_invalid_vector_configuration_does_not_block_ingest(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sources.yml"
    path.write_text("sources: {arxiv_listing: {categories: [cond-mat.mes-hall]}}\n"
                    "embedding: {revision: invalid}\n")
    assert load_config(path, embedding=False).sources.arxiv_listing.categories == ["cond-mat.mes-hall"]
    seen = []

    class Source:
        def __init__(self, **kwargs):
            seen.extend(kwargs["categories"])

    monkeypatch.setattr(cli, "get_source", lambda name: Source)
    monkeypatch.setattr(cli, "ingest_sources", lambda sources, **kwargs: [
        {"date": "2026-10-28", "fetched": 0, "new": 0, "updated": 0, "errors": []}])
    assert cli.main(["ingest", "--config", str(path), "--data-dir", str(tmp_path / "data")]) == 0
    assert seen == ["cond-mat.mes-hall"]
    assert json.loads(capsys.readouterr().out)["errors"] == []


def test_embed_cli_incremental_json_report_without_real_model(tmp_path, paper, monkeypatch, capsys):
    class Backend:
        def __init__(self, options):
            self.options = options

        def encode(self, texts):
            return np.array([[byte + 1 for byte in hashlib.sha256(text.encode()).digest()[:4]]
                             for text in texts], dtype=np.float32)

    monkeypatch.setattr(embedding, "SentenceTransformerEmbedder", Backend)
    PaperStore(tmp_path).upsert([paper()], day=date(2026, 10, 28), write_daily=False)
    args = ["embed", "--data-dir", str(tmp_path), "--config", str(ROOT / "config/sources.yml")]
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert (first["new"], first["updated"], first["skipped"], first["dimension"]) == (1, 0, 0, 4)
    assert first["elapsed_seconds"] >= 0 and first["model"] == EmbeddingOptions().identifier
    assert cli.main(args) == 0
    again = json.loads(capsys.readouterr().out)
    assert (again["new"], again["updated"], again["skipped"], again["computed"]) == (0, 0, 1, 0)


@pytest.mark.parametrize("invalid_config", [False, True])
def test_failed_embed_outputs_safe_json_summary_and_readable_stderr(tmp_path, monkeypatch, capsys, invalid_config):
    config = ROOT / "config/sources.yml"
    if invalid_config:
        config = tmp_path / "invalid.yml"
        config.write_text("embedding: {revision: private-token}\n")

    def broken(*args, **kwargs):
        raise RuntimeError("private-token private@example.org")

    monkeypatch.setattr(embedding, "embed_papers", broken)
    assert cli.main(["embed", "--data-dir", str(tmp_path), "--config", str(config)]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["new"] == report["updated"] == report["skipped"] == 0
    assert report["elapsed_seconds"] >= 0 and report["errors"][0]["code"] == "embedding_failure"
    assert "embed failed" in captured.err
    assert "private-token" not in captured.out + captured.err
    assert "private@example.org" not in captured.out + captured.err


def test_index_and_search_cli_default_local_index_limit_and_raw(tmp_path, paper, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data-checkout"
    records = [paper(arxiv_id=f"2610.{number:05}", title="Yu-Shiba-Rusinov p+ip Bi2Se3", authors=["Møller", "Second", "Third", "Fourth"])
               for number in range(3)]
    PaperStore(data).upsert(records, day=date(2026, 10, 28), write_daily=False)
    assert cli.main(["search", '"Yu-Shiba-Rusinov" (p+ip) Bi2Se3', "--limit", "2"]) == 0
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 2 and (data / "index.sqlite3").exists()
    assert results[0]["authors"] == ["Møller", "Second", "Third"]
    assert results[0]["announced"] == "2026-10-27" and results[0]["abs"].startswith("https://arxiv.org/abs/")
    assert cli.main(["search", "Moller"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 3
    index = tmp_path / "rebuilt.sqlite3"
    assert cli.main(["index", "--data-dir", str(data), "--out", str(index)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 3
    assert cli.main(["search", 'title:Bi2Se3', "--raw", "--index", str(index), "--limit", "1"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1
    added = paper(arxiv_id="2610.99999", title="Automatically refreshed discovery")
    PaperStore(data).upsert([added], day=date(2026, 10, 28), write_daily=False)
    assert cli.main(["search", "refreshed discovery", "--index", str(index)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["paper_id"] == added.paper_id
    assert cli.main(["search", 'title:(', "--raw", "--index", str(index)]) == 1
    assert "invalid FTS5 query" in capsys.readouterr().err


def test_cli_import_does_not_load_optional_embedding_runtime():
    script = "import sys; import feed.cli; assert not any(n in sys.modules for n in ('torch', 'numpy', 'pyarrow', 'transformers', 'sentence_transformers'))"
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cpu_optional_dependencies_and_lock_contain_no_cuda():
    import tomllib

    settings = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert not any(package.split(">=")[0] in {"torch", "numpy", "pyarrow", "transformers", "sentence-transformers", "peft"}
                   for package in settings["project"]["dependencies"])
    assert "torch" in " ".join(settings["project"]["optional-dependencies"]["embed"])
    index = next(index for index in settings["tool"]["uv"]["index"] if index["name"] == "pytorch-cpu")
    assert index["explicit"] and index["url"] == "https://download.pytorch.org/whl/cpu"
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert not any(package["name"].startswith(("nvidia", "triton")) for package in lock["package"])
