"""Public source and embedding settings; no reader-specific preferences."""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_MODEL = "jinaai/jina-embeddings-v5-text-nano"
DEFAULT_REVISION = "8a7f00aac812071b69403df470f1038ec85f8925"


class EmbeddingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = DEFAULT_MODEL
    revision: str = Field(default=DEFAULT_REVISION, pattern=r"^[0-9a-f]{40}$")
    task: str = "retrieval"
    prompt_name: Literal["document", "passage"] = "document"
    batch_size: int = Field(default=8, ge=1, le=256)

    @field_validator("model", "task", "prompt_name")
    @classmethod
    def public_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", value) or value.startswith(("/", ".")):
            raise ValueError("invalid public model or task identifier")
        return value

    @property
    def identifier(self) -> str:
        return f"{self.model}@{self.revision}|task={self.task}|prompt={self.prompt_name}"


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
    embedding: EmbeddingOptions = Field(default_factory=EmbeddingOptions)


def load_config(path: str | Path, *, embedding: bool = True) -> SourceConfig:
    settings = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not embedding and isinstance(settings, dict):
        # Invalid vector settings must not prevent independent paper ingestion.
        settings = {key: value for key, value in settings.items() if key != "embedding"}
    return SourceConfig.model_validate(settings)
