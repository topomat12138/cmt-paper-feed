"""PR 2 ingestion entry point; other commands arrive in their own PRs."""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from feed.config import load_config
from feed.sources import SourceError, get_source
from feed.store import PaperStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feed")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest the current arXiv announcement")
    ingest.add_argument("--data-dir", type=Path, required=True, help="separate data branch checkout")
    ingest.add_argument("--config", type=Path, default=Path("config/sources.yml"))
    ingest.add_argument("--date", type=date.fromisoformat, help="optional check against the page announcement date")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            source = get_source("arxiv_listing")(categories=config.sources.arxiv_listing.categories, client=client)
            fetched = source.fetch(day=args.date, seen_at=datetime.now(timezone.utc))
        reports = []
        store = PaperStore(args.data_dir)
        for announced in fetched.announcement_dates:
            papers = [paper for paper in fetched.papers if paper.announced == announced]
            result = store.upsert(papers, day=announced)
            reports.append({"date": announced.isoformat(), "fetched": len(papers), "new": len(result.new),
                            "updated": len(result.updated), "errors": [error.model_dump(mode="json") for error in result.errors]})
    except (SourceError, ValidationError, yaml.YAMLError, OSError, ValueError):
        print("arxiv_listing ingest failed; storage or source/configuration is invalid", file=sys.stderr)
        return 1
    print(json.dumps(reports[0] if len(reports) == 1 else {"announcements": reports}, sort_keys=True))
    return 1 if any(report["errors"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
