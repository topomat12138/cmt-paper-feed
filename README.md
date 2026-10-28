# cmt-paper-feed

Public paper metadata for downstream tools. The implementation follows
[`docs/spec-layer1.md`](docs/spec-layer1.md). Implemented through **PR 2**:
the uv project, Pydantic v2 data contract, monthly JSONL storage, and the arXiv
listing/API source. Historical import is reserved for PR 3.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run --no-sync pytest
```

`test.yml` runs these checks on pull requests and pushes to `main`, on Python
3.11, 3.12, and 3.13. Tests use synthetic records and temporary directories and
make no network requests. Installing dependencies requires network access.

## arXiv ingestion

```sh
uv run feed ingest --data-dir /path/to/data-checkout --date 2026-10-28
```

`--config` defaults to `config/sources.yml`; `--date` defaults to today's UTC
date. The configured categories are `cond-mat.mes-hall` and
`cond-mat.supr-con`, as requested for PR 2. The source reads all three sections
of `https://arxiv.org/list/<category>/new?show=2000`: new submissions,
cross-listings, and replacements. It retains cross-listings even when the
primary category is outside the configured categories. For a paper appearing
in multiple sections/categories, `new` takes precedence over `cross`, then
`replacement`, and the highest explicitly listed version is requested.

Metadata is fetched from the arXiv Atom API using `id_list`, in batches of at
most 100 unique IDs. All requests, including retries, start at least three
seconds apart. Transient failures retry at most three times. The source maps
the original API publication date to `submitted`, the listing date to
`announced`, and the observation time to `first_seen`/`last_updated`. All records
have `sources: ["arxiv_listing"]` and pass through the existing `PaperStore`
merge logic. Replaying unchanged inputs leaves stored bytes unchanged.

On weekends/holidays, when `/new` still shows the previous announcement, the
source writes an empty daily manifest for the requested date. An explicitly
empty listing also succeeds. Malformed/partial listings, missing API entries,
and failed requests return a nonzero exit status before any records are written.
The command prints only counts and structured store conflicts.

`/new` only exposes the latest announcement. A requested date older than the
page's announcement fails explicitly rather than ingesting the wrong day;
arbitrary historical listing replay is not implemented in this PR.

Recorded HTML and XML for 2026-10-28 are in
[`tests/fixtures/arxiv/2026-10-28`](tests/fixtures/arxiv/2026-10-28).
The two pages contain 38 and 17 entries across all three sections, with
53 unique IDs after deduplication. Tests use `httpx.MockTransport`; a suite-wide
socket guard prevents accidental live network access. Batch-boundary and error
tests mutate copies of these recordings in memory.

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
conflicts = [error.model_dump(mode="json") for error in changes.errors]
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

Storage writes `papers/YYYY/YYYY-MM.jsonl`, partitioned by the earliest known
`first_seen` month in UTC. Records and JSON keys are sorted; output is UTF-8 with
a final newline. Individual files are replaced atomically and only if bytes
change. Existing files with invalid records, duplicate IDs, or incorrect shards
raise an error before writing. Callers must arrange a single writer.

Merge behavior:

- Empty values never erase populated metadata. Sources and categories are
  sorted unions; links merge by key; authors retain their original order.
- `arxiv_version` takes the maximum and `first_seen` takes the earlier of the
  stored and incoming values. Historical records can move a paper to an earlier
  month/year shard; the old shard is removed when empty.
  Nonempty scalar/list metadata follows the higher arXiv version, then the newer
  `last_updated`; stale records may still fill gaps. This prevents older retries
  from restoring old metadata.
- `last_updated` advances only for a metadata change. New observation timestamps
  alone do not rewrite records.
- Matching uses a paper_id map, a DOI index, and a normalized-title index for
  all records. An incoming `title:` record merges into an identified record
  only when exactly one identified record matches its normalized title. Multiple
  identified matches skip that input and return an `ambiguous_title` error.
  Identified records never merge with each other by title alone; an unresolved
  title subindex supports their upgrades without scanning identified candidates.
  Indexes follow each successful merge, including ID and title changes. The
  strongest ID wins.
- Conflicting arXiv/DOI identifiers skip only that input record. All matched
  stored records and indexes stay unchanged, and other inputs continue. The
  returned `UpsertResult.errors` contains structured errors with the input
  paper_id, matched IDs, field, sources, and a generic message. Errors describe
  this invocation only; PR 4 can serialize them into daily errors. Corrupt
  on-disk data and I/O failures still raise exceptions.

`daily/YYYY-MM-DD.json` contains exactly `date`, `new`, and `updated`. IDs are
sorted and disjoint. Repeated calls accumulate that day's changes; a record
added and then updated that day stays in `new`. ID upgrades count as updates
and replace obsolete IDs in the current day's manifest. Prior dates' manifests
are historical and remain unchanged. Replaying a batch leaves both the monthly
shards and current daily manifest byte-identical, including after reopening the
store. An empty batch writes an empty manifest if none exists.

Matching builds indexes once and performs dictionary lookups per input instead
of scanning and re-normalizing every stored title. Tests bound normalization
calls by the input/store size, without machine-dependent timing assertions.

The store performs no fuzzy deduplication or metadata resolution. Historical
import, data-branch Git synchronization, daily scheduling, embeddings, indexing,
and email access belong to subsequent PRs. This PR never imports the old 4200
papers or changes the existing tracker.
