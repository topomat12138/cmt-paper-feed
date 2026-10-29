import hashlib
import os
import sys
from datetime import date
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import feed.embed as embedding
from feed.config import EmbeddingOptions
from feed.embed import EmbedError, embed_papers, paper_text, text_hash
from feed.store import PaperStore

DAY = date(2026, 10, 28)


class FakeEmbedder:
    """Offline deterministic numeric output derived only from the input text."""

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(list(texts))
        return np.asarray([[value + 1 for value in hashlib.sha256(text.encode()).digest()[:8]]
                           for text in texts], dtype=np.float32)


@pytest.fixture
def options():
    return EmbeddingOptions(model="synthetic/document-model", revision="a" * 40, batch_size=2)


def seed(root, paper):
    records = [paper(arxiv_id="2610.00002", title="Second paper", abstract="Second abstract"),
               paper(arxiv_id="2604.00001", title="First paper", abstract="First abstract",
                     first_seen="2026-04-11T00:00:00Z"),
               paper(arxiv_id="2604.00003", title="Third paper", abstract="Third abstract",
                     first_seen="2026-04-12T00:00:00Z")]
    PaperStore(root).upsert(records, day=DAY, write_daily=False)
    return records


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in sorted((root / "embeddings").glob("*/*.parquet"))}


def rows(root):
    return [row for path in sorted((root / "embeddings").glob("*/*.parquet"))
            for row in pq.ParquetFile(path).read().to_pylist()]


def test_text_and_model_hash_contract(paper, options):
    record = paper(title="  Trimmed title  ", abstract="  Trimmed abstract \n")
    text = paper_text(record)
    assert text == "Trimmed title\n\nTrimmed abstract"
    expected = hashlib.sha256((options.identifier + "\n" + text).encode()).hexdigest()
    assert text_hash(options.identifier, text) == expected
    assert text_hash(options.identifier + "changed", text) != expected
    assert paper_text(paper(abstract=None)) == "A synthetic paper\n\n"


def test_float16_normalized_sorted_correct_shards_and_identical_replay(tmp_path, paper, options):
    seed(tmp_path, paper)
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert (result.new, result.updated, result.skipped, result.computed) == (3, 0, 0, 3)
    assert result.model == options.identifier
    assert result.dimension == 8 and result.written_shards == 2
    assert [len(batch) for batch in backend.calls] == [2, 1]
    before = snapshot(tmp_path)
    assert set(before) == {"embeddings/2026/2026-04.parquet", "embeddings/2026/2026-10.parquet"}
    modified = {path: path.stat().st_mtime_ns for path in (tmp_path / "embeddings").glob("*/*.parquet")}
    for path in modified:
        table = pq.ParquetFile(path).read()
        assert table.column_names == ["paper_id", "model", "text_hash", "vector"]
        assert table.schema.field("vector").type == pa.list_(pa.float16(), 8)
        assert table.column("paper_id").to_pylist() == sorted(table.column("paper_id").to_pylist())
        values = np.asarray(table.column("vector").to_pylist(), dtype=np.float16)
        assert values.dtype == np.float16
        assert np.allclose(np.linalg.norm(values.astype(np.float32), axis=1), 1, atol=0.001)
    backend.calls.clear()
    again = embed_papers(tmp_path, options=options, embedder=backend)
    assert (again.new, again.updated, again.skipped, again.computed) == (0, 0, 3, 0)
    assert again.written_shards == 0
    assert backend.calls == [] and snapshot(tmp_path) == before
    assert {path: path.stat().st_mtime_ns for path in modified} == modified


def test_same_inputs_in_fresh_directories_have_identical_parquet_bytes(tmp_path, paper, options):
    left, right = tmp_path / "left", tmp_path / "right"
    seed(left, paper)
    seed(right, paper)
    embed_papers(left, options=options, embedder=FakeEmbedder())
    embed_papers(right, options=options, embedder=FakeEmbedder())
    assert snapshot(left) == snapshot(right)


def test_equal_text_hashes_preserve_each_existing_vector_bytes(tmp_path, paper, options):
    # Separate inference batches can round otherwise equivalent vectors slightly
    # differently. An unchanged paper must keep its own valid stored values.
    records = [paper(arxiv_id="2610.00001"), paper(arxiv_id="2610.00002")]
    PaperStore(tmp_path).upsert(records, day=DAY, write_daily=False)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    path = tmp_path / "embeddings/2026/2026-10.parquet"
    table = pq.ParquetFile(path).read()
    content = table.to_pylist()
    assert content[0]["text_hash"] == content[1]["text_hash"]
    altered = np.asarray(content[1]["vector"], dtype=np.float32)
    altered[0] += 0.001
    content[1]["vector"] = (altered / np.linalg.norm(altered)).astype(np.float16)
    assert not np.array_equal(content[0]["vector"], content[1]["vector"])
    pq.write_table(pa.Table.from_pylist(content, schema=table.schema), path)
    before = path.read_bytes()
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert result.skipped == 2 and result.computed == result.written_shards == 0
    assert backend.calls == [] and path.read_bytes() == before


def test_changed_abstract_recomputes_only_that_paper_and_shard(tmp_path, paper, options):
    records = seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    before = snapshot(tmp_path)
    replacement = records[0].model_copy(update={"abstract": "Replacement updated abstract",
                                                "arxiv_version": 2, "listing_type": "replacement"})
    PaperStore(tmp_path).upsert([replacement], day=DAY, write_daily=False)
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert (result.new, result.updated, result.skipped, result.computed) == (0, 1, 2, 1)
    assert backend.calls == [[paper_text(replacement)]]
    assert result.written_shards == 1
    after = snapshot(tmp_path)
    assert before["embeddings/2026/2026-04.parquet"] == after["embeddings/2026/2026-04.parquet"]
    assert before["embeddings/2026/2026-10.parquet"] != after["embeddings/2026/2026-10.parquet"]


def test_model_revision_change_recomputes_all_papers(tmp_path, paper, options):
    seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    changed = options.model_copy(update={"revision": "b" * 40})
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=changed, embedder=backend)
    assert (result.new, result.updated, result.skipped, result.computed) == (0, 3, 0, 3)
    assert {row["model"] for row in rows(tmp_path)} == {changed.identifier}
    assert sum(map(len, backend.calls)) == 3


def test_new_paper_does_not_recompute_existing_vectors(tmp_path, paper, options):
    seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    record = paper(arxiv_id="2610.00004", title="Fourth paper")
    PaperStore(tmp_path).upsert([record], day=DAY, write_daily=False)
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert (result.new, result.updated, result.skipped, result.computed) == (1, 0, 3, 1)
    assert backend.calls == [[paper_text(record)]]


def test_earlier_first_seen_moves_vector_without_recomputing(tmp_path, paper, options):
    record = paper()
    store = PaperStore(tmp_path)
    store.upsert([record], day=DAY, write_daily=False)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    original = rows(tmp_path)[0]["vector"]
    store.upsert([record.model_copy(update={"first_seen": record.first_seen.replace(month=4, day=11)})],
                 day=DAY, write_daily=False)
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert backend.calls == [] and result.skipped == 1
    assert result.written_shards == result.removed_shards == 1
    assert set(snapshot(tmp_path)) == {"embeddings/2026/2026-04.parquet"}
    assert rows(tmp_path)[0]["vector"] == original


def test_identifier_upgrade_reuses_unchanged_text_vector(tmp_path, paper, options):
    store = PaperStore(tmp_path)
    store.upsert([paper(arxiv_id=None, arxiv_version=None)], day=DAY, write_daily=False)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    original = rows(tmp_path)[0]["vector"]
    store.upsert([paper()], day=DAY, write_daily=False)
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert backend.calls == [] and result.computed == 0
    assert result.new == 1 and result.written_shards == 1
    assert [(row["paper_id"], row["vector"]) for row in rows(tmp_path)] == [(paper().paper_id, original)]


def test_noop_and_empty_store_do_not_initialize_model(tmp_path, paper, options, monkeypatch):
    seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged records must not load a model")

    monkeypatch.setattr(embedding, "SentenceTransformerEmbedder", forbidden)
    assert embed_papers(tmp_path, options=options).computed == 0
    empty = embed_papers(tmp_path / "empty", options=options)
    assert empty.new == empty.skipped == empty.computed == 0
    assert empty.dimension is None and empty.written_shards == 0


def test_removed_paper_shards_remove_obsolete_vector_shards(tmp_path, paper, options):
    seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    (tmp_path / "papers/2026/2026-10.jsonl").unlink()
    backend = FakeEmbedder()
    result = embed_papers(tmp_path, options=options, embedder=backend)
    assert backend.calls == [] and result.removed_shards == 1
    assert set(snapshot(tmp_path)) == {"embeddings/2026/2026-04.parquet"}
    assert not (tmp_path / "daily").exists()


@pytest.mark.parametrize("bad", [np.zeros((2, 8)), np.full((2, 8), np.nan), np.ones((1, 8)), np.ones((2, 0))])
def test_invalid_backend_vectors_never_write_shards(tmp_path, paper, options, bad):
    seed(tmp_path, paper)

    class Invalid:
        def encode(self, texts):
            return bad

    with pytest.raises(EmbedError, match="invalid vectors"):
        embed_papers(tmp_path, options=options, embedder=Invalid())
    assert snapshot(tmp_path) == {}


def test_backend_failure_retains_existing_bytes_and_hides_private_error(tmp_path, paper, options, capsys):
    records = seed(tmp_path, paper)
    embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    before = snapshot(tmp_path)
    PaperStore(tmp_path).upsert([records[0].model_copy(update={"abstract": "Changed"})], day=DAY, write_daily=False)

    class Broken:
        def encode(self, texts):
            raise RuntimeError("private-token and private@example.org")

    with pytest.raises(EmbedError, match="inference failed") as caught:
        embed_papers(tmp_path, options=options, embedder=Broken())
    assert "private" not in str(caught.value)
    assert snapshot(tmp_path) == before
    assert capsys.readouterr() == ("", "")


def test_malformed_existing_parquet_has_safe_diagnostic(tmp_path, paper, options):
    seed(tmp_path, paper)
    path = tmp_path / "embeddings/2026/2026-10.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a Parquet file; private-token")
    with pytest.raises(EmbedError, match="invalid embedding Parquet") as caught:
        embed_papers(tmp_path, options=options, embedder=FakeEmbedder())
    assert "private-token" not in str(caught.value)


def test_real_adapter_pins_all_assets_locally_and_uses_cpu_document_task(tmp_path, options, monkeypatch):
    calls = {}
    local_snapshot = str(tmp_path / "fixed-snapshot")
    float32 = object()

    def download(**kwargs):
        calls["download"] = kwargs
        return local_snapshot

    class Model:
        def __init__(self, path, **kwargs):
            calls["load"] = (path, kwargs)

        def encode(self, texts, **kwargs):
            calls["encode"] = (texts, kwargs)
            return [[3, 4]]

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(float32=float32))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model))
    adapter = embedding.SentenceTransformerEmbedder(options)
    assert adapter.encode(["Paper document"]) == [[3, 4]]
    assert calls["download"]["repo_id"] == options.model
    assert calls["download"]["revision"] == options.revision
    assert "adapters/**" in calls["download"]["allow_patterns"]
    path, kwargs = calls["load"]
    assert path == local_snapshot and kwargs["local_files_only"] is True
    assert kwargs["revision"] == options.revision
    assert kwargs["trust_remote_code"] is True and kwargs["device"] == "cpu"
    assert kwargs["model_kwargs"]["dtype"] is float32
    assert kwargs["config_kwargs"]["_attn_implementation"] == "eager"
    assert calls["encode"] == (["Paper document"], {
        "task": options.task, "prompt_name": options.prompt_name,
        "batch_size": options.batch_size, "convert_to_numpy": True,
        "normalize_embeddings": False, "show_progress_bar": False,
    })


@pytest.mark.model_smoke
@pytest.mark.skipif(os.environ.get("FEED_REAL_MODEL_SMOKE") != "1",
                    reason="manual CPU smoke requires a prepopulated pinned HF cache; no model downloads in tests")
def test_real_model_smoke_from_offline_cache(tmp_path, paper, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    PaperStore(tmp_path).upsert([paper()], day=DAY, write_daily=False)
    result = embed_papers(tmp_path, options=EmbeddingOptions())
    assert result.new == result.computed == 1 and result.dimension == 768
