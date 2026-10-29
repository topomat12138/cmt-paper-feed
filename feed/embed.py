"""Incremental, deterministic document vectors beside the monthly paper shards.

Only this module's operations need the optional embedding dependencies. Merely
importing the CLI or this module does not import torch, NumPy or PyArrow.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from feed.config import EmbeddingOptions
from feed.schema import Paper
from feed.store import PaperStore, _write_if_changed


class EmbedError(ValueError):
    """An actionable embedding failure whose message contains no raw secrets."""


class EmbedReport(BaseModel):
    new: int
    updated: int
    skipped: int
    elapsed_seconds: float
    model: str
    dimension: int | None
    computed: int = 0
    written_shards: int = 0
    removed_shards: int = 0


class Embedder(Protocol):
    def encode(self, texts: list[str]):
        """Return a numeric matrix with one row per input document."""


def paper_text(paper: Paper) -> str:
    return paper.title.strip() + "\n\n" + (paper.abstract or "").strip()


def text_hash(model: str, text: str) -> str:
    return hashlib.sha256((model + "\n" + text).encode("utf-8")).hexdigest()


class SentenceTransformerEmbedder:
    """Pinned document-side retrieval inference on CPU, initialized on demand."""

    def __init__(self, options: EmbeddingOptions):
        try:
            import torch
            from huggingface_hub import snapshot_download
            from sentence_transformers import SentenceTransformer

            # The model's remote loader does not forward revision to all of its
            # nested tokenizer/adapter loads. Resolve a pinned local snapshot
            # first so every nested from_pretrained sees the same local files.
            snapshot = snapshot_download(
                repo_id=options.model,
                revision=options.revision,
                allow_patterns=["*.json", "*.py", "*.safetensors", "*.txt", "*.model", "*.jinja", "adapters/**"],
            )
            self.model = SentenceTransformer(
                snapshot,
                revision=options.revision,
                trust_remote_code=True,
                local_files_only=True,
                device="cpu",
                model_kwargs={"dtype": torch.float32},
                config_kwargs={"_attn_implementation": "eager"},
            )
            self.options = options
        except ImportError:
            raise EmbedError("embedding dependencies are missing; install with uv sync --extra embed") from None
        except Exception:
            raise EmbedError("embedding model initialization failed on CPU; check the pinned model and HF cache") from None

    def encode(self, texts: list[str]):
        try:
            return self.model.encode(
                texts,
                task=self.options.task,
                prompt_name=self.options.prompt_name,
                batch_size=self.options.batch_size,
                convert_to_numpy=True,
                normalize_embeddings=False,
                show_progress_bar=False,
            )
        except Exception:
            raise EmbedError("embedding inference failed on CPU; check the document task and pinned model") from None


def _dependencies():
    try:
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise EmbedError("embedding storage dependencies are missing; install with uv sync --extra embed") from None
    return np, pa, pq


def _read_embeddings(root: Path, np, pa, pq):
    by_id, by_hash, tables = {}, {}, {}
    for path in sorted((root / "embeddings").glob("*/*.parquet")):
        relative = path.relative_to(root / "embeddings").as_posix()
        if not re.fullmatch(r"(\d{4})/\1-(?:0[1-9]|1[0-2])\.parquet", relative):
            raise EmbedError("invalid embedding shard path; expected embeddings/YYYY/YYYY-MM.parquet")
        try:
            table = pq.ParquetFile(path).read()
            if table.column_names != ["paper_id", "model", "text_hash", "vector"]:
                raise ValueError("columns")
            vector_type = table.schema.field("vector").type
            if not pa.types.is_fixed_size_list(vector_type) or vector_type.value_type != pa.float16():
                raise ValueError("vector type")
            if vector_type.list_size < 1:
                raise ValueError("dimension")
            for name in ("paper_id", "model", "text_hash"):
                if table.schema.field(name).type != pa.string():
                    raise ValueError("metadata type")
            for row in table.to_pylist():
                if any(not isinstance(row[key], str) or not row[key] for key in ("paper_id", "model", "text_hash")):
                    raise ValueError("metadata")
                if not re.fullmatch(r"[0-9a-f]{64}", row["text_hash"]):
                    raise ValueError("hash")
                vector = np.asarray(row["vector"], dtype=np.float16)
                if vector.shape != (vector_type.list_size,) or not np.isfinite(vector).all():
                    raise ValueError("vector")
                if not np.isclose(np.linalg.norm(vector.astype(np.float64)), 1.0, atol=0.002):
                    raise ValueError("normalization")
                if row["paper_id"] in by_id:
                    raise ValueError("duplicate ID")
                row["vector"] = vector
                by_id[row["paper_id"]] = row
                by_hash.setdefault((row["model"], row["text_hash"]), vector)
            tables[path] = table
        except Exception:
            raise EmbedError("invalid embedding Parquet shard; rebuild or repair the stored vectors") from None
    return by_id, by_hash, tables


def _normalize(encoded, count: int, np):
    try:
        values = np.asarray(encoded, dtype=np.float64)
        if values.ndim != 2 or values.shape[0] != count or values.shape[1] < 1:
            raise ValueError("shape")
        if not np.isfinite(values).all():
            raise ValueError("non-finite")
        norms = np.linalg.norm(values, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or (norms == 0).any():
            raise ValueError("zero norm")
        return (values / norms).astype(np.float16)
    except (ValueError, TypeError, OverflowError):
        raise EmbedError("embedding backend returned invalid vectors; expected finite, nonzero rows") from None


def _table(rows: list[dict], dimension: int, pa):
    schema = pa.schema([
        pa.field("paper_id", pa.string(), nullable=False),
        pa.field("model", pa.string(), nullable=False),
        pa.field("text_hash", pa.string(), nullable=False),
        pa.field("vector", pa.list_(pa.float16(), dimension), nullable=False),
    ])
    return pa.Table.from_pylist(rows, schema=schema)


def embed_papers(data_dir: str | Path, *, options: EmbeddingOptions,
                 embedder: Embedder | None = None) -> EmbedReport:
    """Compute changed texts only, then atomically replace changed shard bytes.

    Existing vectors are indexed across shards so moving first_seen to an older
    month, or upgrading an ID without changing its text, reuses their values.
    Inference and vector validation finish before any output file is modified.
    """
    started = time.perf_counter()
    np, pa, pq = _dependencies()
    root = Path(data_dir)
    try:
        papers = PaperStore(root).load()
    except (ValueError, OSError):
        raise EmbedError("paper storage is invalid or unreadable; check the monthly JSONL shards") from None
    existing, cached, tables = _read_embeddings(root, np, pa, pq)
    model = options.identifier
    new = updated = skipped = 0
    pending, rows = [], []
    for paper_id in sorted(papers):
        paper = papers[paper_id]
        text = paper_text(paper)
        digest = text_hash(model, text)
        previous = existing.get(paper_id)
        unchanged = previous is not None and previous["model"] == model and previous["text_hash"] == digest
        if previous is None:
            new += 1
        elif unchanged:
            skipped += 1
        else:
            updated += 1
        row = {"paper_id": paper_id, "model": model, "text_hash": digest,
               "vector": previous["vector"] if unchanged else cached.get((model, digest))}
        rows.append(row)
        if row["vector"] is None:
            pending.append((row, text))

    computed = 0
    if pending:
        backend = embedder if embedder is not None else SentenceTransformerEmbedder(options)
        for start in range(0, len(pending), options.batch_size):
            batch = pending[start:start + options.batch_size]
            try:
                encoded = backend.encode([text for _, text in batch])
            except EmbedError:
                raise
            except Exception:
                raise EmbedError("embedding inference failed; no vector shards were changed") from None
            vectors = _normalize(encoded, len(batch), np)
            for (row, _), vector in zip(batch, vectors, strict=True):
                row["vector"] = vector
            computed += len(batch)

    dimensions = {len(row["vector"]) for row in rows}
    if len(dimensions) > 1:
        raise EmbedError("embedding dimensions disagree for the configured model; no vector shards were changed")
    dimension = next(iter(dimensions), None)
    shards = defaultdict(list)
    for row in rows:
        paper = papers[row["paper_id"]]
        path = root / "embeddings" / paper.first_seen.strftime("%Y") / paper.first_seen.strftime("%Y-%m.parquet")
        shards[path].append(row)

    content, written = {}, 0
    for path, entries in sorted(shards.items()):
        table = _table(entries, dimension, pa)
        if path in tables and table.equals(tables[path]):
            continue
        stream = io.BytesIO()
        pq.write_table(table, stream, version="2.6", compression="zstd", use_dictionary=False,
                       data_page_version="2.0", row_group_size=65536, write_statistics=True)
        content[path] = stream.getvalue()
    # Serialization also finishes before writes, so an unsupported Parquet
    # float16 build cannot leave only part of a model migration on disk.
    for path, data in content.items():
        if not path.exists() or path.read_bytes() != data:
            _write_if_changed(path, data)
            written += 1
    obsolete = set(tables) - set(shards)
    for path in sorted(obsolete):
        path.unlink()
    return EmbedReport(new=new, updated=updated, skipped=skipped,
                       elapsed_seconds=round(time.perf_counter() - started, 6),
                       model=model, dimension=dimension, computed=computed,
                       written_shards=written, removed_shards=len(obsolete))
