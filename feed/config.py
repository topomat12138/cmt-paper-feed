"""Source/category configuration only; no reader-specific preferences."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArxivOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[str] = Field(min_length=1)

    @field_validator("categories")
    @classmethod
    def canonical_categories(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?", v) for v in values):
            raise ValueError("invalid arXiv category")
        return sorted(set(values))


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arxiv_listing: ArxivOptions


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: Sources


def load_config(path: str | Path) -> SourceConfig:
    return SourceConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
