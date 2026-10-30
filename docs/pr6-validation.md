# PR 6 validation and manual rollout

Implements PR 6 on main `4cbf88803e614b24e740533e3a98abf764a8e517` with
the owner's external Apps Script → repository_dispatch specification.
The script and email reading are outside this repository and this PR.

## Offline acceptance

- **368 tests pass, 1 real-model smoke defaults to skipped.** The socket guard
  remains enabled. All Scholar payloads and HTTP responses are artificial;
  httpx.MockTransport supplies arXiv/OpenAlex responses. No real email, alert
  content, mailbox credential or model download is used by tests.
- arXiv abs/pdf/version/.pdf/legacy IDs, APS short DOIs, Wiley abs/full/pdf,
  IOP suffixes and Nature slugs are covered. ScienceDirect PII paths are not
  misclassified as DOIs. Query strings, fragments and encoded Nature tracking
  suffixes do not enter canonical links.
- Exact and ambiguous arXiv/OpenAlex title matches, both long-title truncation
  suffixes, the 60-character threshold and OpenAlex prefix ambiguity are covered.
  Incomplete result pages cannot establish uniqueness.
- arXiv metadata is batched at at most 100 IDs with the existing three-second
  spacing/shared three-attempt retry policy. Partial feeds retry and salvage
  valid members. Duplicate conflicting members are not guessed. Other valid
  items survive external failures or malformed remote Unicode.
- Validation covers envelope version, the 200-item cap, title/URL boundaries,
  invalid members, Google links, batch deduplication and Unicode/LaTeX formulas.
  Payload extras and URL tracking never reach stored records or reports.
- CLI tests execute both GitHub event formats and local input modes, verify
  count-only reports, UTC weekend daily files, arXiv source unions, preservation
  of existing announcement metadata, per-invocation update counts and ID upgrades.
- Replaying identical metadata five minutes later or on the next UTC date
  changes no paper or daily bytes. A no-op does not create a new empty daily.
- Per-source diagnostics preserve another writer's failures/conflicts. Recovery
  clears only the recovering source's errors; a foreign failure does not mark a
  successful command failed. Existing new/updated IDs still accumulate.
- A CLI → real local bare Git integration imports two synthetic records,
  publishes only papers/daily as `data: scholar 2/0`, and reruns without another
  commit. The data branch stays at one root commit, and the main SHA is unchanged.
- Publisher tests reject unrelated staged paths, preserve the existing push/
  rebase retry logic and verify no commit on unchanged Scholar reruns.
- Workflow tests check the shared lock, contents-only write permission, both
  triggers, safe event-file input, count summary and publication before failure.
  scholar.yml references no secrets and never interpolates event/input bodies
  into shell or environment values; only checkout/publish receive github.token.
- All workflows pass **actionlint 1.7.12**, with the precise compatibility
  exception for the official queue field described below. Other checks remain
  enabled. Build checks are recorded in the PR.

## Data and privacy boundaries

Incoming Scholar records have sources=["scholar_alert"], announced=null and
listing_type=null. Existing identified records retain populated metadata and
accumulate their original sources. Only canonical arXiv/DOI links are stored;
arbitrary incoming URLs are parsed locally, never fetched. The only remote
endpoints are the arXiv API and OpenAlex API. User-Agent contains the repository
URL and no email. OpenAlex basic access is
[available without a key](https://help.openalex.org/api/authentication/).

Daily diagnostics add invalid_item/resolution_failure objects without raw URLs,
payload bodies, triggering authors or alert labels. Command reports contain only
counts, including error_counts. Fatal storage errors do not emit a publishable
report, so partial storage failures cannot be committed with fabricated counts.

The shared title normalizer removes TeX math delimiters $_{} so MnBi₂Te₄ and
MnBi$_2$Te$_4$ share an identity. The inspected data snapshot
`83c851d711c5d50fce2d8001a43d274364ec2800` has 2898 identified papers and zero
title-only records, so no existing title identity requires migration there.

## Explicit implementation details

- The external sender's 50-item batches and the receiver's 200-item validation
  limit are distinct, as requested. Invalid envelopes fail closed with one
  invalid_item diagnostic; invalid members leave valid members unaffected.
- Both Actions triggers read GITHUB_EVENT_PATH. workflow_dispatch's items_json
  is decoded inside Python; the workflow does not expose it as an env value.
- The command additionally accepts a direct versioned payload or JSON array for
  local tests. The event file is bounded to 2 MiB. Unsafe credential-bearing or
  malformed URLs, invalid Unicode and titles without any normalized text are
  rejected rather than breaking the batch.
- OpenAlex requests use the keyless endpoint with a 0.1-second request-start
  interval and bounded retries. Crossref is optional and is not added.
- Store options report current-call changes while keeping cumulative manifests,
  scope error replacement to each source, and omit newly empty no-op manifests.
  Paper field names and daily date/new/updated/errors fields are unchanged.
- All four data writers use queue: max with their shared concurrency group.
  GitHub's default queue replaces existing pending runs, which could lose
  distinct dispatch batches. The official queue: max option preserves up to
  100 pending runs. See [GitHub's concurrency documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
  The latest actionlint release 1.7.12 and its inspected upstream parser do not
  recognize queue yet. .github/actionlint.yaml filters only that exact unknown-
  queue diagnostic in the four named workflows. Tests require the exact shared
  queue/concurrency settings and verify other lint diagnostics remain unfiltered.
- No production repository_dispatch or workflow_dispatch has been sent as part
  of this implementation. Generated test data stays outside the code checkout.

## Post-merge manual test

1. In this repository's Actions page, open **Scholar alert ingest** → Run workflow
   on **main**. Paste this array into items_json:

```json
[
  {
    "title": "Unpaired Majorana fermions in quantum wires",
    "url": "https://arxiv.org/abs/cond-mat/0010440"
  },
  {
    "title": "Majorana Fermions and a Topological Phase Transition in Semiconductor-Superconductor Heterostructures",
    "url": "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.105.077001"
  }
]
```

These are public, independent examples, verified against
[the arXiv page](https://arxiv.org/abs/cond-mat/0010440) and
[the APS page](https://doi.org/10.1103/PhysRevLett.105.077001), not real alert content.

2. Confirm the step summary shows two valid items, an arxiv_url resolution and
   a doi_url resolution, with no error_counts. Inspect data, not main, for
   arxiv:cond-mat/0010440 and the second record's lower-case DOI
   10.1103/physrevlett.105.077001. OpenAlex may identify its arXiv location; in
   that case paper_id is arxiv-based while doi retains the journal DOI.
3. Check sources contains scholar_alert, first_seen/last_updated use UTC, and
   the current UTC daily file includes the new/updated IDs. A pre-existing
   record may be an update rather than an insertion. Newly created Scholar
   records have null announced/listing_type; old arXiv metadata remains intact.
4. Repeat unchanged input after a successful run. new/updated must both be zero
   and the data branch must gain no commit while remote metadata is unchanged.
5. Confirm **Paper embeddings** is triggered after Scholar ingest completes and
   fills missing/changed hashes through its existing independent workflow.

If an external API is unavailable, title-only fallbacks and error_counts are
expected; resolve the reported source error and rerun before considering the
full metadata lookup test complete. Live rollout is not claimed by offline tests.
