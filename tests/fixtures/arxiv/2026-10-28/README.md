# Recorded arXiv responses

Recorded on 2026-10-28 from public arXiv endpoints, with at least three seconds
between requests. These are metadata responses, not downloaded papers.

- `cond-mat.mes-hall.html`: `https://arxiv.org/list/cond-mat.mes-hall/new?show=2000`
  — 15 new, 12 cross, 11 replacement entries (38 total).
- `cond-mat.supr-con.html`: `https://arxiv.org/list/cond-mat.supr-con/new?show=2000`
  — 9 new, 2 cross, 6 replacement entries (17 total).
- `api-000.xml`: `https://export.arxiv.org/api/query`, with `id_list` containing
  the sorted union of all listing identifiers and `max_results=53`.
- `recording.json`: the exact recorded ID list and requested categories.

The two pages contain 55 entries and 53 unique unversioned arXiv IDs. Files are
unmodified response bodies. Tests replay them with `httpx.MockTransport` and do
not access the network. Edge-case tests mutate copies of the recordings in
memory (batch boundaries, errors, incomplete pages, and empty announcements).
