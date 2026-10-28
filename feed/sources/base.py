"""Source interface and registration; orchestration belongs to later PRs."""

from abc import ABC, abstractmethod
from datetime import date, datetime

from feed.schema import Paper


class SourceError(RuntimeError):
    """A source could not supply a complete, valid batch."""


class Source(ABC):
    @abstractmethod
    def fetch(self, *, day: date, seen_at: datetime) -> list[Paper]:
        """Fetch one announcement date without modifying storage."""


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
