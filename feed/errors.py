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
