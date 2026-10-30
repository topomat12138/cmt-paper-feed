"""Collect independent sources, preserving successes alongside diagnostics."""

from datetime import date, datetime
from collections.abc import Iterable
from pathlib import Path

from feed.errors import SourceFailure
from feed.sources.base import Source
from feed.store import PaperStore


def ingest_sources(sources: dict[str, Source], *, data_dir: str | Path,
                   seen_at: datetime, day: date | None = None,
                   source_errors: Iterable[SourceFailure] = ()) -> list[dict]:
    papers, errors = [], list(source_errors)
    dates = set()
    fallback = day or seen_at.date()
    for name, source in sorted(sources.items()):
        try:
            fetched = source.fetch(day=day, seen_at=seen_at)
        except Exception:
            errors.append(SourceFailure(source=name, stage="source", announced=day,
                                        message="source fetch failed"))
            continue
        papers.extend(fetched.papers)
        errors.extend(fetched.errors)
        dates.update(fetched.announcement_dates)
    dates.update(paper.announced or fallback for paper in papers)
    dates.update(error.announced for error in errors if error.announced is not None)
    if not dates:
        dates.add(fallback)
    store = PaperStore(data_dir)
    reports = []
    for announced in sorted(dates):
        batch = [paper for paper in papers if (paper.announced or fallback) == announced]
        failures = [error for error in errors if error.announced in (None, announced)]
        active = set(sources) | {error.source for error in errors}
        merged = store.upsert(batch, day=announced, source_errors=failures, error_sources=active)
        reports.append({"date": announced.isoformat(), "fetched": len(batch), "new": len(merged.new),
                        "updated": len(merged.updated),
                        "errors": [error.model_dump(mode="json") for error in merged.errors]})
    return reports
