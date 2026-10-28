from collections.abc import Callable

import pytest

from feed.schema import Paper


@pytest.fixture
def paper() -> Callable[..., Paper]:
    """Synthetic metadata only; no HTTP, mailboxes, or personal configuration."""
    def make(**overrides) -> Paper:
        data = {
            "arxiv_id": "2610.33463",
            "arxiv_version": 1,
            "title": "A synthetic paper",
            "authors": ["Example Author"],
            "abstract": "Synthetic abstract for storage tests.",
            "primary_category": "cond-mat.supr-con",
            "categories": ["cond-mat.supr-con"],
            "comment": "10 pages",
            "submitted": "2026-10-26",
            "announced": "2026-10-27",
            "listing_type": "new",
            "sources": ["arxiv_listing"],
            "links": {"abs": "https://arxiv.org/abs/2610.33463"},
            "first_seen": "2026-10-28T01:30:00Z",
            "last_updated": "2026-10-28T01:30:00Z",
        }
        data.update(overrides)
        return Paper.model_validate(data)
    return make
