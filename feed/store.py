"""Deterministic JSONL storage inside a separately checked-out data branch.

No Git operations or network access are performed here. Writes assume a single
writer; workflow concurrency is configured in the later workflow PR.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from feed.schema import Paper, normalize_title


class DailyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    new: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)


class StoreConflict(BaseModel):
    """A skipped input record; safe to serialize into future daily errors."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["identifier_conflict", "ambiguous_title"] = "identifier_conflict"
    paper_id: str
    matched_ids: list[str]
    field: Literal["arxiv_id", "doi", "title"]
    message: str
    sources: list[str]


class UpsertResult(DailyManifest):
    """Daily changes plus conflicts encountered in this invocation only."""

    errors: list[StoreConflict] = Field(default_factory=list)


class IdentifierConflict(ValueError):
    def __init__(self, field: Literal["arxiv_id", "doi"]):
        self.field = field
        super().__init__(f"conflicting {field} for a merged paper")


def merge_papers(existing: Paper, incoming: Paper) -> Paper:
    """Merge matching identities, preserving populated fields and earliest sighting.

    Older revisions may fill gaps but cannot replace newer metadata. Sources,
    categories and individual links are merged without losing existing values.
    last_updated advances only when metadata actually changes.
    """
    for field in ("arxiv_id", "doi"):
        left, right = getattr(existing, field), getattr(incoming, field)
        if left and right and left != right:
            raise IdentifierConflict(field)
    data = existing.model_dump()
    data["first_seen"] = min(existing.first_seen, incoming.first_seen)
    old_revision = (existing.arxiv_version or 0, existing.last_updated)
    new_revision = (incoming.arxiv_version or 0, incoming.last_updated)
    prefer_incoming = new_revision >= old_revision
    special = {"paper_id", "first_seen", "last_updated", "arxiv_version", "sources", "categories", "links"}
    for key, value in incoming.model_dump().items():
        if key not in special and value not in (None, "", [], {}):
            if prefer_incoming or data[key] in (None, "", [], {}):
                data[key] = value
    data["arxiv_version"] = max(existing.arxiv_version or 0, incoming.arxiv_version or 0) or None
    data["sources"] = sorted(set(existing.sources) | set(incoming.sources))
    data["categories"] = sorted(set(existing.categories) | set(incoming.categories))
    data["links"] = {**incoming.links, **existing.links}
    if prefer_incoming:
        data["links"].update(incoming.links)
    data["paper_id"] = ""
    merged = Paper.model_validate(data)
    if merged == existing:
        return existing
    data["last_updated"] = max(existing.last_updated, incoming.last_updated)
    return Paper.model_validate(data)


class _PaperIndex:
    """Incremental DOI and title indexes over the paper_id map."""

    def __init__(self, papers: dict[str, Paper]):
        self.papers = papers
        self.by_doi: dict[str, set[str]] = defaultdict(set)
        self.by_title: dict[str, set[str]] = defaultdict(set)
        self.unresolved_by_title: dict[str, set[str]] = defaultdict(set)
        self.title_keys: dict[str, str] = {}
        for paper in papers.values():
            self.add(paper)

    def add(self, paper: Paper) -> None:
        if paper.doi:
            self.by_doi[paper.doi].add(paper.paper_id)
        title = normalize_title(paper.title)
        self.by_title[title].add(paper.paper_id)
        self.title_keys[paper.paper_id] = title
        if paper.paper_id.startswith("title:"):
            self.unresolved_by_title[title].add(paper.paper_id)

    def remove(self, paper: Paper) -> None:
        if paper.doi:
            bucket = self.by_doi[paper.doi]
            bucket.remove(paper.paper_id)
            if not bucket:
                del self.by_doi[paper.doi]
        if paper.paper_id in self.title_keys:
            title = self.title_keys.pop(paper.paper_id)
            bucket = self.by_title[title]
            bucket.remove(paper.paper_id)
            if not bucket:
                del self.by_title[title]
            if paper.paper_id.startswith("title:"):
                bucket = self.unresolved_by_title[title]
                bucket.remove(paper.paper_id)
                if not bucket:
                    del self.unresolved_by_title[title]

    def matches(self, paper: Paper) -> set[str]:
        matched = {paper.paper_id} if paper.paper_id in self.papers else set()
        if paper.doi:
            matched.update(self.by_doi.get(paper.doi, ()))
        titles = self.by_title if paper.paper_id.startswith("title:") else self.unresolved_by_title
        if titles:
            matched.update(titles.get(normalize_title(paper.title), ()))
        return matched


def _shard(paper: Paper) -> Path:
    return Path("papers") / paper.first_seen.strftime("%Y") / paper.first_seen.strftime("%Y-%m.jsonl")


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_if_changed(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PaperStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def load(self) -> dict[str, Paper]:
        """Read all monthly shards and reject corrupt/duplicate records."""
        papers = {}
        for path in sorted((self.root / "papers").glob("*/*.jsonl")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    paper = Paper.model_validate_json(line)
                    if path.relative_to(self.root) != _shard(paper):
                        raise ValueError("shard must match first_seen month in UTC")
                    if paper.paper_id in papers:
                        raise ValueError("duplicate paper_id in stored data")
                except ValueError as error:
                    raise ValueError(f"invalid paper at {path}:{number}") from error
                papers[paper.paper_id] = paper
        return papers

    def upsert(self, incoming: Iterable[Paper], *, day: date, write_daily: bool = True) -> UpsertResult:
        """Merge a batch and optionally accumulate a daily change manifest.

        Identifier upgrades count as updates. A paper first added today stays
        in `new` if updated again today. Replaying a batch changes no bytes.
        Conflicting inputs are skipped and returned in errors; valid inputs
        continue. On-disk corruption and I/O errors still fail the operation.
        write_daily=False never reads or writes daily files; returned changes
        then describe this invocation only (used by historical backfill).
        """
        if type(day) is not date:
            raise TypeError("day must be a datetime.date")
        original = self.load()
        papers = dict(original)
        index = _PaperIndex(papers)
        origins = {key: {key} for key in papers}
        aliases = {}
        errors = []
        daily_path = self.root / "daily" / f"{day.isoformat()}.json"
        daily = DailyManifest.model_validate_json(daily_path.read_bytes()) if write_daily and daily_path.exists() else DailyManifest(date=day)
        if daily.date != day:
            raise ValueError("daily manifest date does not match its filename")

        # Normalize batch order, including duplicate records, before any writes.
        batch = sorted(incoming, key=lambda p: (p.first_seen, p.paper_id, p.last_updated, p.model_dump_json()))
        for paper in batch:
            matches = index.matches(paper)
            if paper.paper_id.startswith("title:"):
                identified = {key for key in matches if not key.startswith("title:")}
                if len(identified) > 1:
                    errors.append(StoreConflict(
                        code="ambiguous_title", paper_id=paper.paper_id,
                        matched_ids=sorted(matches), field="title", sources=paper.sources,
                        message="multiple identified papers share the normalized title",
                    ))
                    continue
            previous_ids = set()
            if matches:
                ordered = sorted(matches, key=lambda key: (papers[key].first_seen, key))
                merged = papers[ordered[0]]
                try:
                    for key in ordered[1:]:
                        merged = merge_papers(merged, papers[key])
                    merged = merge_papers(merged, paper)
                except IdentifierConflict as error:
                    errors.append(StoreConflict(
                        paper_id=paper.paper_id, matched_ids=sorted(matches),
                        field=error.field, message=str(error), sources=paper.sources,
                    ))
                    continue
                # Commit this record only after every candidate merged cleanly.
                paper = merged
                for key in matches:
                    index.remove(papers[key])
                    previous_ids.update(origins.pop(key))
                    del papers[key]
                    if key != paper.paper_id:
                        aliases[key] = paper.paper_id
            papers[paper.paper_id] = paper
            origins[paper.paper_id] = previous_ids
            index.add(paper)

        def current_id(key: str) -> str:
            while key in aliases:
                key = aliases[key]
            return key

        added = {current_id(key) for key in daily.new}
        updated = {current_id(key) for key in daily.updated}
        for key, paper in papers.items():
            if not origins[key]:
                added.add(key)
            elif origins[key] != {key} or original.get(key) != paper:
                updated.add(key)
        daily = DailyManifest(date=day, new=sorted(added), updated=sorted(updated - added))

        shards = defaultdict(list)
        for paper in papers.values():
            shards[_shard(paper)].append(paper)
        for relative in sorted({_shard(p) for p in original.values()} | set(shards)):
            path = self.root / relative
            if relative not in shards:
                path.unlink()
            else:
                content = b"".join(_json(p.model_dump(mode="json")) for p in sorted(shards[relative], key=lambda p: p.paper_id))
                _write_if_changed(path, content)
        if write_daily:
            _write_if_changed(daily_path, _json(daily.model_dump(mode="json")))
        return UpsertResult(**daily.model_dump(), errors=errors)
