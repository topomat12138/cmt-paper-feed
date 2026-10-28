"""Source interface and registration; orchestration belongs to later PRs."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from feed.schema import Paper
from feed.errors import SourceFailure


class SourceError(RuntimeError):
    """A source could not supply a complete, valid batch."""


@dataclass(frozen=True)
class SourceResult:
    announcement_dates: tuple[date, ...]
    papers: list[Paper]
    errors: list[SourceFailure] = field(default_factory=list)


class Source(ABC):
    @abstractmethod
    def fetch(self, *, seen_at: datetime, day: date | None = None) -> SourceResult:
        """Fetch current announcements; day optionally checks the page dates."""


_SOURCES: dict[str, type[Source]] = {}


def register(name: str):
    def decorate(source: type[Source]) -> type[Source]:
        if name in _SOURCES:
            raise ValueError(f"source already registered: {name}")
        _SOURCES[name] = source
        return source
    return decorate


def get_source(name: str) -> type[Source]:
    return _SOURCES[name]
