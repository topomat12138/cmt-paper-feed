"""Built-in source registrations."""

from feed.sources.arxiv_listing import ArxivListing
from feed.sources.base import Source, SourceError, SourceResult, get_source
from feed.sources.scholar_alert import ScholarAlert

__all__ = ["ArxivListing", "ScholarAlert", "Source", "SourceError", "SourceResult", "get_source"]
