"""Current arXiv ingestion and offline legacy backfill entry points."""

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from feed.backfill import DEFAULT_END, DEFAULT_START, BackfillError, backfill
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
    history = commands.add_parser("backfill", help="import raw JSONL from a local legacy data-branch checkout")
    history.add_argument("--source-dir", type=Path, required=True, help="legacy checkout root containing data/")
    history.add_argument("--data-dir", type=Path, required=True, help="separate destination data checkout")
    history.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    history.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    args = parser.parse_args(argv)
    if args.command == "backfill":
        try:
            result = backfill(source_dir=args.source_dir, data_dir=args.data_dir, start=args.start, end=args.end)
        except (BackfillError, OSError, ValueError):
            print("backfill failed; source or storage is invalid", file=sys.stderr)
            return 1
        print(result.model_dump_json())
        return 1 if result.errors else 0
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
