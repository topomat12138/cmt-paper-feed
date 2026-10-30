"""Public, sanitized source diagnostics for the daily data contract."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["source_failure"] = "source_failure"
    source: str
    stage: Literal["listing", "metadata", "source"]
    message: str
    announced: date | None = None
    category: str | None = None
    paper_ids: list[str] = Field(default_factory=list)


class ScholarIssue(BaseModel):
    """A public dispatch diagnostic without URLs, email or alert provenance."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["invalid_item", "resolution_failure"]
    source: Literal["scholar_alert"] = "scholar_alert"
    stage: Literal["payload", "resolve", "metadata"]
    message: str
    item_index: int | None = Field(default=None, ge=0)
    announced: date | None = None
