"""Rebuildable local SQLite FTS5 index over the public paper metadata.

The index is derived data, never a replacement for the JSONL store and never
published to the data branch. Search results retain the original metadata.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from feed.store import PaperStore


class SearchIndexError(ValueError):
    """A safe, actionable index or query diagnostic."""


def _search_text(text: str) -> str:
    # unicode61 handles combining accents, but ø has no Unicode decomposition.
    # Fold it explicitly on both sides while preserving original result text.
    return text.translate(str.maketrans({"ø": "o", "Ø": "O"}))


def plain_query(query: str) -> str:
    """Treat punctuation and FTS operators as ordinary token boundaries."""
    text = unicodedata.normalize("NFC", _search_text(query))
    words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    return " ".join(f'"{word}"' for word in words)


def _output_path(data_dir: str | Path, out: str | Path) -> Path:
    candidate = Path(out)
    if candidate.is_symlink():
        raise SearchIndexError("index output must not be a symbolic link")
    path = candidate.resolve()
    root = Path(data_dir).resolve()
    if path == root or any(path.is_relative_to(root / area)
                           for area in ("papers", "daily", "embeddings", ".git")):
        raise SearchIndexError("index output must be outside stored paper data and Git metadata")
    if path.exists() and not path.is_file():
        raise SearchIndexError("index output must be a file")
    return path


def build_index(data_dir: str | Path, out: str | Path) -> dict:
    """Delete an old local index and rebuild FTS5 plus original metadata."""
    path = _output_path(data_dir, out)
    papers = PaperStore(data_dir).load()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error:
        raise SearchIndexError("cannot create SQLite index; check output storage") from None
    try:
        with connection:
            connection.execute("""CREATE TABLE papers (
                paper_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                authors TEXT NOT NULL,
                authors_json TEXT NOT NULL,
                announced TEXT,
                first_seen TEXT NOT NULL,
                primary_category TEXT,
                abs TEXT
            )""")
            connection.execute("""CREATE VIRTUAL TABLE papers_fts USING fts5(
                title, abstract, authors,
                tokenize = 'unicode61 remove_diacritics 2'
            )""")
            for rowid, paper in enumerate(sorted(papers.values(), key=lambda p: p.paper_id), 1):
                authors = "; ".join(paper.authors)
                abstract = paper.abstract or ""
                connection.execute("""INSERT INTO papers (
                    rowid, paper_id, title, abstract, authors, authors_json,
                    announced, first_seen, primary_category, abs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    rowid, paper.paper_id, paper.title, abstract, authors,
                    json.dumps(paper.authors, ensure_ascii=False),
                    paper.announced.isoformat() if paper.announced else None,
                    paper.first_seen.isoformat(), paper.primary_category,
                    paper.links.get("abs"),
                ))
                connection.execute("INSERT INTO papers_fts(rowid, title, abstract, authors) VALUES (?, ?, ?, ?)",
                                   (rowid, _search_text(paper.title), _search_text(abstract), _search_text(authors)))
            connection.execute("INSERT INTO papers_fts(papers_fts) VALUES ('optimize')")
    except sqlite3.Error:
        connection.close()
        path.unlink(missing_ok=True)
        raise SearchIndexError("cannot build SQLite index; check FTS5 support and output storage") from None
    finally:
        connection.close()
    return {"records": len(papers), "index": str(path)}


def search(query: str, *, index: str | Path, data_dir: str | Path,
           limit: int = 20, raw: bool = False) -> list[dict]:
    """Find literal words by default, or accept explicit FTS5 syntax via raw."""
    if type(limit) is not int or limit < 1:
        raise SearchIndexError("search limit must be a positive integer")
    path = _output_path(data_dir, index)
    if not path.exists():
        build_index(data_dir, path)
    expression = _search_text(query).strip() if raw else plain_query(query)
    if not expression:
        return []
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        raise SearchIndexError("cannot open SQLite index; check storage or rebuild with feed index") from None
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""SELECT p.paper_id, p.title, p.authors_json, p.announced, p.abs
            FROM papers_fts JOIN papers p ON papers_fts.rowid = p.rowid
            WHERE papers_fts MATCH ?
            ORDER BY bm25(papers_fts), p.paper_id
            LIMIT ?""", (expression, limit)).fetchall()
    except sqlite3.Error:
        message = ("invalid FTS5 query or index; check --raw syntax or rebuild with feed index" if raw
                   else "cannot search SQLite index; rebuild with feed index")
        raise SearchIndexError(message) from None
    finally:
        connection.close()
    return [{"paper_id": row["paper_id"], "title": row["title"],
             "authors": json.loads(row["authors_json"])[:3],
             "announced": row["announced"], "abs": row["abs"]} for row in rows]
