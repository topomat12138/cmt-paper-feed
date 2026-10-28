# cmt-paper-feed

Public paper metadata for downstream tools. The implementation follows
[`docs/spec-layer1.md`](docs/spec-layer1.md). Implemented through **PR 3**:
the uv project, Pydantic v2 data contract, monthly JSONL storage, and the arXiv
listing/API source, plus offline historical backfill.

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
uv run feed ingest --data-dir /path/to/data-checkout
```

`--config` defaults to `config/sources.yml`. By default, ingestion follows the
current announcement shown on each listing page, regardless of the run date.
`--date YYYY-MM-DD` is an optional validation check: every page must match that
date, or ingestion fails before writing. The configured categories are `cond-mat.mes-hall` and
`cond-mat.supr-con`, as requested for PR 2. The source reads all three sections
of `https://arxiv.org/list/<category>/new?show=2000`: new submissions,
cross-listings, and replacements. It retains cross-listings even when the
primary category is outside the configured categories. For a paper appearing
in multiple sections/categories, `new` takes precedence over `cross`, then
`replacement`, and the highest explicitly listed version is requested.

Metadata is fetched from the arXiv Atom API using `id_list`, in batches of at
most 100 unique IDs. All requests, including retries, start at least three
seconds apart. HTTP failures and HTTP 200 feeds missing requested entries
(including empty feeds) share a maximum of three attempts per request. The source maps
the original API publication date to `submitted`, the listing date to
`announced`, and the observation time to `first_seen`/`last_updated`. All records
have `sources: ["arxiv_listing"]` and pass through the existing `PaperStore`
merge logic. Replaying unchanged inputs leaves stored bytes unchanged.

On weekends/holidays or a delayed run, `/new` may still show the previous
announcement. It is ingested again using the page date for both `announced`
and `daily/<date>.json`; idempotent storage makes repeated runs harmless. Only
an explicitly empty listing writes an empty manifest for its page date. If
category pages show different dates, records are grouped into separate daily
files; duplicate IDs use the newer page date. The source returns a `SourceResult`
containing papers and announcement dates, retaining dates even for empty pages.
The CLI prints one report, or an `announcements` list for multiple page dates.

Malformed/partial listings, API entries still missing after three attempts,
and failed requests return a nonzero exit status before any records are written.
The command prints only counts and structured store conflicts.

`/new` only exposes the latest announcement, so at least one successful run
between consecutive announcements is still required. Arbitrary historical
listing replay is not implemented in this PR.

Recorded HTML and XML for 2026-10-28 are in
[`tests/fixtures/arxiv/2026-10-28`](tests/fixtures/arxiv/2026-10-28).
The two pages contain 38 and 17 entries across all three sections, with
53 unique IDs after deduplication. Tests use `httpx.MockTransport`; a suite-wide
socket guard prevents accidental live network access. Batch-boundary and error
tests mutate copies of these recordings in memory.

## Historical backfill

Check out the public legacy repository's `data` branch, then pass its root
directory and a separate destination data checkout:

```sh
git clone --depth 1 --single-branch --branch data https://github.com/topomat12138/daily-arXiv-ai-enhanced.git /tmp/legacy-arxiv
uv run feed backfill --source-dir /tmp/legacy-arxiv --data-dir /path/to/data-checkout
```

The default inclusive range is 2026-04-11 through 2026-10-27; override it with
`--start YYYY-MM-DD --end YYYY-MM-DD`. Only exact `data/YYYY-MM-DD.jsonl`
filenames are read, without recursion. Enhanced JSONL, Markdown, `config/`,
`feedback/`, and records outside the range are ignored. Backfill performs no
network requests and never loads source configuration.

Each raw record maps `summary` to `abstract` and its file date to `announced`.
Both observation timestamps start at that file date's **00:00 UTC**, so records
are partitioned by historical month, rather than by the backfill run time.
`submitted` and `listing_type` are null and sources is `["arxiv_listing"]`.
Missing arXiv versions stay null; no metadata is fetched to fill them. Without
an explicit primary category, the first legacy category is used.

Backfill validates all input rows before writing and reuses `PaperStore`'s
indexed merge once for the full batch. Repeated IDs retain the earliest
`first_seen`; later historical metadata may advance `last_updated` to another
historical file date. Existing newer live metadata and its timestamp remain
protected by the normal merge rules.

**Backfill never reads, creates, or updates `daily/` files.** It calls upsert
with `write_daily=False`, including when importing into a live data checkout.
Its JSON stdout report contains source file/row counts, unique IDs, date range,
per-invocation new/updated counts, stored count, and conflicts. Errors return a
nonzero exit status. Unchanged replays produce no file changes.

The [real import report](docs/pr3-validation.md) records 193 source files,
4253 input rows, **2855 unique arXiv IDs**, seven monthly shards and the SHA-256
comparison across two imports. The specification's approximately 4200 estimate
matches the raw row count, not the unique-paper count in this source snapshot.

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

The store performs no fuzzy deduplication or metadata resolution. Data-branch
Git synchronization, daily scheduling, embeddings, indexing, and email access
belong to subsequent PRs. Backfill never changes the legacy tracker.
