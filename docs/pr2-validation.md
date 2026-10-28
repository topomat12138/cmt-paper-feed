# PR 2 validation — 2026-10-28

Scope: arXiv source only, using `cond-mat.mes-hall` and `cond-mat.supr-con` as
requested. No historical import or data-branch push was performed.

## Live check

Executed `feed ingest --date 2026-10-28` twice with a fresh temporary data
directory. Both runs used live listing pages and the live arXiv API, with
requests at least three seconds apart. Each run printed:

```json
{"date":"2026-10-28","errors":[],"fetched":53,"new":53,"updated":0}
```

`new`/`updated` report the cumulative daily manifest, rather than changes in
that particular invocation.

| Listing page | new | cross | replacement | total |
| --- | ---: | ---: | ---: | ---: |
| [mes-hall](https://arxiv.org/list/cond-mat.mes-hall/new) | 15 | 12 | 11 | 38 |
| [supr-con](https://arxiv.org/list/cond-mat.supr-con/new) | 9 | 2 | 6 | 17 |
| Sum before deduplication | 24 | 14 | 17 | 55 |

Independently extracted all `dl dt a[title="Abstract"]` identifiers from the
recorded HTML with BeautifulSoup, stripped versions, and took their set union:
**53 unique IDs**. Read JSONL lines directly with `json.loads` and verified exact
set equality against the stored IDs. Stored listing types after applying
`new > cross > replacement` precedence: **24 new, 13 cross, 16 replacement**.
One API `id_list` request supplies all 53 metadata records.

SHA-256 before and after the second live run was identical for both files:

| File | SHA-256 |
| --- | --- |
| `papers/2026/2026-10.jsonl` | `218ab7ba39ae8ff31aa75f9626274c5e337658f2bea7fc4ee4e71648469aaf34` |
| `daily/2026-10-28.json` | `50438c5c99e365e881e78d61619346d3620574c54eb0a620808eed300902b1ad` |

Generated JSONL/daily data remains outside the code checkout and is not
committed. [The recorded HTTP response bodies](../tests/fixtures/arxiv/2026-10-28)
are included for offline tests, with endpoint and request provenance.

The final page-date implementation was also run live as `feed ingest` without
`--date`, against the same temporary directory. It selected 2026-10-28 from the
pages, returned the same 53-record report, and preserved both SHA-256 values.

## Offline coverage

Tests replay the recorded HTML/XML with `httpx.MockTransport`. A global socket
guard rejects accidental real network access. All **119 tests** pass. Coverage includes:

- All three listing sections, complete metadata mapping, and cross-category
  deduplication with deterministic section precedence.
- Batches of 100/100/1 for 201 IDs, three-second request spacing, and retries.
- Partial and empty HTTP 200 API feeds retry until complete, with at most three
  attempts total shared with HTTP failures. Exhaustion writes no data.
- Existing PR 1 merge behavior and byte-identical replay with a later observed
  timestamp.
- Delayed/weekend runs ingest the page's current announcement; running the
  2026-10-28 recording on 2026-10-31 writes only `daily/2026-10-28.json`.
- Optional date validation rejects both earlier and later mismatches without
  writing. Empty pages preserve their own dates for empty daily files.
- Category pages with different announcement dates produce separate daily files.
- Invalid/truncated listings, missing/duplicate/unexpected API entries, malformed
  metadata, failures before storage, and CLI success/failure paths.

## Explicit implementation choices

- The checked-in categories are the two requested in this PR, replacing the
  specification's four-category default. Only sources/categories are configured.
- BeautifulSoup is the explicitly permitted alternative to selectolax; HTTP is
  via httpx, and XML parsing uses the standard library.
- A minimal `feed ingest` command is included to exercise the source and PR 1
  store. It provides no backfill, scheduling, or Git operations.
- By default, `/new` determines the announcement date, including on delayed
  runs; `--date` only checks that date. This supersedes the specification's
  run-date empty-file behavior on weekends/holidays, per review. An explicitly
  empty listing still produces an empty manifest for its page date. Arbitrary
  historical listing replay remains outside this PR.
