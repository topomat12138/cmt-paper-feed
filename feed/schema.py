"""The public Layer 1 data contract and deterministic paper identities."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

_ARXIV = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7}))(?:v(?P<version>[1-9]\d*))?", re.I)


def normalize_title(title: str) -> str:
    """NFKC, remove LaTeX commands/punctuation, lowercase, collapse whitespace.

    Command arguments remain as text (e.g. \\mathrm{BCS} becomes bcs).
    Unicode punctuation and symbols become spaces, preserving word boundaries.
    """
    text = unicodedata.normalize("NFKC", title)
    text = re.sub(r"\\[a-zA-Z]+\*?", "", text)
    text = text.replace("\\", "")
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)
    return " ".join(text.lower().split())


def make_paper_id(*, arxiv_id: str | None, doi: str | None, title: str) -> str:
    """Build an ID from canonical identifiers, in arXiv/DOI/title order."""
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if doi:
        return f"doi:{doi}"
    normalized = normalize_title(title)
    if not normalized:
        raise ValueError("a title-only paper needs a nonempty normalized title")
    digest = hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"title:{digest[:12]}"


class Paper(BaseModel):
    """A complete or partial record, with timezone-aware UTC timestamps.

    first_seen is supplied by the caller rather than read from the wall clock.
    Missing last_updated defaults to first_seen. Unknown fields are rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str = ""
    arxiv_id: str | None = None
    arxiv_version: int | None = Field(default=None, ge=1)
    doi: str | None = None
    journal_ref: str | None = None
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    primary_category: str | None = None
    categories: list[str] = Field(default_factory=list)
    comment: str | None = None
    submitted: date | None = None
    announced: date | None = None
    listing_type: Literal["new", "cross", "replacement"] | None = None
    sources: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)
    first_seen: datetime
    last_updated: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def canonicalize_identifiers(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in ("arxiv_id", "doi"):
            if isinstance(data.get(field), str):
                data[field] = data[field].strip() or None
        if data.get("arxiv_id"):
            raw = str(data["arxiv_id"]).strip().removeprefix("arXiv:").removeprefix("arxiv:")
            match = _ARXIV.fullmatch(raw)
            if not match:
                raise ValueError("invalid arXiv identifier")
            data["arxiv_id"] = match["id"].lower()
            if match["version"]:
                data["arxiv_version"] = max(int(match["version"]), data.get("arxiv_version") or 1)
        if data.get("doi"):
            doi = str(data["doi"]).strip().lower()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                doi = doi.removeprefix(prefix).strip()
            if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
                raise ValueError("invalid DOI")
            data["doi"] = doi
        return data

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()

    @field_validator("arxiv_id", "doi", "journal_ref", "abstract", "primary_category", "comment", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("authors", "categories", "sources")
    @classmethod
    def clean_lists(cls, values: list[str], info: ValidationInfo) -> list[str]:
        cleaned = list(dict.fromkeys(v.strip() for v in values if v.strip()))
        return sorted(cleaned) if info.field_name in {"sources", "categories"} else cleaned

    @field_validator("links", mode="before")
    @classmethod
    def clean_links(cls, values: object) -> object:
        if isinstance(values, dict):
            return {k: v.strip() for k, v in values.items() if isinstance(v, str) and v.strip()}
        return values

    @field_validator("first_seen", "last_updated")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def finalize(self) -> Self:
        expected = make_paper_id(arxiv_id=self.arxiv_id, doi=self.doi, title=self.title)
        if self.paper_id and self.paper_id != expected:
            raise ValueError("paper_id does not match canonical metadata")
        if self.arxiv_version is not None and self.arxiv_id is None:
            raise ValueError("arxiv_version requires arxiv_id")
        object.__setattr__(self, "paper_id", expected)
        if self.last_updated is None:
            object.__setattr__(self, "last_updated", self.first_seen)
        if self.last_updated < self.first_seen:
            raise ValueError("last_updated must not precede first_seen")
        return self
