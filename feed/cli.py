"""Paper ingestion, incremental document vectors and local full-text search."""

import argparse
import json
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from feed.backfill import DEFAULT_END, DEFAULT_START, BackfillError, backfill
from feed.config import load_config
from feed.errors import SourceFailure
from feed.ingest import ingest_sources
from feed.resolve import link_doi_papers
from feed.sources import ScholarAlert, get_source
from feed.store import PaperStore


def _embed(args) -> int:
    from feed.embed import EmbedError, embed_papers

    started = time.perf_counter()
    model = None
    try:
        options = load_config(args.config).embedding
    except Exception:
        message = "embedding configuration is invalid; check the public model name, commit revision and document task"
    else:
        model = options.identifier
        try:
            report = embed_papers(args.data_dir, options=options)
        except EmbedError as error:
            message = str(error)
        except Exception:
            message = "embedding failed; check storage permissions and the optional CPU runtime"
        else:
            print(report.model_dump_json())
            return 0
    print(f"embed failed: {message}", file=sys.stderr)
    print(json.dumps({"new": 0, "updated": 0, "skipped": 0,
                      "elapsed_seconds": round(time.perf_counter() - started, 6), "model": model,
                      "errors": [{"code": "embedding_failure", "message": message}]}, sort_keys=True))
    return 1


def _scholar_payload(args) -> object:
    """Read public items locally, never interpolate event bodies into a shell."""
    limit = 2 * 1024 * 1024
    try:
        if args.payload is not None:
            with args.payload.open("rb") as stream:
                raw = stream.read(limit + 1)
        else:
            raw = args.items_json.encode("utf-8")
        if len(raw) > limit:
            return None
        value = json.loads(raw)
        if args.payload is not None and isinstance(value, dict):
            if "client_payload" in value:
                return value["client_payload"]
            if isinstance(value.get("inputs"), dict) and "items_json" in value["inputs"]:
                value = json.loads(value["inputs"]["items_json"])
        return {"version": 1, "items": value} if isinstance(value, list) else value
    except (OSError, ValueError, TypeError):
        return None


def _scholar(args) -> int:
    seen_at = datetime.now(timezone.utc)
    day = seen_at.date()
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            source = ScholarAlert(payload=_scholar_payload(args), client=client)
            fetched = source.fetch(seen_at=seen_at)
        store = PaperStore(args.data_dir)
        papers, linked = link_doi_papers(fetched.papers, store.load().values())
        for number in linked:
            source.stats["resolution_counts"][source.routes[number]] -= 1
        source.stats["resolution_counts"]["doi_title_link"] = len(linked)
        result = store.upsert(
            papers, day=day, source_errors=fetched.errors,
            error_sources={"scholar_alert"}, report_current=True, write_empty_daily=False)
    except Exception:
        # Do not expose event text, HTTP exceptions or filesystem identities.
        # No report is emitted on a fatal storage error, preventing publication
        # of a partial store with an invalid commit report.
        print("scholar failed; check payload access, resolver initialization and data storage", file=sys.stderr)
        return 1
    report = {**source.stats, "new": len(result.new), "updated": len(result.updated),
              "error_counts": dict(sorted(Counter(error.code for error in result.errors).items()))}
    print(json.dumps(report, sort_keys=True))
    if result.errors:
        print(f"scholar completed with {len(result.errors)} diagnostics; resolved records were stored",
              file=sys.stderr)
    return int(source.stats["failed"] > 0 or any(error.code != "resolution_failure" for error in result.errors))


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
    scholar = commands.add_parser("scholar", help="ingest public Scholar dispatch titles and links")
    scholar.add_argument("--data-dir", type=Path, required=True)
    inputs = scholar.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--payload", type=Path, help="GitHub event JSON or versioned payload file")
    inputs.add_argument("--items-json", help="a JSON items array or versioned payload, for local testing")
    vectors = commands.add_parser("embed", help="compute missing or changed document vectors on CPU")
    vectors.add_argument("--data-dir", type=Path, required=True)
    vectors.add_argument("--config", type=Path, default=Path("config/sources.yml"))
    indexer = commands.add_parser("index", help="rebuild a local SQLite FTS5 index")
    indexer.add_argument("--data-dir", type=Path, required=True)
    indexer.add_argument("--out", type=Path, required=True)
    searcher = commands.add_parser("search", help="search local metadata using FTS5 bm25")
    searcher.add_argument("query")
    searcher.add_argument("--limit", type=int, default=20)
    searcher.add_argument("--index", type=Path)
    searcher.add_argument("--data-dir", type=Path, default=Path("data-checkout"))
    searcher.add_argument("--raw", action="store_true", help="use advanced FTS5 query syntax")
    args = parser.parse_args(argv)
    if args.command == "scholar":
        return _scholar(args)
    if args.command == "embed":
        return _embed(args)
    if args.command in {"index", "search"}:
        from feed.build_index import SearchIndexError, build_index, search

        try:
            if args.command == "index":
                result = build_index(args.data_dir, args.out)
            else:
                result = search(args.query, index=args.index or args.data_dir / "index.sqlite3",
                                data_dir=args.data_dir, limit=args.limit, raw=args.raw)
        except SearchIndexError as error:
            print(f"{args.command} failed: {error}", file=sys.stderr)
            return 1
        except (OSError, ValueError):
            print(f"{args.command} failed; check the paper store and local index path", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "backfill":
        try:
            result = backfill(source_dir=args.source_dir, data_dir=args.data_dir, start=args.start, end=args.end)
        except (BackfillError, OSError, ValueError):
            print("backfill failed; source or storage is invalid", file=sys.stderr)
            return 1
        print(result.model_dump_json())
        return 1 if result.errors else 0
    try:
        seen_at = datetime.now(timezone.utc)
        with ExitStack() as stack:
            sources, setup_errors = {}, []
            try:
                config = load_config(args.config, embedding=False)
                client = stack.enter_context(httpx.Client(timeout=60, follow_redirects=True))
                sources["arxiv_listing"] = get_source("arxiv_listing")(
                    categories=config.sources.arxiv_listing.categories, client=client)
            except Exception:
                setup_errors.append(SourceFailure(source="arxiv_listing", stage="source", announced=args.date,
                                                  message="source initialization failed"))
            reports = ingest_sources(sources, data_dir=args.data_dir, day=args.date,
                                     seen_at=seen_at, source_errors=setup_errors)
    except (OSError, ValueError):
        print("arxiv_listing ingest failed; storage or source/configuration is invalid", file=sys.stderr)
        return 1
    print(json.dumps(reports[0] if len(reports) == 1 else {"announcements": reports}, sort_keys=True))
    return 1 if any(report["errors"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
