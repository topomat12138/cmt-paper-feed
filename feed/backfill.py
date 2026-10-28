"""Import only original date-named JSONL from a local legacy data checkout.

No network, metadata resolution, configuration or feedback files are used.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from feed.schema import Paper
from feed.store import PaperStore, StoreConflict

DEFAULT_START = date(2026, 4, 11)
DEFAULT_END = date(2026, 10, 27)
_RAW_NAME = re.compile(r"\d{4}-\d{2}-\d{2}\.jsonl")


class BackfillError(ValueError):
    """Invalid source input; messages contain locations, not record contents."""


class BackfillResult(BaseModel):
    files: int
    input_rows: int
    unique_arxiv_ids: int
    first_file_date: date
    last_file_date: date
    new: int
    updated: int
    stored: int
    errors: list[StoreConflict] = Field(default_factory=list)


def raw_files(source_dir: str | Path, *, start: date, end: date) -> list[tuple[date, Path]]:
    if start > end:
        raise BackfillError("start date must not follow end date")
    directory = Path(source_dir) / "data"
    if not directory.is_dir():
        raise BackfillError("source checkout must contain a data directory")
    selected = []
    for path in sorted(directory.iterdir()):
        if not _RAW_NAME.fullmatch(path.name) or not path.is_file():
            continue
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError as error:
            raise BackfillError("invalid raw JSONL filename date") from error
        if start <= file_date <= end:
            if path.is_symlink():
                raise BackfillError("raw JSONL files must not be symbolic links")
            selected.append((file_date, path))
    if not selected:
        raise BackfillError("no original date-named JSONL files in the requested range")
    return selected


def legacy_paper(record: dict, *, file_date: date) -> Paper:
    """Map an explicit metadata allowlist; never copy enrichment fields."""
    identifier = record["id"]
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("legacy record requires an arXiv identifier")
    if "://" in identifier:
        url = urlsplit(identifier)
        if url.hostname not in {"arxiv.org", "export.arxiv.org"} or not url.path.startswith("/abs/"):
            raise ValueError("invalid legacy arXiv URL")
        identifier = url.path.removeprefix("/abs/")
    categories = record.get("categories") or []
    observed = datetime.combine(file_date, time.min, tzinfo=timezone.utc)
    return Paper(
        arxiv_id=identifier, arxiv_version=record.get("arxiv_version"),
        doi=record.get("doi"), journal_ref=record.get("journal_ref"),
        title=record["title"], authors=record.get("authors") or [],
        abstract=record.get("summary"), comment=record.get("comment"),
        primary_category=record.get("primary_category") or (categories[0] if categories else None),
        categories=categories, links={key: record[key] for key in ("abs", "pdf") if record.get(key)},
        submitted=None, announced=file_date, listing_type=None,
        sources=["arxiv_listing"], first_seen=observed, last_updated=observed,
    )


def backfill(*, source_dir: str | Path, data_dir: str | Path,
             start: date = DEFAULT_START, end: date = DEFAULT_END) -> BackfillResult:
    files = raw_files(source_dir, start=start, end=end)
    papers = []
    for file_date, path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("legacy JSONL row must be an object")
                papers.append(legacy_paper(record, file_date=file_date))
            except (ValueError, KeyError, TypeError, IndexError) as error:
                raise BackfillError(f"invalid legacy record at data/{path.name}:{number}") from error
    # Validate every source row before writing, then reuse the indexed merge
    # once for the entire history. No daily files are read or generated.
    store = PaperStore(data_dir)
    merged = store.upsert(papers, day=files[-1][0], write_daily=False)
    return BackfillResult(
        files=len(files), input_rows=len(papers), unique_arxiv_ids=len({p.arxiv_id for p in papers}),
        first_file_date=files[0][0], last_file_date=files[-1][0],
        new=len(merged.new), updated=len(merged.updated), stored=len(store.load()), errors=merged.errors,
    )
