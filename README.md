# cmt-paper-feed

Public paper metadata for downstream tools. The implementation follows
[`docs/spec-layer1.md`](docs/spec-layer1.md). This PR implements **PR 1 only**:
the uv project, Pydantic v2 data contract, monthly JSONL storage, and tests.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run --no-sync pytest
```

`test.yml` runs these checks on pull requests and pushes to `main`, on Python
3.11, 3.12, and 3.13. Tests use synthetic records and temporary directories and
make no network requests. Installing dependencies requires network access.

## Data contract and storage

Code lives on `main`. Pass the path to a **separate checkout of the `data`
branch** to `PaperStore`; the library does not create branches, run Git, or
fetch sources. No generated paper data belongs on `main`.

```python
from datetime import date

from feed import Paper, PaperStore

paper = Paper(
    arxiv_id="2610.33463v1",
    title="A synthetic paper",
    sources=["arxiv_listing"],
    first_seen="2026-10-28T01:30:00Z",
)
store = PaperStore("/path/to/data-checkout")
changes = store.upsert([paper], day=date(2026, 10, 28))
records = store.load()  # dict keyed by canonical paper_id
```

All fields in the specified Paper contract retain their names. Missing metadata
is represented by `null`, `[]`, or `{}`. `title` and a timezone-aware `first_seen`
are required; omitted `last_updated` uses `first_seen`. Timestamps serialize in
UTC. An explicitly supplied `paper_id` must agree with the metadata.

Identity uses an unversioned arXiv ID, then a lowercase DOI, then the first 12
hex digits of the normalized title's UTF-8 SHA-1. Title normalization applies
NFKC, removes LaTeX command names while keeping argument text, replaces Unicode
punctuation and symbols with spaces, lowercases, and collapses whitespace.
DOI URLs and `doi:` prefixes are accepted and canonicalized.

Storage writes `papers/YYYY/YYYY-MM.jsonl`, partitioned by the original
`first_seen` month in UTC. Records and JSON keys are sorted; output is UTF-8 with
a final newline. Individual files are replaced atomically and only if bytes
change. Existing files with invalid records, duplicate IDs, or incorrect shards
raise an error before writing. Callers must arrange a single writer.

Merge behavior:

- Empty values never erase populated metadata. Sources and categories are
  sorted unions; links merge by key; authors retain their original order.
- `arxiv_version` takes the maximum and the stored `first_seen` stays fixed.
  Nonempty scalar/list metadata follows the higher arXiv version, then the newer
  `last_updated`; stale records may still fill gaps. This prevents older retries
  from restoring old metadata.
- `last_updated` advances only for a metadata change. New observation timestamps
  alone do not rewrite records.
- ID upgrades merge by a shared DOI or an exact normalized title when at least
  one record is title-only. The strongest ID wins and the earliest stored
  `first_seen` is kept when collapsing multiple stored records. Conflicting
  identifiers and ambiguous title-only matches fail without writing.

`daily/YYYY-MM-DD.json` contains exactly `date`, `new`, and `updated`. IDs are
sorted and disjoint. Repeated calls accumulate that day's changes; a record
added and then updated that day stays in `new`. ID upgrades count as updates
and replace obsolete IDs in the current day's manifest. Prior dates' manifests
are historical and remain unchanged. Replaying a batch leaves both the monthly
shards and current daily manifest byte-identical, including after reopening the
store. An empty batch writes an empty manifest if none exists.

The store performs no fuzzy deduplication or metadata resolution. Source
adapters, CLI ingestion, historical import, data-branch Git synchronization,
embeddings, indexing, and email access belong to subsequent PRs.
