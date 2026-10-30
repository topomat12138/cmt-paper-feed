# cmt-paper-feed

Public paper metadata for downstream tools. The implementation follows
[`docs/spec-layer1.md`](docs/spec-layer1.md). Implemented through **PR 6**:
the uv project, Pydantic v2 data contract, monthly JSONL storage, and the arXiv
listing/API source, historical backfill, isolated data-branch workflows,
incremental CPU document vectors, rebuildable local full-text search and public
Scholar title/link dispatch ingestion.

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run --no-sync pytest
```

`test.yml` runs these checks on pull requests and pushes to `main`, on Python
3.11, 3.12, and 3.13. Tests use synthetic records and temporary directories and
make no network requests. Installing dependencies requires network access.
CI also runs SHA-256-pinned actionlint 1.7.12 against all workflow files. Its
configuration has one precise compatibility exception for GitHub's new queue
field; tests independently check the exact queue setting on all data writers.
The development group includes NumPy/PyArrow for fake-vector tests; it does not
install torch or download a model. Production ingest uses `--no-dev` and installs
none of the vector dependencies. Real-model smoke tests are skipped by default.

## arXiv ingestion

```sh
uv sync --locked --no-dev
uv run --no-sync feed ingest --data-dir /path/to/data-checkout
```

`--config` defaults to `config/sources.yml`. By default, ingestion follows the
current announcement shown on each listing page, regardless of the run date.
`--date YYYY-MM-DD` is an optional validation check: mismatching pages produce
diagnostics without importing their papers. The configured categories are `cond-mat.mes-hall` and
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
and `daily/<date>.json`; idempotent storage makes repeated runs harmless. An
explicitly empty listing writes a no-error empty manifest for its page date. If
category pages show different dates, records are grouped into separate daily
files; duplicate IDs use the newer page date. The source returns a `SourceResult`
containing papers and announcement dates, retaining dates even for empty pages.
The CLI prints one report, or an `announcements` list for multiple page dates.

Failures are isolated by source, category and metadata batch. Successful papers
are stored alongside sanitized diagnostics in `daily/<date>.json.errors`, then
the CLI returns a nonzero status. No raw exceptions, HTTP bodies, credentials
or environment dumps are serialized. When every listing fails before its date
is known, diagnostics use the checked date, or the run's UTC date. Unknown-date
failures accompany any known announcement dates discovered by other sources.
The command prints only structured paper counts/dates and errors.

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
uv run --no-sync feed backfill --source-dir /tmp/legacy-arxiv --data-dir /path/to/data-checkout
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

## Scholar dispatch ingestion

Email reading belongs to an external Google Apps Script. This repository does
not contain that script, mailbox credentials, original emails, alert names or
triggering-author information. The external sender submits repository_dispatch
type `scholar_alert` with `client_payload`:

```json
{"version":1,"items":[{"title":"A public paper title","url":"https://arxiv.org/abs/2610.12345"}]}
```

The sender normally sends at most 50 items per request. The receiver validates
up to 200, skips invalid entries with `invalid_item`, and combines equal
normalized titles deterministically. Titles must be nonblank strings of at
most 1000 characters; URLs must be null or HTTP(S) strings of at most 2000.
Google/Scholar links become null. URL query strings/fragments and payload extras
are discarded. Credential-bearing/malformed URLs and invalid Unicode are rejected.

```sh
uv run --no-sync feed scholar --data-dir /path/to/data-checkout --payload /tmp/event.json
uv run --no-sync feed scholar --data-dir /path/to/data-checkout --items-json '[{"title":"Synthetic local test","url":null}]'
```

`--payload` accepts a GitHub event, a versioned payload or an items array. The
manual Actions input `items_json` is decoded inside the process from the event
file; it is never copied into a workflow shell expression or environment value.
The report contains only integer counts: received, valid, deduplicated, failed,
new, updated, resolution_counts and error_counts. It has no item titles, URLs or IDs.

Resolution prefers URL arXiv IDs, then URL DOIs, exact normalized arXiv title
matches, exact normalized OpenAlex title matches, then title-only records.
arXiv abs/pdf/versioned/legacy IDs, APS short DOIs, Wiley and IOP DOI paths,
and Nature article slugs are supported. ScienceDirect PII paths require title
lookup. Long titles ending in … or ... use a unique prefix match only when the
visible part is at least 60 characters; short truncated titles stay unresolved.
Incomplete arXiv search pages cannot establish uniqueness. OpenAlex uses
`filter=title.search:<normalized title>` and checks the first 50 results even
when meta.count is larger. Same-title preprint/journal works contribute a
unique arXiv location and a unique journal DOI (excluding 10.48550/arXiv.*).
If only one identity is unique, only that identity is retained; if neither is
unique, the match is rejected. Truncated prefixes must identify one full
normalized title. Chemical TeX delimiters are removed so MnBi₂Te₄ and
MnBi$_2$Te$_4$ normalize identically.

Title comparisons also use a whitespace-free key after the existing
normalization, so Scholar's `Ag $ *3 $ LiRh $ 2 $ O $ 6$` can match
`Ag$3$LiRh$2$O$6$`. arXiv/OpenAlex exact and prefix comparisons, Scholar batch
deduplication, the title/surname DOI bridge and store title indexes share this
key. Ambiguity checks still apply. `normalize_title` and the whitespace-preserving
title hash used to generate paper_id are unchanged; existing IDs need no migration.

Only fixed arXiv/OpenAlex API endpoints are requested, with the repository URL
as User-Agent and no email or API token. arXiv lookups and metadata share PR 2's
three-second spacing, three-attempt retry budget and batches of at most 100 IDs.
After a partial metadata batch exhausts retries, valid independent entries are
kept; ambiguous duplicate entries are not guessed. OpenAlex metadata supplies
DOI records, authors, reconstructed abstracts and journal names. Sources are
`["scholar_alert"]`; announced and listing_type are null in incoming records.
Existing data uses the normal merge rules and accumulates both source names.
Before storing a DOI-only Scholar record, an indexed lookup can link it to an
existing arXiv record without a DOI when exactly one record has the same
normalized title and a shared author surname. This route is counted as
doi_title_link. Missing/different authors or ambiguous matches keep separate
records; an existing DOI match takes precedence.

Remote failures preserve a known DOI with the supplied title and canonical DOI
link even if metadata is unavailable. Otherwise they retain title-only records.
Safe diagnostics are kept in both cases. A recovered lookup or partial DOI
record does not fail the command; invalid input, failed title-only fallbacks,
storage errors and identifier conflicts still fail. The failed count measures
title-only fallbacks caused by resolution failures.
The command writes the run's UTC daily file, including on
weekends, and accumulates existing new/updated IDs. Diagnostics are replaced per
source so Scholar and arXiv cannot erase one another's errors. Command counts
describe this invocation; replaying the same payload changes no bytes, including
on a later UTC day when there are no new records/errors to put in a new daily.
Layer 2 must process every daily file added or changed since its previous run.

The **Scholar alert ingest** workflow accepts repository_dispatch or manual
`items_json`, runs on trusted main with the shared data lock and contents-only
write permission, and publishes only papers/daily as `data: scholar <new>/<updated>`.
Only checkout/publish get github.token. Counts appear in the step summary;
successful data is published before a failing run is marked red. Its completion
also triggers the separate embeddings workflow. See
[PR 6 validation and manual test](docs/pr6-validation.md).
All four writers use `queue: max` with the shared lock, retaining up to 100
pending runs rather than replacing a pending dispatch with a later batch.

## Document embeddings

```sh
uv sync --locked --no-dev --extra embed
uv run --no-sync feed embed --data-dir /path/to/data-checkout
```

`--config` defaults to `config/sources.yml`. Its public `embedding` section pins
`jinaai/jina-embeddings-v5-text-nano` to commit
`8a7f00aac812071b69403df470f1038ec85f8925`, with `task: retrieval`,
`prompt_name: document` and batch size 8. A revision must be a full commit hash;
changing models requires pinning that model's commit too. Only the document or
passage side is accepted. The model identifier is
`<model>@<revision>|task=<task>|prompt=<prompt_name>`.

The model runs locally on CPU with float32 inference and outputs 768 dimensions.
All model code, weights, adapters and tokenizer are downloaded as one pinned
snapshot, then loaded from its local path; nested upstream loaders cannot fall
back to the model's mutable main branch. No model weights belong in Git. The
Linux/Windows torch dependency uses uv's explicit `pytorch-cpu` index; macOS uses
its CPU-capable PyPI wheel and is also forced to CPU. No CUDA packages are locked.

Text is `title.strip() + "\n\n" + (abstract or "").strip()`. Its `text_hash` is
SHA-256 of the model identifier, a newline, then that text. Missing or changed
hashes are computed; unchanged records do not load a model or recompute. Each
vector is L2-normalized before conversion to float16. Parquet rows are sorted
by paper_id and contain exactly paper_id, model, text_hash and a fixed-size
float16 vector list.

The path `embeddings/YYYY/YYYY-MM.parquet` mirrors each paper shard's first_seen
month. Changed contents replace a shard atomically; unchanged shards retain
their exact bytes and modification time. ID upgrades and month moves can reuse
vectors without inference. Obsolete rows/shards are removed. Inference and
serialization finish before any vector shard is written, and paper JSONL/daily
files are never modified by embedding. Reports contain new/updated/skipped
counts, elapsed_seconds, model identifier, dimension and actual computed count;
failures produce safe JSON plus readable stderr.

## Local full-text search

```sh
uv run --no-sync feed index --data-dir /path/to/data-checkout --out /tmp/papers.sqlite3
uv run --no-sync feed search "Shiba chain" --index /tmp/papers.sqlite3 --data-dir /path/to/data-checkout
uv run --no-sync feed search 'title:Shiba OR authors:Moller' --raw --index /tmp/papers.sqlite3
```

Index/search require only the standard runtime, with SQLite FTS5 support; no
model or vector dependencies are imported. Indexing removes the previous local
SQLite file and rebuilds from all JSONL records. FTS columns are title, abstract
and authors joined by `; `; a regular table retains paper_id, announced,
first_seen, primary_category, abs link and original display metadata. SQLite
files and their sidecars are ignored by Git and are never published.

Search creates a missing or stale index and orders results by bm25, with paper_id as a
stable tie breaker. JSON results show paper_id, title, the first three authors,
announced and abs. The default limit is 20; override it with `--limit`. Without
`--data-dir`, search uses `data-checkout`; without `--index`, it uses
`<data-dir>/index.sqlite3`. Each search compares a stored source fingerprint
(relative JSONL paths and content SHA-256) with the current paper shards.
Added, modified or deleted shards trigger a rebuild; identical contents reuse
the existing index even if checkout timestamps changed. Older index formats
without current metadata are rebuilt automatically.

Queries are literal text by default: tokenize into words and quote each word
before using FTS5, so hyphens, plus signs, quotes and parentheses cannot become
query syntax. `--raw` explicitly enables advanced FTS5 syntax. The tokenizer is
`porter unicode61 remove_diacritics 2`, so English singular/plural forms match
through stemming. SQLite does not decompose ø, so both indexed
search text and queries additionally fold ø/Ø to o/O; displayed metadata keeps
the original spelling. This enables Moller to find Møller. Search is full-text
only; vector retrieval belongs to Layer 2.

## GitHub Actions

After merging, run **Historical arXiv backfill** once using `workflow_dispatch`.
Its date defaults match the historical migration range. It clones only raw
date-named JSONL from the public legacy data branch, runs `feed backfill`, and
publishes changed monthly shards. If this repository has no data branch, the
workflow initializes an orphan data branch with no main history. Repeating an
unchanged import creates no commit.

**Daily arXiv ingest** runs at UTC 01:30 and also supports `workflow_dispatch`.
Its optional date input only validates the current announcement. Both workflows
always check out code from main; data and legacy checkouts and reports live
under `RUNNER_TEMP`, outside the code workspace. Both share concurrency group
`cmt-paper-feed-data`, with running jobs never cancelled by another writer.
Their only declared token permission is `contents: write`. Scholar uses this
same lock and permission with its independent dispatch workflow.

Paper publication allows only `papers/` and `daily/`; embedding publication
allows only `embeddings/`. A clean Git index causes no
commit or push. Daily commit subjects are `data: YYYY-MM-DD`, or a sorted list
of announcement dates when pages span multiple days. Historical subjects use
`data: backfill START..END`. A rejected push is followed by `pull --rebase` and
another push, with at most three push attempts. A content conflict aborts the
rebase and fails the job; no force push or automatic content overwrite occurs.

The ingest step may fail while preserving valid papers and diagnostics. Its
publish step still runs, and a final status step marks the job red afterward.
Backfill likewise publishes valid changes before reporting any import conflict.
Embedding runs in the independent **Paper embeddings** workflow after arXiv
ingest, historical backfill or Scholar ingest completes, including partial
failures, or by workflow_dispatch.
It executes trusted main code, uses the same concurrency group and only
`contents: write` permission, and has a 60-minute limit for the initial historical
calculation. A failed vector calculation cannot undo paper publication.

HF_HOME is a runner temporary directory cached with a key containing the model
name and fixed revision. The workflow installs only the optional CPU runtime,
processes every missing/changed hash, shows its JSON report in the step summary,
and commits only changed vector shards as `data: embeddings <new+updated>`.
An unchanged rerun produces no commit. The daily incremental target is under
10 minutes; measured local timings and the remaining Actions check are recorded
in [PR 5 validation](docs/pr5-validation.md).

Business-step JSON reports are saved in temporary files and copied to the
Actions step summary, including counts and errors even when ingestion fails.
Step errors remain visible in logs. Git output is captured; a terminal failure
prints its operation and the last five error lines per failed operation after
redacting tokens, Authorization headers, URL credentials and email addresses. Token authentication
uses process environment rather than repository URLs, arguments or persistent
Git configuration. HTTP response bodies and environment dumps are not printed.

See [PR 4 validation](docs/pr4-validation.md) for local Git integration checks,
full-history initialization/replay evidence and the post-merge rollout checks.

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
  this invocation only and are persisted in daily errors. Corrupt
  on-disk data and I/O failures still raise exceptions.

`daily/YYYY-MM-DD.json` contains `date`, `new`, `updated`, and the added `errors`
field. Existing files without errors remain readable. IDs are
sorted and disjoint. Repeated calls accumulate that day's changes; a record
added and then updated that day stays in `new`. ID upgrades count as updates
and replace obsolete IDs in the current day's manifest. Prior dates' manifests
are historical and remain unchanged. Replaying a batch leaves both the monthly
shards and current daily manifest byte-identical, including after reopening the
store. An empty batch writes an empty manifest if none exists.

Errors include the original `identifier_conflict` / `ambiguous_title` objects,
`source_failure` diagnostics, and Scholar's `invalid_item` / `resolution_failure`.
They describe the latest attempt per source; recovery clears only that source's
errors, and unchanged failures do not append duplicate events. New/updated IDs
still accumulate within the day. The library's `error_sources` option scopes
diagnostic replacement; `report_current=True` returns per-invocation changes
without changing cumulative daily storage. Scholar also uses
`write_empty_daily=False` to avoid creating new empty manifests on replays.
Backfill never reads or writes daily files.

Matching builds indexes once and performs dictionary lookups per input instead
of scanning and re-normalizing every stored title. Tests bound normalization
calls by the input/store size, without machine-dependent timing assertions.

The store performs no fuzzy deduplication or metadata resolution. Resolution,
embeddings and indexing use the separate components above. Email access stays
outside this repository. Backfill never changes the legacy tracker.
