# PR 3 — historical import validation

## Source and scope

Imported only original `data/YYYY-MM-DD.jsonl` from the public
[topomat12138/daily-arXiv-ai-enhanced data branch](https://github.com/topomat12138/daily-arXiv-ai-enhanced/tree/4f006b2a6743a154486d7982bed487da9774b778/data).
The exact source commit is `4f006b2a6743a154486d7982bed487da9774b778`.

The inclusive requested range is **2026-04-11 through 2026-10-27**.
There are **193 source files**, **4253 raw rows**, and **2855 unique arXiv IDs**.
The 1398 repeated rows are cross-day duplicates. The specification's approximate
4200 figure matches the raw row count, rather than the unique-paper count of
this snapshot; the exact unique-ID equality is the acceptance criterion.

The range contains 200 calendar dates. The seven dates absent from the source
are `2026-05-18`, `2026-06-05`, `2026-09-24`, `2026-09-25`, `2026-09-26`, `2026-09-27`, `2026-09-28`.
No missing files were invented and no metadata API was queried.

## Commands and real output

The source was shallow-cloned on its data branch with sparse checkout limited
to exact date-named raw JSONL files. No enhanced files, config or feedback were
read by the importer.

A separate local repository on branch `data` was created for the real import.
The command was:

```sh
feed backfill --source-dir /private/tmp/cmt-pr3-legacy --data-dir /private/tmp/cmt-pr3-acceptance
```

First run:

```json
{"files":193,"input_rows":4253,"unique_arxiv_ids":2855,"first_file_date":"2026-04-11","last_file_date":"2026-10-27","new":2855,"updated":0,"stored":2855,"errors":[]}
```

Second run with identical arguments and source:

```json
{"files":193,"input_rows":4253,"unique_arxiv_ids":2855,"first_file_date":"2026-04-11","last_file_date":"2026-10-27","new":0,"updated":0,"stored":2855,"errors":[]}
```

## Independent checks and byte identity

Read the source rows directly with `json.loads` and computed, for every arXiv
ID, the earliest and latest source filenames. Read the generated JSONL directly,
without calling the importer or PaperStore, and verified:

- The output ID set equals the unique source ID set exactly: **2855** records.
- Every first_seen equals the earliest file date at **00:00:00Z**.
- Every last_updated equals the latest observed historical file date at
  **00:00:00Z** in this fresh import; announced also matches that file date.
- Each output record is in the month of its historical first_seen.
- Every submitted and listing_type is null; sources is only arxiv_listing.
- There is **no daily directory or daily JSON file**.

All seven monthly files had identical SHA-256 before and after the second run:

| Month / JSONL shard | Records | SHA-256 (identical before and after) |
| --- | ---: | --- |
| 2026-04 | 319 | `3addeeca477ec81d20ef623bf38cf46832e87d200c73c4b53585afa77ed51dc5` |
| 2026-05 | 382 | `0ffec3cc0253b5388406f986c0cab9644a628c57490c8ab58de80a9ccd1248be` |
| 2026-06 | 434 | `f094cf5014678d268326d1ce620db6e2b6ba5ebe289beab9ce3dce46564d05bf` |
| 2026-07 | 469 | `46092b1e14efd38162bfde243d7eece1efe0a63ed733187d9a0000ecd48ca881` |
| 2026-08 | 396 | `7ba6f10965d3e3ecb81bea26a24cfbb9e98631ce20329358dd3978d6a6f87264` |
| 2026-09 | 423 | `cd1c07bbc9fecb50c641cb3dc7e6904a082c1cba0363ec6e045edc60fc697cc1` |
| 2026-10 | 432 | `4b9120048e4b28de8aaa734574dedc4e24242e09d65d603cc93bbb158813ab02` |

The first output was also committed locally on the isolated data branch as
`dfc889c46d781617b03200805cf06ef1ffc7f99d`. After the second import,
`git diff --exit-code` returned 0 and `git status --short` was empty.

These import artifacts remain in the separate local data repository, outside
the code checkout. They are not included in this code PR and have not been
pushed to a remote data branch.

## Offline verification

**137 tests pass**, including the existing PR 1/2 suite. New tests cover
historical midnight timestamps, summary mapping, month partitioning, byte/mtime
identity, date boundaries, exact filename selection, ignored enhanced/Markdown/
config/feedback files, preservation of richer live metadata, and no reads or
writes of existing daily files. The CLI test rejects HTTP-client construction,
source-configuration loading and runtime-clock reads. The entire suite retains
its socket guard against real network access.

Backfill maps records from source file dates before merging. Normal merge
semantics still protect a newer existing live record and its last_updated
timestamp; first_seen can move earlier, as required by the previous reviews.
