# PR 5 validation

## Scope and offline checks

Implements PR 5 of spec-layer1.md and the owner's detailed embedding/index
requirements on top of main `777d1af5914ab66c9cf1d8508cee26e178efd726`.
No Scholar/Layer 2 retrieval or generated paper metadata is added.

- 246 offline tests pass; one opt-in real-model smoke test is skipped by default.
  The suite's socket guard remains enabled and no test downloads a model.
- Fake vectors are generated deterministically from text hashes. Tests cover
  unchanged hashes, one replacement abstract, changed model revision, float16
  fixed-size vectors, normalization, sorted monthly shards and byte-identical
  fresh/repeated runs. Month moves and ID upgrades reuse vectors, deleted rows
  disappear, inference failures leave stored vectors unchanged, and identical
  text hashes retain each unchanged record's own vector despite numeric drift.
- Model-loader stubs verify a pinned complete snapshot and local-only CPU
  loading, including document-side task/prompt selection.
- Real local Git tests verify embedding-only publication, rejection of
  unrelated pre-staged changes, no commit/push on unchanged reruns and existing
  bounded rebase/push retries. Workflow trigger/cache/summary/permission tests
  and the PR 1–4 suite remain included.
- FTS5 tests cover a clean rebuild, missing-index generation, bm25/limit,
  original metadata, literal Yu-Shiba-Rusinov/p+ip/Bi2Se3/quotes/parentheses,
  Moller/Møller and other diacritics, explicit raw syntax and safe errors.
- Following Claude's review, the tokenizer is porter unicode61
  remove_diacritics 2. Singular searches match plural text. An index meta table
  stores its format version and all source JSONL relative paths/content hashes.
  Search refreshes after added/modified/deleted papers, detects content changes
  even at unchanged size/mtime, and preserves an unchanged index despite mtime
  changes. Older formats refresh automatically. A mid-read source change fails
  clearly without replacing the index with an inconsistent fingerprint.
- CLI tests cover JSON reports, failed-run summaries with no raw credentials,
  lazy imports and independent ingest despite invalid vector configuration.
- actionlint 1.7.12 passes all workflows. Wheel and source distribution build.

Production dependency export with `--locked --no-dev` contains only the existing
ingest runtime. NumPy/PyArrow are in the optional embed extra and the development
test group; torch/transformers/SentenceTransformers/PEFT are only in the embed
extra. The lock contains CPU torch 2.8.0+cpu from the explicit pytorch-cpu index
on Linux/Windows, and macOS's CPU-capable PyPI torch wheel. No NVIDIA or Triton
packages are locked. Inference always selects CPU.

## Real model verification

The public model is unchanged:

- Name: `jinaai/jina-embeddings-v5-text-nano`
- Fixed Hugging Face revision: `8a7f00aac812071b69403df470f1038ec85f8925`
- Identity: `jinaai/jina-embeddings-v5-text-nano@8a7f00aac812071b69403df470f1038ec85f8925|task=retrieval|prompt=document`
- Measured output dimension: **768**, finite, normalized before float16 storage.
- Runtime: local macOS arm64, Python 3.12.13, CPU float32, torch 2.8.0,
  transformers 4.57.6, sentence-transformers 5.7.0, PEFT 0.21.2,
  NumPy 2.5.4 and PyArrow 24.0.0.

The [official pinned model card](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano/blob/8a7f00aac812071b69403df470f1038ec85f8925/README.md)
documents the document retrieval task. Inspecting the pinned upstream loader
showed that some nested base-model/adapter/tokenizer loads omit revision. The
implementation first obtains the entire pinned snapshot and passes its local
path to SentenceTransformer, with local_files_only enabled, so these nested
calls cannot silently fetch main. It uses eager CPU attention, not FlashAttention.

Initial model download/loading plus one synthetic smoke call completed in
40.663 seconds; cached model loading in the full-data run took 3.497 seconds.
This was a manual validation command outside pytest, using an isolated HF_HOME.
The test-suite smoke can be explicitly selected with a prepopulated cache:

```sh
HF_HOME=/path/to/pinned/cache FEED_REAL_MODEL_SMOKE=1 uv run --no-sync pytest -m model_smoke
```

The smoke test itself forces offline mode; missing cached weights must not
cause downloads in tests.

## Full real-data back-computation

Input: the papers tree from this repository's data branch at
`9cfac719a591ae7e0726a0676b5089789fd4aa4a`, **2898 papers** in seven monthly
shards, first_seen beginning 2026-04-11 and announcements through 2026-10-28.
Only public metadata was copied to an isolated temporary directory.

With four CPU threads and batch size 8:

| Check | Result |
| --- | --- |
| First calculation | new 2898, updated 0, skipped 0, computed 2898 |
| Calculation and Parquet I/O | **316.024550 seconds (5 min 16 sec)** |
| Cached model load, measured separately | 3.497 seconds |
| Output | 7 shards, 768 dimensions, fixed-size float16 lists |
| Immediate rerun | new 0, updated 0, skipped 2898, computed 0 |
| Rerun elapsed | 0.545952 seconds |
| Rerun file writes/deletions | 0 / 0 |
| All shard bytes | Identical |

The model cache, generated vectors and SQLite index remain in temporary
directories outside the code checkout; none are committed to main or pushed to
the real data branch during this validation.

A separate incremental rehearsal kept all 2898 papers and existing vectors
except the 150 most recent records' vectors. With two CPU threads and batch size
8, it computed **150** missing vectors, skipped **2748**, and rewrote one shard
in **17.910254 seconds**. Cached model load was 4.185 seconds. Its immediate
rerun computed zero, took 0.551374 seconds and preserved every shard byte.
This is a local CPU measurement, not a GitHub Actions timing claim.

| Shard | SHA-256 before and after rerun |
| --- | --- |
| embeddings/2026/2026-04.parquet | `7563d3f7484784774200c85949a65665ea06818f775609de87212b7be5e496af` |
| embeddings/2026/2026-05.parquet | `c770f07446c8bede7591fdc79579f8f62f168018f685e0bbca705394686f21b2` |
| embeddings/2026/2026-06.parquet | `c9a387e9e84ef7808909bad92fea1cffde349ed386118e89a6153f84396cd492` |
| embeddings/2026/2026-07.parquet | `edc61e29dfcbe1b3cb2e06e6ce541d7f48905e48eacf96d9baa30d49da746e0f` |
| embeddings/2026/2026-08.parquet | `1af9f40a22672da9ef5728fb8fd34fa9b1d5bd2387b95b21e199c37a0869c1ae` |
| embeddings/2026/2026-09.parquet | `1df25650cdbdb2c3a50c786351b09e4d79628f9de812c71327917ec71e92433e` |
| embeddings/2026/2026-10.parquet | `3115f432a078807c4044d025ce57e64d45d1e685502fced777e95d584ccc175e` |

The full-text index was rebuilt from all 2898 records. A manual literal Moller
query returned a record containing author Martin Møller Greve, retaining the
original spelling in the JSON result.

After the review changes, searching the existing pre-metadata index rebuilt it
automatically. Majorana zero mode and Majorana zero modes returned the same
48 paper IDs; superconductor and superconductors returned the same 530 IDs.
A subsequent search without source changes left the SQLite bytes unchanged.

## Approved changes and implementation details

- Vector paths use embeddings/YYYY/YYYY-MM.parquet, as explicitly approved,
  instead of the original specification's flat embeddings/YYYY-MM.parquet.
- Embedding runs in a separate completed-workflow/dispatch workflow, as
  requested, rather than being appended to ingest. All writers share its lock;
  a vector failure cannot block the preceding paper publication.
- Public model/revision/task settings extend configuration beyond the original
  source/category-only scope, as requested. Unknown/private fields are rejected.
- The model identifier includes retrieval/document selection. Query-side prompt
  settings are rejected. Model code, weights and adapters all use the same SHA.
- unicode61 remove_diacritics 2 does not fold ø; searchable text and queries
  additionally fold ø/Ø to o/O. Display fields keep original metadata.
- Claude's review adds the Porter wrapper for singular/plural matching and
  source content fingerprints for automatic index freshness checks.
- SQLite contains additional original display fields beside the requested
  metadata columns; it is local derived data and excluded from publication.

## Remaining production observation

The production workflow intentionally executes main, so the first real GitHub
embedding dispatch and its initial data-branch publication remain post-merge.
The local timings above do not claim an Actions measurement. After merge,
dispatch Paper embeddings once for the history, then observe an actual daily
incremental run against the ten-minute Actions target. Repeating unchanged
inputs must create no data commit.
