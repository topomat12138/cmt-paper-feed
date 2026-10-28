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

from pydantic import BaseModel, ConfigDict, Field

from feed.schema import Paper, normalize_title


class DailyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    new: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)


def merge_papers(existing: Paper, incoming: Paper) -> Paper:
    """Merge matching identities, preserving populated fields and first_seen.

    Older revisions may fill gaps but cannot replace newer metadata. Sources,
    categories and individual links are merged without losing existing values.
    last_updated advances only when metadata actually changes.
    """
    for field in ("arxiv_id", "doi"):
        left, right = getattr(existing, field), getattr(incoming, field)
        if left and right and left != right:
            raise ValueError(f"conflicting {field} for a merged paper")
    data = existing.model_dump()
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

    def upsert(self, incoming: Iterable[Paper], *, day: date) -> DailyManifest:
        """Merge a batch and accumulate a sorted daily change manifest.

        Identifier upgrades count as updates. A paper first added today stays
        in `new` if updated again today. Replaying a batch changes no bytes.
        """
        if type(day) is not date:
            raise TypeError("day must be a datetime.date")
        original = self.load()
        papers = dict(original)
        origins = {key: {key} for key in papers}
        aliases = {}
        daily_path = self.root / "daily" / f"{day.isoformat()}.json"
        daily = DailyManifest.model_validate_json(daily_path.read_bytes()) if daily_path.exists() else DailyManifest(date=day)
        if daily.date != day:
            raise ValueError("daily manifest date does not match its filename")

        # Normalize batch order, including duplicate records, before any writes.
        batch = sorted(incoming, key=lambda p: (p.first_seen, p.paper_id, p.last_updated, p.model_dump_json()))
        for paper in batch:
            title = normalize_title(paper.title)
            matches = [key for key, old in papers.items() if (
                key == paper.paper_id
                or (paper.doi is not None and paper.doi == old.doi)
                or (title and normalize_title(old.title) == title
                    and (key.startswith("title:") or paper.paper_id.startswith("title:")))
            )]
            # Never guess between distinct identified papers with the same title.
            strong_ids = {key for key in matches if not key.startswith("title:")}
            if paper.paper_id.startswith("title:") and len(strong_ids) > 1:
                raise ValueError("ambiguous title-only identity")
            previous_ids = set()
            if matches:
                ordered = sorted(matches, key=lambda key: (papers[key].first_seen, key))
                merged = papers[ordered[0]]
                for key in ordered[1:]:
                    merged = merge_papers(merged, papers[key])
                paper = merge_papers(merged, paper)
                for key in matches:
                    previous_ids.update(origins.pop(key))
                    del papers[key]
                    if key != paper.paper_id:
                        aliases[key] = paper.paper_id
            papers[paper.paper_id] = paper
            origins[paper.paper_id] = previous_ids

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
        _write_if_changed(daily_path, _json(daily.model_dump(mode="json")))
        return daily
