# PR 4 validation

## Offline checks

- **164 tests pass**, including the PR 1–3 suite, real local Git repositories,
  and recorded arXiv HTML/XML. The socket guard remains enabled.
- **actionlint 1.7.12** passes for all workflows. The local Darwin arm64 binary
  was verified against the official SHA-256; CI independently verifies the
  pinned Linux amd64 archive before running actionlint.
- Workflow tests verify dispatch-only backfill, UTC 01:30 ingest plus dispatch,
  checkout of main, the same non-cancelling concurrency group, contents-only
  write permission, isolated temporary paths, publication after failed ingest,
  and final failed status after publication.

## Required acceptance cases

**No changes means no commit.** With an existing main commit in a local bare
repository, prepare a new orphan data checkout, run ingest or backfill, and
publish. Prepare a fresh second checkout, replay identical input, and publish
again. The data branch still has one commit and the same HEAD; main is unchanged.

**Push conflict retries.** A second real Git checkout pushes a different daily
file just before the first writer's push. The first push is rejected; the
publisher runs `pull --rebase origin data`, retries, and preserves both writers'
changes. A separate failure-injection test verifies three push attempts and two
pull/rebases, with no raw stderr emitted. Content conflicts abort without
overwriting the remote branch.

**Source failure preserves successes.** A successful source and a failed source
produce a valid paper plus a sanitized `source_failure`. Both the paper and
daily diagnostic are committed to a real local bare repository while the run
status remains failed. Repeating the same failure does not create a commit.
Recorded-response tests also verify one failed arXiv category alongside 17
successful papers, and a failed 100-ID API batch alongside 101 successful papers
from the other two batches. Workflow ordering publishes before its final
failure step.

## Full historical bootstrap rehearsal

Using the PR 3 source snapshot
`4f006b2a6743a154486d7982bed487da9774b778`, exercised the production Git helper
and `feed backfill` against an isolated local bare remote. This is not a live
GitHub workflow run and did not publish to the real repository.

| Check | Result |
| --- | --- |
| Source files / rows | 193 / 4253 |
| Unique / stored arXiv IDs | 2855 / 2855 |
| First import | new 2855, updated 0, errors 0 |
| Fresh-checkout replay | new 0, updated 0, errors 0 |
| Data commits after both runs | 1 |
| Initial data history | Root/orphan commit, no main parent |
| Main branch | Unchanged |
| Monthly file bytes | All seven SHA-256 values unchanged |
| Daily files from backfill | None |

The hashes equal [the PR 3 validation hashes](pr3-validation.md). Real generated
data remains outside the code checkout; the source's config, feedback and
enhanced records are not imported.

## Data contract and diagnostics

Only `errors` is added to daily JSON; date/new/updated and every Paper field
retain their names. Missing errors in older daily files default to an empty
list. The current attempt replaces errors so recovery clears resolved failures
and identical failures remain byte-stable. Backfill continues to skip all daily
reads and writes.

Identified batch records are processed before weak title records. This ensures
the first run and replay use the same known candidates for ambiguity checks,
preventing a replay-only diagnostic from creating an otherwise empty commit.

Raw Git output is always captured. Terminal failures print a readable operation
error and at most five sanitized stderr lines per failed Git operation. When a
failed push is followed by a failed pull, both reasons are retained. Tests
simulate rejected pushes and failed branch inspection/cloning, verifying that diagnostics retain the
reason but redact tokens, their Basic-auth encoding, Authorization contents,
URL credentials and email addresses. Credentials remain absent from arguments
and persistent config, and source messages cannot copy private response text.
Publication rejects main, non-data staged files and data checkouts inside the
code workspace.

The workflows retain step stderr and dependency-install diagnostics. Both
successful and failed ingest/backfill reports are copied to GITHUB_STEP_SUMMARY;
tests execute the actual summary shell steps and check counts/errors or a
missing-report diagnostic. Publication still precedes the final failure step.

## Post-merge rollout

The production workflows explicitly execute main, so live dispatch and scheduled
runs are deferred until this PR is merged. First dispatch backfill, then ingest,
then repeat them to verify the real data branch gains no unchanged commits.
The specification's three consecutive business days of unattended execution
remain a post-merge check; this PR does not claim that observation has happened.
No embedding/indexing/email work is included.
